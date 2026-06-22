from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import (
    DuplicateGroup, DuplicateGroupMember, DuplicateType,
    FileInstance, MediaAsset, ReviewStatus, ScanSession,
    ScanStatus, StorageDevice,
)


class DeviceRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_marker_id(self, marker_id: str) -> Optional[StorageDevice]:
        return self._s.scalar(
            select(StorageDevice).where(StorageDevice.device_marker_id == marker_id)
        )

    def save(self, device: StorageDevice) -> StorageDevice:
        self._s.add(device)
        self._s.flush()
        return device

    def touch(self, device_id: str, mount_hint: str) -> None:
        self._s.execute(
            update(StorageDevice)
            .where(StorageDevice.id == device_id)
            .values(last_seen_at=datetime.utcnow(), mount_hint=mount_hint)
        )


class ScanSessionRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(self, device_id: str) -> ScanSession:
        sess = ScanSession(device_id=device_id)
        self._s.add(sess)
        self._s.flush()
        return sess

    def update_progress(
        self, session_id: str, last_path: str, total: int, processed: int
    ) -> None:
        self._s.execute(
            update(ScanSession)
            .where(ScanSession.id == session_id)
            .values(last_scanned_path=last_path, total_files=total, processed_files=processed)
        )

    def complete(self, session_id: str, total: int) -> None:
        self._s.execute(
            update(ScanSession)
            .where(ScanSession.id == session_id)
            .values(
                status=ScanStatus.completed,
                finished_at=datetime.utcnow(),
                total_files=total,
                processed_files=total,
            )
        )

    def interrupt(self, session_id: str, processed: int) -> None:
        self._s.execute(
            update(ScanSession)
            .where(ScanSession.id == session_id)
            .values(status=ScanStatus.interrupted, processed_files=processed)
        )


class FileInstanceRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_device_path(self, device_id: str, path: str) -> Optional[FileInstance]:
        return self._s.scalar(
            select(FileInstance).where(
                FileInstance.device_id == device_id,
                FileInstance.path == path,
            )
        )

    def save(self, fi: FileInstance) -> FileInstance:
        self._s.add(fi)
        self._s.flush()
        return fi

    def update_hash(self, instance_id: str, quick_hash: str) -> None:
        self._s.execute(
            update(FileInstance)
            .where(FileInstance.id == instance_id)
            .values(quick_hash=quick_hash)
        )

    def update_content_hash(self, instance_id: str, content_hash: str) -> None:
        self._s.execute(
            update(FileInstance)
            .where(FileInstance.id == instance_id)
            .values(content_hash=content_hash)
        )

    def mark_media_asset(self, instance_id: str, asset_id: str) -> None:
        self._s.execute(
            update(FileInstance)
            .where(FileInstance.id == instance_id)
            .values(media_asset_id=asset_id)
        )

    def mark_missing_after_scan(self, device_id: str, session_id: str) -> int:
        """Mark all device files not visited in this session as gone."""
        result = self._s.execute(
            update(FileInstance)
            .where(
                FileInstance.device_id == device_id,
                FileInstance.exists == True,  # noqa: E712
                FileInstance.last_scan_session_id != session_id,
            )
            .values(exists=False)
        )
        return result.rowcount

    def get_quick_hash_collision_ids(self) -> list[tuple[str, int]]:
        """Return (quick_hash, size) pairs with more than one instance."""
        rows = self._s.execute(
            select(FileInstance.quick_hash, FileInstance.size)
            .where(FileInstance.quick_hash.is_not(None), FileInstance.exists == True)  # noqa: E712
            .group_by(FileInstance.quick_hash, FileInstance.size)
            .having(func.count(FileInstance.id) > 1)
        ).all()
        return [(r.quick_hash, r.size) for r in rows]

    def get_by_quick_hash_and_size(self, quick_hash: str, size: int) -> list[FileInstance]:
        return list(
            self._s.scalars(
                select(FileInstance).where(
                    FileInstance.quick_hash == quick_hash,
                    FileInstance.size == size,
                    FileInstance.exists == True,  # noqa: E712
                )
            )
        )

    def get_exact_duplicate_groups(self) -> list[list[FileInstance]]:
        """Return groups of FileInstances sharing the same content_hash (count > 1)."""
        hashes = self._s.execute(
            select(FileInstance.content_hash)
            .where(FileInstance.content_hash.is_not(None), FileInstance.exists == True)  # noqa: E712
            .group_by(FileInstance.content_hash)
            .having(func.count(FileInstance.id) > 1)
        ).scalars().all()

        groups = []
        for h in hashes:
            members = list(
                self._s.scalars(
                    select(FileInstance)
                    .where(FileInstance.content_hash == h, FileInstance.exists == True)  # noqa: E712
                    .order_by(FileInstance.mtime)
                )
            )
            groups.append(members)
        return groups


class MediaAssetRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_content_hash(self, content_hash: str) -> Optional[MediaAsset]:
        return self._s.scalar(
            select(MediaAsset).where(MediaAsset.content_hash == content_hash)
        )

    def save(self, asset: MediaAsset) -> MediaAsset:
        self._s.add(asset)
        self._s.flush()
        return asset


class DuplicateGroupRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_content_hash(self, content_hash: str) -> Optional[DuplicateGroup]:
        """Find an existing exact DuplicateGroup whose members share this content_hash."""
        member = self._s.scalar(
            select(DuplicateGroupMember)
            .join(FileInstance, FileInstance.id == DuplicateGroupMember.instance_id)
            .join(DuplicateGroup, DuplicateGroup.id == DuplicateGroupMember.group_id)
            .where(
                FileInstance.content_hash == content_hash,
                DuplicateGroup.duplicate_type == DuplicateType.exact,
            )
        )
        if member is None:
            return None
        return self._s.get(DuplicateGroup, member.group_id)

    def create_exact_group(
        self,
        instance_ids: list[str],
        recommended_keep_id: str,
        content_hash: str,
    ) -> DuplicateGroup:
        group = DuplicateGroup(
            duplicate_type=DuplicateType.exact,
            confidence=1.0,
            reason=f"content_hash:{content_hash[:16]}",
            review_status=ReviewStatus.pending,
            recommended_keep_instance_id=recommended_keep_id,
        )
        self._s.add(group)
        self._s.flush()
        for iid in instance_ids:
            self._s.add(DuplicateGroupMember(group_id=group.id, instance_id=iid))
        self._s.flush()
        return group

    def get_all_exact(self) -> list[DuplicateGroup]:
        return list(
            self._s.scalars(
                select(DuplicateGroup)
                .where(DuplicateGroup.duplicate_type == DuplicateType.exact)
                .order_by(DuplicateGroup.created_at.desc())
            )
        )

    def get_members(self, group_id: str) -> list[FileInstance]:
        return list(
            self._s.scalars(
                select(FileInstance)
                .join(DuplicateGroupMember, DuplicateGroupMember.instance_id == FileInstance.id)
                .where(DuplicateGroupMember.group_id == group_id)
            )
        )
