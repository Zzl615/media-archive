from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from .db.database import open_session
from .db.models import ArchiveStatus
from .db.repository import DeviceRepo, FileInstanceRepo, MediaAssetRepo
from .hasher import content_hash as compute_content_hash, short_hex


class PlanEntry:
    __slots__ = (
        # identity
        "asset_id", "instance_id", "device_id",
        # source / target
        "source_path", "target_path",
        # content
        "content_hash", "size",
        # time
        "taken_at", "date_source",
        # media info
        "media_type", "width", "height",
        # device readable name
        "device_label",
        # duplicate context
        "duplicate_copies", "duplicate_devices",
        # flags (set by human/AI before apply)
        "skip", "name_collision",
    )

    def __init__(self, **kw):
        # defaults for all slots (handles old plan files missing new fields)
        _defaults = {
            "skip": False,
            "name_collision": False,
            "duplicate_copies": 1,
            "duplicate_devices": [],
            "date_source": None,
            "media_type": None,
            "width": None,
            "height": None,
            "device_label": None,
        }
        for s in self.__slots__:
            object.__setattr__(self, s, _defaults.get(s))
        for k, v in kw.items():
            if k in self.__slots__:
                setattr(self, k, v)

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "PlanEntry":
        return cls(**d)


# ── Plan generation ───────────────────────────────────────────────────────────

def generate_plan(
    archive_root: Path,
    session_factory: sessionmaker,
    device_id: Optional[str] = None,
) -> list[PlanEntry]:
    plan: list[PlanEntry] = []

    with open_session(session_factory) as s:
        device_names: dict[str, str] = {
            d.id: (d.volume_label or d.mount_hint or d.id[:8])
            for d in DeviceRepo(s).get_all()
        }

        asset_repo = MediaAssetRepo(s)
        fi_repo = FileInstanceRepo(s)

        assets = asset_repo.get_pending_archive()
        seen_targets: set[str] = set()

        for asset in assets:
            instances = fi_repo.get_by_asset_id(asset.id)
            if not instances:
                continue

            all_existing = [i for i in instances if i.exists]
            candidates = all_existing
            if device_id:
                preferred = [i for i in candidates if i.device_id == device_id]
                candidates = preferred or candidates

            if not candidates:
                continue

            best = min(candidates, key=lambda f: (f.mtime or datetime.max, f.path))
            chash = asset.content_hash or best.quick_hash or ""
            ext = Path(best.file_name).suffix

            target = _build_target_path(archive_root, asset.taken_at, best.file_name, chash, ext)

            # Collision detection within this plan run
            target_str = str(target)
            name_collision = False
            if target_str in seen_targets:
                base = target.stem
                target = target.with_stem(f"{base}_{best.id[:6]}")
                target_str = str(target)
                name_collision = True
            seen_targets.add(target_str)

            # All devices that hold a copy of this asset
            dup_devices = sorted({
                device_names.get(i.device_id, i.device_id[:8])
                for i in all_existing
            })

            plan.append(PlanEntry(
                asset_id=asset.id,
                instance_id=best.id,
                device_id=best.device_id,
                device_label=device_names.get(best.device_id, best.device_id[:8]),
                source_path=best.path,
                target_path=target_str,
                content_hash=asset.content_hash,
                size=asset.size,
                taken_at=asset.taken_at.isoformat() if asset.taken_at else None,
                date_source=asset.taken_at_source,
                media_type=asset.media_type,
                width=asset.width,
                height=asset.height,
                duplicate_copies=len(all_existing),
                duplicate_devices=dup_devices,
                skip=False,
                name_collision=name_collision,
            ))

    return plan


def _build_target_path(
    archive_root: Path,
    taken_at: Optional[datetime],
    original_name: str,
    chash: str,
    ext: str,
) -> Path:
    if taken_at:
        date_dir = archive_root / f"{taken_at.year:04d}" / f"{taken_at.month:02d}"
        ts = taken_at.strftime("%Y-%m-%d_%H-%M-%S")
    else:
        date_dir = archive_root / "unknown"
        ts = "0000-00-00_00-00-00"

    stem = Path(original_name).stem
    filename = f"{ts}_{stem}_{short_hex(chash)}{ext.lower()}"
    return date_dir / filename


# ── Plan I/O ─────────────────────────────────────────────────────────────────

def write_plan(plan: list[PlanEntry], plan_file: Path) -> None:
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    with plan_file.open("w", encoding="utf-8") as f:
        for entry in plan:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


def read_plan(plan_file: Path) -> list[PlanEntry]:
    entries = []
    with plan_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(PlanEntry.from_dict(json.loads(line)))
    return entries


# ── Apply ─────────────────────────────────────────────────────────────────────

class ApplyResult:
    def __init__(self) -> None:
        self.total = 0
        self.copied = 0
        self.already_done = 0
        self.skipped = 0       # skip=true in plan
        self.conflict = 0
        self.missing_source = 0
        self.error = 0
        self.conflicts: list[str] = []


def apply_plan(
    plan_file: Path,
    mount_paths: dict[str, Path],
    session_factory: sessionmaker,
    dry_run: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> ApplyResult:
    entries = read_plan(plan_file)
    result = ApplyResult()
    result.total = len(entries)

    for i, entry in enumerate(entries):
        if progress_cb:
            progress_cb(i, result.total, entry.target_path)

        # Respect skip flag set by human/AI
        if entry.skip:
            result.skipped += 1
            continue

        target = Path(entry.target_path)
        mount = mount_paths.get(entry.device_id)
        if mount is None:
            result.error += 1
            continue

        source = mount / entry.source_path
        if not source.exists():
            result.missing_source += 1
            continue

        # Idempotency: target already exists
        if target.exists():
            if entry.content_hash:
                actual = compute_content_hash(target)
                if actual == entry.content_hash:
                    if not dry_run:
                        _mark_archived(entry.asset_id, session_factory)
                    result.already_done += 1
                else:
                    result.conflict += 1
                    result.conflicts.append(entry.target_path)
            else:
                result.already_done += 1
            continue

        if dry_run:
            result.copied += 1
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

            if entry.content_hash:
                actual = compute_content_hash(target)
                if actual != entry.content_hash:
                    target.unlink(missing_ok=True)
                    result.error += 1
                    continue

            _mark_archived(entry.asset_id, session_factory)
            result.copied += 1
        except OSError:
            result.error += 1

    if progress_cb:
        progress_cb(result.total, result.total, "")

    return result


def _mark_archived(asset_id: str, session_factory) -> None:
    with open_session(session_factory) as s:
        MediaAssetRepo(s).update_archive_status(asset_id, ArchiveStatus.archived)
