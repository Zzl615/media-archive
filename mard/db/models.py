from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Enum as SAEnum, Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class ScanStatus(str, Enum):
    running = "running"
    completed = "completed"
    interrupted = "interrupted"


class DuplicateType(str, Enum):
    exact = "exact"
    likely = "likely"
    similar = "similar"


class ReviewStatus(str, Enum):
    pending = "pending"
    reviewed = "reviewed"
    resolved = "resolved"


class ArchiveStatus(str, Enum):
    pending = "pending"
    archived = "archived"
    skipped = "skipped"


class StorageDevice(Base):
    __tablename__ = "storage_devices"

    id = Column(String, primary_key=True, default=_new_id)
    volume_label = Column(String)
    filesystem_uuid = Column(String)
    device_marker_id = Column(String, unique=True, nullable=False)
    mount_hint = Column(String)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(String)

    scan_sessions = relationship("ScanSession", back_populates="device")
    file_instances = relationship("FileInstance", back_populates="device")


class ScanSession(Base):
    __tablename__ = "scan_sessions"

    id = Column(String, primary_key=True, default=_new_id)
    device_id = Column(String, ForeignKey("storage_devices.id"), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(SAEnum(ScanStatus), default=ScanStatus.running)
    last_scanned_path = Column(String, nullable=True)
    total_files = Column(Integer, default=0)
    processed_files = Column(Integer, default=0)

    device = relationship("StorageDevice", back_populates="scan_sessions")


class FileInstance(Base):
    __tablename__ = "file_instances"

    id = Column(String, primary_key=True, default=_new_id)
    device_id = Column(String, ForeignKey("storage_devices.id"), nullable=False)
    # path relative to device root, always stored with forward slashes
    path = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    extension = Column(String)
    size = Column(Integer, nullable=False)
    mtime = Column(DateTime)
    ctime = Column(DateTime)
    # inode is None on FAT32/exFAT; treated as advisory only
    inode_or_file_id = Column(String, nullable=True)
    quick_hash = Column(String, nullable=True)
    # None until content_hash phase runs
    content_hash = Column(String, nullable=True)
    media_asset_id = Column(String, ForeignKey("media_assets.id"), nullable=True)
    scan_at = Column(DateTime, default=datetime.utcnow)
    last_scan_session_id = Column(String, ForeignKey("scan_sessions.id"), nullable=True)
    exists = Column(Boolean, default=True)

    device = relationship("StorageDevice", back_populates="file_instances")
    media_asset = relationship("MediaAsset", back_populates="file_instances")

    __table_args__ = (
        Index("ix_fi_device_path", "device_id", "path", unique=True),
        Index("ix_fi_quick_hash", "quick_hash"),
        Index("ix_fi_content_hash", "content_hash"),
        Index("ix_fi_session", "last_scan_session_id"),
        Index("ix_fi_exists", "exists"),
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(String, primary_key=True, default=_new_id)
    content_hash = Column(String, unique=True, nullable=False)
    size = Column(Integer, nullable=False)
    media_type = Column(String)            # photo / video / other
    taken_at = Column(DateTime, nullable=True)
    taken_at_source = Column(String, nullable=True)
    duration = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    camera_model = Column(String, nullable=True)
    gps_lat = Column(Float, nullable=True)
    gps_lng = Column(Float, nullable=True)
    perceptual_hash = Column(String, nullable=True)
    # string FK only — avoids circular ORM relationship; managed at app level
    best_instance_id = Column(String, nullable=True)
    archive_status = Column(SAEnum(ArchiveStatus), default=ArchiveStatus.pending)

    file_instances = relationship("FileInstance", back_populates="media_asset")

    __table_args__ = (
        Index("ix_ma_content_hash", "content_hash", unique=True),
        Index("ix_ma_taken_at", "taken_at"),
        Index("ix_ma_archive_status", "archive_status"),
    )


class DuplicateGroupMember(Base):
    __tablename__ = "duplicate_group_members"

    group_id = Column(String, ForeignKey("duplicate_groups.id"), primary_key=True)
    instance_id = Column(String, ForeignKey("file_instances.id"), primary_key=True)


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    id = Column(String, primary_key=True, default=_new_id)
    duplicate_type = Column(SAEnum(DuplicateType), nullable=False)
    confidence = Column(Float, default=1.0)
    reason = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    review_status = Column(SAEnum(ReviewStatus), default=ReviewStatus.pending)
    # string FK only — same rationale as best_instance_id above
    recommended_keep_instance_id = Column(String, nullable=True)
