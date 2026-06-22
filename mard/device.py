from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Optional


MARKER_FILE = ".media-archive-device.json"

_SKIP_DIRS = {
    ".Trashes", ".Spotlight-V100", ".fseventsd",
    "System Volume Information", "$RECYCLE.BIN", ".media-archive-quarantine",
}

MEDIA_EXTENSIONS = frozenset({
    # Photos
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".heic", ".heif",
    # RAW
    ".raw", ".arw", ".nef", ".cr2", ".cr3", ".orf",
    ".rw2", ".dng", ".raf", ".pef", ".srw", ".x3f", ".3fr",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
    ".flv", ".webm", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
})


def media_type_of(ext: str) -> str:
    ext = ext.lower()
    if ext in {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
        ".heic", ".heif", ".raw", ".arw", ".nef", ".cr2", ".cr3", ".orf",
        ".rw2", ".dng", ".raf", ".pef", ".srw", ".x3f", ".3fr",
    }:
        return "photo"
    if ext in {
        ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
        ".flv", ".webm", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
    }:
        return "video"
    return "other"


def identify_device(mount_path: Path) -> dict:
    """Read or create the device marker file at mount_path root.

    Returns a dict with keys: device_marker_id, volume_label, filesystem_uuid.
    """
    marker_path = mount_path / MARKER_FILE

    if marker_path.exists():
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        if "device_id" not in data:
            raise ValueError(f"Marker file at {marker_path} is missing 'device_id'.")
        return {
            "device_marker_id": data["device_id"],
            "volume_label": data.get("name", mount_path.name),
            "filesystem_uuid": _get_filesystem_uuid(mount_path),
        }

    device_id = str(uuid.uuid4())
    data = {
        "device_id": device_id,
        "name": mount_path.name,
        "created_at": _utcnow_iso(),
    }
    marker_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "device_marker_id": device_id,
        "volume_label": mount_path.name,
        "filesystem_uuid": _get_filesystem_uuid(mount_path),
    }


def is_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".")


def _get_filesystem_uuid(mount_path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["diskutil", "info", str(mount_path)],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "Volume UUID" in line:
                return line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return None


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
