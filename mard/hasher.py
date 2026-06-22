from __future__ import annotations

from pathlib import Path

try:
    import blake3 as _blake3
    def _new_hasher():
        return _blake3.blake3()
    HASH_ALGO = "blake3"
except ImportError:
    import hashlib
    def _new_hasher():
        return hashlib.sha256()
    HASH_ALGO = "sha256"

_THRESHOLD = 8 * 1024 * 1024   # 8 MB: files below this get full hash
_CHUNK = 4 * 1024 * 1024        # 4 MB head + 4 MB tail for large files
_READ_BUF = 65_536              # streaming read buffer


def quick_hash(path: Path, size: int) -> tuple[str, bool]:
    """Compute quick fingerprint.

    Returns (hex_digest, is_full_hash).
    is_full_hash=True means quick_hash == content_hash (file < threshold).
    """
    if size <= _THRESHOLD:
        return _hash_full(path), True
    return _hash_head_tail(path), False


def content_hash(path: Path) -> str:
    return _hash_full(path)


def short_hex(hex_digest: str, length: int = 8) -> str:
    return hex_digest[:length]


def _hash_full(path: Path) -> str:
    h = _new_hasher()
    with path.open("rb") as f:
        while buf := f.read(_READ_BUF):
            h.update(buf)
    return h.hexdigest()


def _hash_head_tail(path: Path) -> str:
    h = _new_hasher()
    with path.open("rb") as f:
        h.update(f.read(_CHUNK))
        f.seek(-_CHUNK, 2)
        h.update(f.read(_CHUNK))
    return h.hexdigest()
