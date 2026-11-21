"""Compare editor saves against the bytes originally opened on disk."""

from hashlib import file_digest, sha256
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import NamedTemporaryFile


def snapshot_bytes(data):
    return sha256(data).digest()


def snapshot_file(path):
    try:
        with path.open("rb") as stream:
            return file_digest(stream, "sha256").digest()
    except FileNotFoundError:
        return None


def write_if_unchanged(path, data, expected):
    """Replace a file atomically if it still matches the version read."""
    if snapshot_file(path) != expected:
        raise OSError("Файл изменён на диске. Перечитайте его перед записью.")
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        if expected is not None and os.name != "nt":
            temporary.chmod(S_IMODE(path.stat().st_mode))
        if snapshot_file(path) != expected:
            raise OSError("Файл изменился во время записи.")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
