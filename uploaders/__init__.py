import os
import time
from collections.abc import Callable

from constants import UPLOAD_DESTINATIONS
from uploaders.imgbb import upload_to_imgbb
from uploaders.nextcloud import upload_to_nextcloud

UPLOADERS: dict[str, Callable[[bytes, str], str]] = {
    "imgbb": upload_to_imgbb,
    "nextcloud": upload_to_nextcloud,
}


def _unique_filename(filename: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(filename))
    return f"{stem}_{int(time.time())}{ext}"


def upload_to_destination(data: bytes, filename: str, destination: str) -> str:
    uploader = UPLOADERS.get(destination.lower())
    if not uploader:
        raise ValueError(
            f"Unknown upload destination '{destination}'. Available: {list(UPLOADERS)}"
        )
    return uploader(data, filename)


def upload_file(
    data: bytes,
    filename: str,
    destinations: list[str] | None = None,
) -> dict[str, str]:
    targets = destinations if destinations is not None else UPLOAD_DESTINATIONS
    unique_name = _unique_filename(filename)
    last_error: Exception | None = None

    for dest in targets:
        try:
            url = upload_to_destination(data, unique_name, dest)
            return {dest: url}
        except Exception as e:
            print(f"[Upload] '{dest}' skipped/failed for '{filename}': {e}")
            last_error = e

    if last_error:
        raise last_error
    return {}
