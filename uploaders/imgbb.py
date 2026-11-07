import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"
IMGBB_MAX_BYTES = 32 * 1024 * 1024  # 32 MB


def _is_image_or_pdf(file_bytes: bytes, filename: str | None = None) -> bool:
    if filename:
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type and (
            mime_type.startswith("image/") or mime_type == "application/pdf"
        ):
            return True

    return (
        file_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        or file_bytes.startswith(b"\xff\xd8\xff")
        or file_bytes.startswith((b"GIF87a", b"GIF89a"))
        or (file_bytes.startswith(b"RIFF") and file_bytes[8:12] == b"WEBP")
        or file_bytes.startswith(b"BM")
        or file_bytes.startswith(b"%PDF-")
    )


def _request(
    method: str,
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def upload_to_imgbb(
    data: bytes | str | Path,
    filename: str | None = None,
    api_key: str | None = None,
    expiration: int | None = None,
) -> str:
    api_key = api_key or os.environ["IMGBB_API_KEY"]

    if isinstance(data, (str, Path)):
        file_path = Path(data)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        file_bytes = file_path.read_bytes()
        filename = filename or file_path.name
    else:
        file_bytes = data
        if not filename:
            raise ValueError("filename is required when uploading raw bytes")

    if len(file_bytes) > IMGBB_MAX_BYTES:
        raise ValueError(
            f"File size ({len(file_bytes)} bytes) exceeds ImgBB 32 MB limit"
        )
    if not _is_image_or_pdf(file_bytes, filename):
        raise ValueError(
            f"File '{filename}' is not a supported image or PDF for ImgBB"
        )

    payload: dict[str, str] = {
        "key": api_key,
        "image": base64.b64encode(file_bytes).decode("ascii"),
    }
    if filename:
        payload["name"] = Path(filename).stem
    if expiration is not None:
        payload["expiration"] = str(expiration)

    form_data = urllib.parse.urlencode(payload).encode("utf-8")

    status_code, resp_text = _request(
        "POST",
        IMGBB_UPLOAD_URL,
        data=form_data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    if not (200 <= status_code < 300):
        raise RuntimeError(f"Failed to upload file: {status_code} {resp_text}")

    try:
        resp_json = json.loads(resp_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid response from ImgBB: {resp_text}") from e

    data_obj = resp_json.get("data") if isinstance(resp_json, dict) else None
    url = data_obj.get("url") if isinstance(data_obj, dict) else None
    if not url:
        raise RuntimeError(f"Upload succeeded but no URL was returned: {resp_text}")

    return url
