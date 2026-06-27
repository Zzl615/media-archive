from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from .db.database import open_session
from .db.repository import DuplicateGroupRepo, FileInstanceRepo

QUARANTINE_DIR = ".media-archive-quarantine"


class QuarantineResult:
    def __init__(self) -> None:
        self.total_groups = 0
        self.moved = 0
        self.already_gone = 0      # exists=False, skip
        self.skipped_no_keep = 0   # keep file missing, unsafe to quarantine
        self.skipped_only_copy = 0 # would leave zero copies on device
        self.error = 0
        self.moved_paths: list[tuple[str, str]] = []  # (src_rel, dst_rel)


def run_quarantine(
    mount_path: Path,
    device_id: str,
    session_factory: sessionmaker,
    dry_run: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> QuarantineResult:
    result = QuarantineResult()

    with open_session(session_factory) as s:
        groups = DuplicateGroupRepo(s).get_all_exact()

    result.total_groups = len(groups)

    for i, group in enumerate(groups):
        if progress_cb:
            progress_cb(i, result.total_groups, "")

        with open_session(session_factory) as s:
            dup_repo = DuplicateGroupRepo(s)
            fi_repo = FileInstanceRepo(s)

            members = dup_repo.get_members(group.id)
            keep_id = group.recommended_keep_instance_id

            # Safety: verify the keep file exists somewhere
            keep_fi = next((m for m in members if m.id == keep_id), None)
            if keep_fi is None or not keep_fi.exists:
                result.skipped_no_keep += 1
                continue

            # Only quarantine existing non-keep copies on this device
            to_move = [
                m for m in members
                if m.id != keep_id
                and m.device_id == device_id
                and m.exists
            ]

            if not to_move:
                continue

            # Safety: don't quarantine if this would leave no accessible copy
            # (keep is on another device and all device copies would be removed)
            keep_on_device = keep_fi.device_id == device_id
            other_existing = [
                m for m in members
                if m.id != keep_id and m.exists and m not in to_move
            ]
            if not keep_on_device and not other_existing:
                result.skipped_only_copy += 1
                continue

            for fi in to_move:
                src = mount_path / fi.path
                dst = mount_path / QUARANTINE_DIR / fi.path
                dst_rel = str(Path(QUARANTINE_DIR) / fi.path)

                if not src.exists():
                    result.already_gone += 1
                    if not dry_run:
                        fi_repo.set_exists(fi.id, False)
                    continue

                if dry_run:
                    result.moved += 1
                    result.moved_paths.append((fi.path, dst_rel))
                    continue

                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    fi_repo.set_exists(fi.id, False)
                    result.moved += 1
                    result.moved_paths.append((fi.path, dst_rel))
                except OSError:
                    result.error += 1

    if progress_cb:
        progress_cb(result.total_groups, result.total_groups, "")

    return result
