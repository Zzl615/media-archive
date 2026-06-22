from __future__ import annotations

import os
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(epoch: float) -> datetime:
    return datetime.utcfromtimestamp(epoch)


class ScanResult:
    def __init__(self) -> None:
        self.total = 0
        self.new = 0
        self.updated = 0
        self.skipped = 0
        self.gone = 0
        self.quick_hash_candidates = 0
        self.content_hashed = 0
        self.exact_dup_groups = 0
        self.exact_dup_files = 0


def scan_device(
    mount_path: Path,
    session_factory: sessionmaker,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> ScanResult:
    """Full scan pipeline: walk → quick hash → content hash → dedup grouping."""
    result = ScanResult()
    mount_str = str(mount_path)

    # ── Phase 0: identify device ──────────────────────────────────────────────
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

    # ── Phase 1: walk + quick hash ────────────────────────────────────────────
    processed = 0
    try:
        for dirpath, dirnames, filenames in os.walk(mount_str):
            # Prune skipped dirs in-place so os.walk won't descend into them
            dirnames[:] = [d for d in dirnames if not is_skip_dir(d)]

            for fname in filenames:
                full_path = Path(dirpath) / fname
                ext = full_path.suffix.lower()
                if ext not in MEDIA_EXTENSIONS:
                    continue
                if full_path.is_symlink():
                    continue

                result.total += 1
                rel_path = str(full_path.relative_to(mount_path)).replace("\\", "/")

                try:
                    st = full_path.stat()
                except OSError:
                    continue

                file_size = st.st_size
                file_mtime = _ts(st.st_mtime)
                file_ctime = _ts(st.st_ctime)
                try:
                    inode = str(st.st_ino) if st.st_ino != 0 else None
                except AttributeError:
                    inode = None

                with open_session(session_factory) as s:
                    fi_repo = FileInstanceRepo(s)
                    existing = fi_repo.get_by_device_path(device_id, rel_path)

                    if existing and existing.size == file_size and existing.mtime == file_mtime:
                        # File unchanged — just refresh session tag
                        existing.last_scan_session_id = session_id
                        existing.exists = True
                        s.add(existing)
                        result.skipped += 1
                    else:
                        try:
                            qhash, is_full = compute_quick_hash(full_path, file_size)
                        except OSError:
                            continue

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
                                # If file < threshold, quick_hash == content_hash
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

                processed += 1
                if progress_cb:
                    progress_cb(processed, result.total, rel_path)

    except KeyboardInterrupt:
        with open_session(session_factory) as s:
            ScanSessionRepo(s).interrupt(session_id, processed)
        raise

    # ── Phase 1b: mark files that disappeared ─────────────────────────────────
    with open_session(session_factory) as s:
        result.gone = FileInstanceRepo(s).mark_missing_after_scan(device_id, session_id)

    # ── Phase 2: content hash for quick_hash collision candidates ─────────────
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
                try:
                    chash = compute_content_hash(full_path)
                except OSError:
                    continue
                fi_repo.update_content_hash(fi.id, chash)
                result.content_hashed += 1

    # ── Phase 3: group exact duplicates ──────────────────────────────────────
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

            # Recommend: earliest mtime wins; ties broken by smallest device+path
            keep = min(group, key=lambda f: (f.mtime or datetime.max, f.device_id, f.path))

            if dup_repo.get_by_content_hash(chash) is None:
                dup_repo.create_exact_group(
                    instance_ids=[f.id for f in group],
                    recommended_keep_id=keep.id,
                    content_hash=chash,
                )

        ScanSessionRepo(s).complete(session_id, result.total)

    return result
