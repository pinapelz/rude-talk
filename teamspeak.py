import base64
import getpass
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from constants import TS_SESSION_FILE

load_dotenv()

MYTS_AUTH_URL = "https://clientapi.myteamspeak.com/authentication"
MYTS_USER_URL = "https://clientapi.myteamspeak.com/user"
MYTS_CONTENT_TYPE = "application/ts3cloud"
GCS_SIGNED_CONTENT_TYPE = "text/plain"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

DEFAULT_SESSION_PATH = Path(TS_SESSION_FILE)
LEGACY_SESSION_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "ts6-matrix-poc"
    / "session.json"
)

LOGIN_ERRORS: dict[int, str] = {
    0: "OK_OR_UNSET",
    100: "ERROR_FE_UNKNOWN_METHOD",
    101: "ERROR_FE_UNKNOWN_DATA",
    102: "ERROR_SESSION_OK",
    103: "ERROR_SESSION_EXPIRED",
    104: "ERROR_SESSION_SERVER_OFFLINE",
    200: "ERROR_LOGIN_OK",
    201: "ERROR_LOGIN_OFFLINE",
    202: "ERROR_LOGIN_FAILED (check email/password)",
    203: "ERROR_LOGIN_EMAIL_PENDING",
    206: "ERROR_AUTH_TOKEN_INVALID",
    207: "ERROR_RENEWAL_TOKEN_INVALID",
    208: "ERROR_AUTH_REQUIRES_OTP (2FA code required)",
    209: "ERROR_2FA_OTP_INVALID",
    210: "ERROR_2FA_OTP_SECRET_MISSING",
    211: "ERROR_2FA_DEVICE_ID_MISSING",
    212: "ERROR_2FA_RECOVERY_KEY_INVALID",
}

FILE_ERRORS: dict[int, str] = {
    0: "NONE",
    100: "ERROR_MISSING_REQUIRED_FIELD",
    103: "ERROR_DATABASE",
    104: "ERROR_LOGIN_FAILED",
    107: "ERROR_NOT_ALLOWED (account must be a member of this TS6 chat room)",
    109: "ERROR_SESSION_EXPIRED",
    110: "ERROR_CODE_INVALID",
    116: "ERROR_INTERNAL",
    117: "ERROR_QUOTA_EXCEEDED",
}

def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def pb_varint(field_num: int, value: int) -> bytes:
    if not value:
        return b""
    return _encode_varint((field_num << 3) | 0) + _encode_varint(value)


def pb_bytes(field_num: int, data: bytes | str) -> bytes:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not raw:
        return b""
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(raw)) + raw


def pb_decode(buf: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    idx = 0
    n = len(buf)
    while idx < n:
        tag = 0
        shift = 0
        while True:
            if idx >= n:
                raise ValueError("Truncated Protobuf tag")
            b = buf[idx]
            idx += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:
            val = 0
            shift = 0
            while True:
                if idx >= n:
                    raise ValueError("Truncated Protobuf varint")
                b = buf[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            fields.setdefault(field_num, []).append(buf[idx : idx + 8])
            idx += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            while True:
                if idx >= n:
                    raise ValueError("Truncated Protobuf length")
                b = buf[idx]
                idx += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.setdefault(field_num, []).append(buf[idx : idx + length])
            idx += length
        elif wire_type == 5:
            fields.setdefault(field_num, []).append(buf[idx : idx + 4])
            idx += 4
        else:
            raise ValueError(f"Unsupported Protobuf wire type: {wire_type}")
    return fields


def pb_get_int(decoded: dict[int, list[Any]], field_num: int, default: int = 0) -> int:
    vals = decoded.get(field_num)
    return int(vals[-1]) if vals else default


def pb_get_str(decoded: dict[int, list[Any]], field_num: int, default: str = "") -> str:
    vals = decoded.get(field_num)
    if not vals:
        return default
    raw = vals[-1]
    return raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)


def pb_get_bytes(decoded: dict[int, list[Any]], field_num: int) -> bytes:
    vals = decoded.get(field_num)
    if not vals:
        return b""
    raw = vals[-1]
    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else b""


def derive_myts_password_hash(email: str, password: str) -> str:
    salt = f"{email.strip().lower()}ts3Login{password}".encode("utf-8")
    dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 10000, dklen=48)
    return base64.b64encode(dk).decode("ascii")


def build_rpc_envelope(method_name: str, protobuf_payload: bytes) -> bytes:
    method_bytes = method_name.encode("ascii")
    if len(method_bytes) > 127:
        raise ValueError("RPC method name exceeds 127 bytes")
    return bytes([len(method_bytes)]) + method_bytes + protobuf_payload


def call_myts_rpc(endpoint_url: str, method_name: str, protobuf_payload: bytes) -> dict[int, list[Any]]:
    body = build_rpc_envelope(method_name, protobuf_payload)
    req = urllib.request.Request(
        endpoint_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": MYTS_CONTENT_TYPE,
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} from {endpoint_url} ({method_name}): {err_body[:200]}"
        ) from exc
    return pb_decode(resp_bytes)


def load_cached_state(path: Path = DEFAULT_SESSION_PATH) -> dict[str, str]:
    for candidate in (path, LEGACY_SESSION_PATH):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def save_cached_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def renew_myts_session(renewal_token: str, auth_token: str = "") -> dict[str, str] | None:
    payload = pb_bytes(1, renewal_token) + pb_bytes(2, auth_token)
    resp = call_myts_rpc(MYTS_AUTH_URL, "loginWithRenewalToken", payload)
    err = pb_get_int(resp, 5, 0)
    session = pb_get_str(resp, 2)
    if session and err in (0, 200):
        out = {"session": session}
        alt_raw = pb_get_bytes(resp, 16)
        if alt_raw:
            alt = pb_decode(alt_raw)
            if pb_get_str(alt, 1):
                out["renewal_token"] = pb_get_str(alt, 1)
            if pb_get_str(alt, 2):
                out["otp_renewal_token"] = pb_get_str(alt, 2)
            if pb_get_str(alt, 3):
                out["device_id"] = pb_get_str(alt, 3)
        return out
    return None


def login_myts_headless(
    email: str,
    password: str,
    otp: str = "",
    device_id: str = "",
    otp_renewal_token: str = "",
    device_name: str = "Desktop",
    interactive: bool = False,
) -> dict[str, str]:
    if not device_id:
        device_id = uuid.uuid4().hex

    pw_hash_b64 = derive_myts_password_hash(email, password)

    def _send_login(otp_code: str, cur_device_id: str) -> dict[int, list[Any]]:
        login_pb = (
            pb_bytes(1, email.strip())
            + pb_bytes(2, pw_hash_b64)
            + pb_varint(5, 1)
            + pb_bytes(6, otp_code.strip())
            + pb_bytes(7, cur_device_id)
            + pb_bytes(8, otp_renewal_token)
            + pb_bytes(9, device_name)
        )
        return call_myts_rpc(MYTS_AUTH_URL, "login", login_pb)

    resp = _send_login(otp, device_id)
    err = pb_get_int(resp, 5, 0)

    attempts = 0
    while err in (208, 209) and interactive and sys.stdin.isatty() and attempts < 3:
        attempts += 1
        alt_raw = pb_get_bytes(resp, 16)
        if alt_raw:
            alt = pb_decode(alt_raw)
            if pb_get_str(alt, 3):
                device_id = pb_get_str(alt, 3)
        if err == 209:
            print("[!] Invalid 2FA OTP code, please try again.", flush=True)
        sys.stdout.write("Enter 6-digit myTeamSpeak 2FA OTP code: ")
        sys.stdout.flush()
        otp_prompted = sys.stdin.readline().strip()
        if not otp_prompted:
            break
        resp = _send_login(otp_prompted, device_id)
        err = pb_get_int(resp, 5, 0)

    session = pb_get_str(resp, 2)
    if not session or err not in (0, 200):
        err_desc = LOGIN_ERRORS.get(err, f"UNKNOWN_LOGIN_ERROR_{err}")
        if err == 208 and not interactive:
            raise RuntimeError(
                "myTeamSpeak 2FA is required. Run 'uv run ts_login.py' first to generate ts_session.json."
            )
        raise RuntimeError(f"myTeamSpeak login failed: {err_desc} (code={err})")

    result: dict[str, str] = {
        "email": email.strip(),
        "session": session,
        "username": pb_get_str(resp, 8),
        "uuid": pb_get_str(resp, 4),
        "device_id": device_id,
    }
    alt_raw = pb_get_bytes(resp, 16)
    if alt_raw:
        alt = pb_decode(alt_raw)
        if pb_get_str(alt, 1):
            result["renewal_token"] = pb_get_str(alt, 1)
        if pb_get_str(alt, 2):
            result["otp_renewal_token"] = pb_get_str(alt, 2)
        if pb_get_str(alt, 3):
            result["device_id"] = pb_get_str(alt, 3)
    return result


def get_active_session(
    cache_path: Path = DEFAULT_SESSION_PATH,
    force_refresh: bool = False,
    interactive: bool = False,
) -> str:
    env_session = os.environ.get("TS_SESSION", "").strip()
    if env_session and not force_refresh:
        return env_session

    cached = load_cached_state(cache_path)
    if not force_refresh and cached.get("session"):
        return cached["session"]

    if cached.get("renewal_token"):
        renewed = renew_myts_session(cached["renewal_token"], cached.get("auth_token", ""))
        if renewed and renewed.get("session"):
            cached.update(renewed)
            save_cached_state(cache_path, cached)
            return cached["session"]

    email = os.environ.get("TS_EMAIL") or cached.get("email") or ""
    password = os.environ.get("TS_PASSWORD") or os.environ.get("PASSWORD") or ""
    otp = os.environ.get("TS_OTP") or ""

    if interactive and sys.stdin.isatty():
        if not email:
            sys.stdout.write("myTeamSpeak Email: ")
            sys.stdout.flush()
            email = sys.stdin.readline().strip()
        if not password:
            password = getpass.getpass("myTeamSpeak Password: ")

    if not email or not password:
        raise RuntimeError(
            "No cached myTeamSpeak session found. Run 'uv run ts_login.py' first or set TS_EMAIL/TS_PASSWORD in .env."
        )

    fresh = login_myts_headless(
        email=email,
        password=password,
        otp=otp,
        device_id=cached.get("device_id", ""),
        otp_renewal_token=cached.get("otp_renewal_token", ""),
        interactive=interactive,
    )
    cached.update(fresh)
    save_cached_state(cache_path, cached)
    return fresh["session"]


def request_download_signed_url(
    session: str, user_id: str, room_id: str, file_id: str
) -> tuple[str, int]:
    req_pb = (
        pb_bytes(1, session)
        + pb_bytes(2, user_id)
        + pb_bytes(3, room_id)
        + pb_bytes(4, file_id)
    )
    resp = call_myts_rpc(MYTS_USER_URL, "requestDownloadFile", req_pb)
    success = bool(pb_get_int(resp, 1, 0))
    error_code = pb_get_int(resp, 2, 0)
    signed_url = pb_get_str(resp, 6)
    if success and signed_url:
        return signed_url, 0
    return "", error_code or -1


def download_raw_attachment(signed_url: str) -> bytes:
    req = urllib.request.Request(
        signed_url,
        method="GET",
        headers={
            "Content-Type": GCS_SIGNED_CONTENT_TYPE,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_ts_file(
    sender: str,
    room_id: str,
    file_id: str,
    cache_path: Path = DEFAULT_SESSION_PATH,
) -> bytes:
    user_id = sender.lstrip("@").split(":", 1)[0]
    session = get_active_session(cache_path=cache_path)

    signed_url, err_code = request_download_signed_url(
        session=session,
        user_id=user_id,
        room_id=room_id,
        file_id=file_id,
    )

    if err_code == 109:
        session = get_active_session(cache_path=cache_path, force_refresh=True)
        signed_url, err_code = request_download_signed_url(
            session=session,
            user_id=user_id,
            room_id=room_id,
            file_id=file_id,
        )

    if not signed_url:
        err_name = FILE_ERRORS.get(err_code, f"UNKNOWN_FILE_ERROR_{err_code}")
        raise RuntimeError(f"requestDownloadFile failed: {err_name} (code={err_code})")

    return download_raw_attachment(signed_url)
