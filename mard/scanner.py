from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from .db.database import open_session
from .db.models import FileInstance, MediaAsset, StorageDevice
from .db.repository import (
    DeviceRepo, DuplicateGroupRepo, FileInstanceRepo,
    MediaAssetRepo, ScanSessionRepo,
)
from .device import MEDIA_EXTENSIONS, identify_device, is_skip_dir, media_type_of
from .hasher import content_hash as compute_content_hash
from .hasher import quick_hash as compute_quick_hash

_BATCH = 200  # files per DB transaction


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(epoch: float) -> datetime:
    return datetime.utcfromtimestamp(epoch)


def _safe_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def _safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _safe_quick_hash(full_path: Path, file_size: int) -> tuple:
    try:
        return compute_quick_hash(full_path, file_size)
    except OSError:
        return None, False


def _safe_content_hash(full_path: Path):
    try:
        return compute_content_hash(full_path)
    except OSError:
        return None


# ── result ───────────────────────────────────────────────────────────────────

class ScanResult:
    __slots__ = (
        "total", "new", "updated", "skipped", "gone",
        "io_errors", "quick_hash_candidates", "content_hashed",
        "exact_dup_groups", "exact_dup_files",
    )

    def __init__(self) -> None:
        self.total = 0
        self.new = 0
        self.updated = 0
        self.skipped = 0
        self.gone = 0
        self.io_errors = 0
        self.quick_hash_candidates = 0
        self.content_hashed = 0
        self.exact_dup_groups = 0
        self.exact_dup_files = 0


# ── batch processing ─────────────────────────────────────────────────────────

def _process_file_batch(
    pending: list[dict],
    factory: sessionmaker,
    device_id: str,
    session_id: str,
    result: ScanResult,
    executor: Optional[ThreadPoolExecutor],
) -> None:
    """Hash + write one batch of files. Updates result counters in-place."""

    with open_session(factory) as s:
        fi_repo = FileInstanceRepo(s)

        # ── step 1: DB lookup → split into unchanged / needs-hash ──────────
        to_hash: list[tuple[Path, int, object, dict]] = []
        for meta in pending:
            existing = fi_repo.get_by_device_path(device_id, meta["rel_path"])
            if (
                existing
                and existing.size == meta["file_size"]
                and existing.mtime == meta["file_mtime"]
            ):
                existing.last_scan_session_id = session_id
                existing.exists = True
                s.add(existing)
                result.skipped += 1
            else:
                to_hash.append(
                    (meta["full_path"], meta["file_size"], existing, meta)
                )

        # ── step 2: hash (parallel if executor, else sequential) ───────────
        hash_inputs = [(fp, sz) for fp, sz, _, _ in to_hash]
        if executor and len(hash_inputs) > 1:
            futures = [
                executor.submit(_safe_quick_hash, fp, sz)
                for fp, sz in hash_inputs
            ]
            hash_results = [f.result() for f in futures]
        else:
            hash_results = [
                _safe_quick_hash(fp, sz) for fp, sz in hash_inputs
            ]

        # ── step 3: write to DB ────────────────────────────────────────────
        for (_, _, existing, meta), (qhash, is_full) in zip(to_hash, hash_results):
            if qhash is None:
                result.io_errors += 1
                continue

            rel_path = meta["rel_path"]
            fname = meta["fname"]
            ext = meta["ext"]
            file_size = meta["file_size"]
            file_mtime = meta["file_mtime"]
            file_ctime = meta["file_ctime"]
            inode = meta["inode"]

            if existing is None:
                fi = FileInstance(
                    device_id=device_id,
                    path=rel_path,
                    file_name=fname,
                    extension=ext,
                    size=file_size,
                    mtime=file_mtime,
                    ctime=file_ctime,
                    inode_or_file_id=inode,
                    quick_hash=qhash,
                    content_hash=qhash if is_full else None,
                    scan_at=_utcnow(),
                    last_scan_session_id=session_id,
                    exists=True,
                )
                fi_repo.save(fi)
                result.new += 1
            else:
                existing.size = file_size
                existing.mtime = file_mtime
                existing.ctime = file_ctime
                existing.quick_hash = qhash
                existing.content_hash = qhash if is_full else None
                existing.scan_at = _utcnow()
                existing.last_scan_session_id = session_id
                existing.exists = True
                s.add(existing)
                result.updated += 1


# ── main scan ────────────────────────────────────────────────────────────────

def scan_device(
    mount_path: Path,
    session_factory: sessionmaker,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    workers: int = 1,
) -> ScanResult:
    """Full scan pipeline: walk → quick hash → content hash → dedup grouping.

    Parameters
    ----------
    workers : int
        Number of threads for parallel quick_hash computation (Phase 1).
        Default 1 (sequential).  Increase for SSDs; keep low for spinning HDDs.
    """
    result = ScanResult()
    mount_str = str(mount_path)

    # ── Phase 0: identify device ──────────────────────────────────────────
    device_info = identify_device(mount_path)
    with open_session(session_factory) as s:
        dev_repo = DeviceRepo(s)
        device = dev_repo.get_by_marker_id(device_info["device_marker_id"])
        if device is None:
            device = StorageDevice(
                device_marker_id=device_info["device_marker_id"],
                volume_label=device_info["volume_label"],
                filesystem_uuid=device_info.get("filesystem_uuid"),
                mount_hint=mount_str,
            )
            dev_repo.save(device)
        else:
            dev_repo.touch(device.id, mount_str)
        device_id = device.id

        sess_repo = ScanSessionRepo(s)
        scan_session = sess_repo.create(device_id)
        session_id = scan_session.id

    # ── Phase 1: walk + quick hash (batched) ──────────────────────────────
    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    pending: list[dict] = []
    processed = 0

    try:
        for dirpath, dirnames, filenames in os.walk(mount_str):
            dirnames[:] = [d for d in dirnames if not is_skip_dir(d)]

            for fname in filenames:
                full_path = Path(dirpath) / fname
                ext = full_path.suffix.lower()
                if ext not in MEDIA_EXTENSIONS:
                    continue
                if _safe_is_symlink(full_path):
                    continue

                result.total += 1
                rel_path = str(full_path.relative_to(mount_path)).replace("\\", "/")

                st = _safe_stat(full_path)
                if st is None:
                    result.io_errors += 1
                    processed += 1
                    continue

                file_size = st.st_size
                file_mtime = _ts(st.st_mtime)
                file_ctime = _ts(st.st_ctime)
                try:
                    inode = str(st.st_ino) if st.st_ino != 0 else None
                except AttributeError:
                    inode = None

                pending.append({
                    "full_path": full_path,
                    "rel_path": rel_path,
                    "fname": fname,
                    "ext": ext,
                    "file_size": file_size,
                    "file_mtime": file_mtime,
                    "file_ctime": file_ctime,
                    "inode": inode,
                })

                if len(pending) >= _BATCH:
                    _process_file_batch(
                        pending, session_factory, device_id, session_id,
                        result, executor,
                    )
                    processed += len(pending)
                    pending.clear()

                    if progress_cb:
                        progress_cb(processed, result.total, rel_path)

        # flush final batch
        if pending:
            _process_file_batch(
                pending, session_factory, device_id, session_id,
                result, executor,
            )
            processed += len(pending)
            if progress_cb:
                progress_cb(processed, result.total, pending[-1]["rel_path"])

    except KeyboardInterrupt:
        with open_session(session_factory) as s:
            ScanSessionRepo(s).interrupt(session_id, processed)
        raise
    finally:
        if executor:
            executor.shutdown(wait=False)

    # ── Phase 1b: mark files that disappeared ─────────────────────────────
    with open_session(session_factory) as s:
        result.gone = FileInstanceRepo(s).mark_missing_after_scan(device_id, session_id)

    # ── Phase 2: content hash for quick_hash collision candidates ─────────
    with open_session(session_factory) as s:
        fi_repo = FileInstanceRepo(s)
        candidates = fi_repo.get_quick_hash_collision_ids()
        result.quick_hash_candidates = sum(
            len(fi_repo.get_by_quick_hash_and_size(qh, sz)) for qh, sz in candidates
        )

    for qhash, size in candidates:
        with open_session(session_factory) as s:
            fi_repo = FileInstanceRepo(s)
            group = fi_repo.get_by_quick_hash_and_size(qhash, size)
            for fi in group:
                if fi.content_hash is not None:
                    continue
                full_path = mount_path / fi.path
                chash = _safe_content_hash(full_path)
                if chash is None:
                    continue
                fi_repo.update_content_hash(fi.id, chash)
                result.content_hashed += 1

    # ── Phase 3: group exact duplicates ──────────────────────────────────
    with open_session(session_factory) as s:
        fi_repo = FileInstanceRepo(s)
        asset_repo = MediaAssetRepo(s)
        dup_repo = DuplicateGroupRepo(s)

        dup_groups = fi_repo.get_exact_duplicate_groups()
        result.exact_dup_groups = len(dup_groups)

        for group in dup_groups:
            result.exact_dup_files += len(group)
            chash = group[0].content_hash

            asset = asset_repo.get_by_content_hash(chash)
            if asset is None:
                asset = MediaAsset(
                    content_hash=chash,
                    size=group[0].size,
                    media_type=media_type_of(group[0].extension or ""),
                )
                asset_repo.save(asset)

            for fi in group:
                fi.media_asset_id = asset.id
                s.add(fi)

            keep = min(group, key=lambda f: (f.mtime or datetime.max, f.device_id, f.path))

            if dup_repo.get_by_content_hash(chash) is None:
                dup_repo.create_exact_group(
                    instance_ids=[f.id for f in group],
                    recommended_keep_id=keep.id,
                    content_hash=chash,
                )

        ScanSessionRepo(s).complete(session_id, result.total)

    return result
