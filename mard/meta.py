from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from .db.database import open_session
from .db.models import MediaAsset
from .db.repository import FileInstanceRepo, MediaAssetRepo
from .device import media_type_of
from .hasher import content_hash as compute_content_hash

_BATCH = 50

_DT_FORMATS = [
    "%Y:%m:%d %H:%M:%S%z",
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
]

# Common filename timestamp patterns: IMG20250510_190339, VID_20250510_190339, etc.
_FNAME_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[_T-]?(\d{2})(\d{2})(\d{2})")


class MetaResult:
    def __init__(self) -> None:
        self.fullhash_total = 0
        self.fullhash_done = 0
        self.assets_created = 0
        self.exif_processed = 0
        self.exif_updated = 0


def run_meta(
    mount_path: Path,
    device_id: str,
    session_factory: sessionmaker,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> MetaResult:
    result = MetaResult()

    # ── Phase A: compute content_hash for files that only have quick_hash ────
    _phase_fullhash(mount_path, device_id, session_factory, result, progress_cb)

    # ── Phase B: create MediaAssets for instances with content_hash but none ─
    _phase_ensure_assets(device_id, session_factory, result)

    # ── Phase C: EXIF extraction and MediaAsset enrichment ───────────────────
    _phase_exif(mount_path, device_id, session_factory, result, progress_cb)

    return result


# ── Phase A ──────────────────────────────────────────────────────────────────

def _phase_fullhash(
    mount_path: Path,
    device_id: str,
    session_factory: sessionmaker,
    result: MetaResult,
    progress_cb,
) -> None:
    with open_session(session_factory) as s:
        instances = FileInstanceRepo(s).get_without_content_hash(device_id)

    result.fullhash_total = len(instances)
    for i, fi in enumerate(instances):
        if progress_cb:
            progress_cb("fullhash", i, result.fullhash_total)
        path = mount_path / fi.path
        if not path.exists():
            continue
        try:
            chash = compute_content_hash(path)
        except OSError:
            continue
        with open_session(session_factory) as s:
            FileInstanceRepo(s).update_content_hash(fi.id, chash)
        result.fullhash_done += 1

    if progress_cb:
        progress_cb("fullhash", result.fullhash_total, result.fullhash_total)


# ── Phase B ──────────────────────────────────────────────────────────────────

def _phase_ensure_assets(
    device_id: str,
    session_factory: sessionmaker,
    result: MetaResult,
) -> None:
    with open_session(session_factory) as s:
        fi_repo = FileInstanceRepo(s)
        asset_repo = MediaAssetRepo(s)
        instances = fi_repo.get_without_asset(device_id)

        for fi in instances:
            if fi.content_hash is None:
                continue
            asset = asset_repo.get_by_content_hash(fi.content_hash)
            if asset is None:
                asset = MediaAsset(
                    content_hash=fi.content_hash,
                    size=fi.size,
                    media_type=media_type_of(fi.extension or ""),
                )
                asset_repo.save(asset)
                result.assets_created += 1
            fi_repo.set_media_asset(fi.id, asset.id)


# ── Phase C ──────────────────────────────────────────────────────────────────

def _phase_exif(
    mount_path: Path,
    device_id: str,
    session_factory: sessionmaker,
    result: MetaResult,
    progress_cb,
) -> None:
    with open_session(session_factory) as s:
        instances = FileInstanceRepo(s).get_without_meta(device_id)

    total = len(instances)
    result.exif_processed = total

    for batch_start in range(0, total, _BATCH):
        batch = instances[batch_start : batch_start + _BATCH]
        if progress_cb:
            progress_cb("exif", batch_start, total)

        existing_paths = [mount_path / fi.path for fi in batch if (mount_path / fi.path).exists()]
        path_to_fi = {str(mount_path / fi.path): fi for fi in batch}
        records = _exiftool_json(existing_paths)

        with open_session(session_factory) as s:
            asset_repo = MediaAssetRepo(s)
            for rec in records:
                fi = path_to_fi.get(rec.get("SourceFile", ""))
                if fi is None or fi.media_asset_id is None:
                    continue
                asset = asset_repo.get_by_id(fi.media_asset_id)
                if asset is None:
                    continue

                taken_at, src = _pick_taken_at(rec, fi)
                w, h = _pick_dimensions(rec)

                asset_repo.update_meta(
                    asset_id=asset.id,
                    taken_at=taken_at,
                    taken_at_source=src,
                    camera_model=_str_or_none(rec.get("Model")),
                    width=w,
                    height=h,
                    gps_lat=_float_or_none(rec.get("GPSLatitude")),
                    gps_lng=_float_or_none(rec.get("GPSLongitude")),
                    duration=_float_or_none(rec.get("Duration")),
                )
                result.exif_updated += 1

    if progress_cb:
        progress_cb("exif", total, total)


# ── ExifTool helpers ─────────────────────────────────────────────────────────

def _exiftool_json(paths: list[Path]) -> list[dict]:
    if not paths:
        return []
    cmd = [
        "exiftool", "-json", "-charset", "utf8", "-n",
        "-DateTimeOriginal", "-CreateDate", "-TrackCreateDate", "-MediaCreateDate",
        "-Model",
        "-ImageWidth", "-ImageHeight",
        "-VideoFrameWidth", "-VideoFrameHeight",
        "-GPSLatitude", "-GPSLongitude",
        "-Duration",
        "--", *[str(p) for p in paths],
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        raw = r.stdout.strip()
        return json.loads(raw) if raw else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


def _parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def _parse_filename_dt(filename: str) -> Optional[datetime]:
    m = _FNAME_RE.search(filename)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def _pick_taken_at(rec: dict, fi) -> tuple[Optional[datetime], str]:
    dt = _parse_dt(rec.get("DateTimeOriginal"))
    if dt:
        return dt, "exif"

    for key in ("CreateDate", "TrackCreateDate", "MediaCreateDate"):
        dt = _parse_dt(rec.get(key))
        if dt:
            return dt, "quicktime"

    dt = _parse_filename_dt(fi.file_name)
    if dt:
        return dt, "filename"

    if fi.mtime:
        return fi.mtime, "mtime"
    if fi.ctime:
        return fi.ctime, "ctime"

    return None, "unknown"


def _pick_dimensions(rec: dict) -> tuple[Optional[int], Optional[int]]:
    w = rec.get("ImageWidth") or rec.get("VideoFrameWidth")
    h = rec.get("ImageHeight") or rec.get("VideoFrameHeight")
    return _int_or_none(w), _int_or_none(h)


def _str_or_none(v) -> Optional[str]:
    return str(v).strip() or None if v else None


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
