import base64
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

from dotenv import load_dotenv

from constants import NEXTCLOUD_REMOTE_PATH

load_dotenv()


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    method: str,
    url: str,
    username: str,
    password: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    req_headers = {"Authorization": _basic_auth_header(username, password)}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def upload_to_nextcloud(
    data: bytes | str | Path,
    filename: str | None = None,
    remote_path: str | None = None,
    nextcloud_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    nextcloud_url = (nextcloud_url or os.environ["NEXTCLOUD_URL"]).rstrip("/")
    username = username or os.environ["NEXTCLOUD_USERNAME"]
    password = password or os.environ["NEXTCLOUD_PASSWORD"]
    remote_path = (
        remote_path
        if remote_path is not None
        else os.environ.get("NEXTCLOUD_REMOTE_PATH", NEXTCLOUD_REMOTE_PATH)
    ).strip("/")

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

    dav_base = f"{nextcloud_url}/remote.php/dav/files/{quote(username, safe='')}"
    current_path = dav_base

    for directory in [d for d in remote_path.split("/") if d]:
        current_path += f"/{quote(directory)}"
        status_code, resp_text = _request("MKCOL", current_path, username, password)

        # 201 = created, 405 = already exists
        if status_code not in (201, 405):
            raise RuntimeError(
                f"Failed to create directory '{directory}': {status_code} {resp_text}"
            )

    encoded_remote = "/".join(quote(p) for p in remote_path.split("/") if p)
    upload_url = (
        f"{dav_base}/{encoded_remote}/{quote(filename)}"
        if encoded_remote
        else f"{dav_base}/{quote(filename)}"
    )

    status_code, resp_text = _request(
        "PUT",
        upload_url,
        username,
        password,
        data=file_bytes,
        headers={"Content-Type": "application/octet-stream"},
    )

    if status_code not in (200, 201, 204):
        raise RuntimeError(f"Failed to upload file: {status_code} {resp_text}")

    share_api = f"{nextcloud_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    share_path = f"/{remote_path}/{filename}" if remote_path else f"/{filename}"
    form_data = urllib.parse.urlencode(
        {
            "path": share_path,
            "shareType": "3",  # Public link
            "permissions": "1",  # Read-only
        }
    ).encode("utf-8")

    status_code, resp_text = _request(
        "POST",
        share_api,
        username,
        password,
        data=form_data,
        headers={
            "OCS-APIRequest": "true",
            "Accept": "application/xml",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    if not (200 <= status_code < 300):
        raise RuntimeError(f"Failed to create share: {status_code} {resp_text}")

    try:
        root = ElementTree.fromstring(resp_text)
        url_element = root.find(".//url")

        if url_element is None or not url_element.text:
            raise RuntimeError(
                f"Share was created but no URL was returned: {resp_text}"
            )

        return url_element.text.rstrip("/")

    except ElementTree.ParseError as e:
        raise RuntimeError(f"Invalid response from Nextcloud: {resp_text}") from e
