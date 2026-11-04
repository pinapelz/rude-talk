import argparse
import getpass
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from teamspeak import (
    DEFAULT_SESSION_PATH,
    load_cached_state,
    login_myts_headless,
    save_cached_state,
)

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authenticate with myTeamSpeak (including 2FA) and save session token to ts_session.json."
    )
    parser.add_argument("--email", help="myTeamSpeak email (defaults to TS_EMAIL in .env)")
    parser.add_argument("--password", help="myTeamSpeak password (defaults to TS_PASSWORD in .env)")
    parser.add_argument("--otp", default="", help="6-digit 2FA OTP code (optional, will prompt if needed)")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_SESSION_PATH,
        help=f"Output session file path (default: {DEFAULT_SESSION_PATH})",
    )
    args = parser.parse_args()

    cached = load_cached_state(args.out)

    email = args.email or os.environ.get("TS_EMAIL") or cached.get("email") or ""
    password = args.password or os.environ.get("TS_PASSWORD") or os.environ.get("PASSWORD") or ""

    if not email:
        sys.stdout.write("myTeamSpeak Email: ")
        sys.stdout.flush()
        email = sys.stdin.readline().strip()
    if not password:
        password = getpass.getpass("myTeamSpeak Password: ")

    if not email or not password:
        print("[!] Email and password are required.", file=sys.stderr)
        return 1

    print(f"[*] Logging into myTeamSpeak as {email}...", flush=True)
    session_data = login_myts_headless(
        email=email,
        password=password,
        otp=args.otp or os.environ.get("TS_OTP", ""),
        device_id=cached.get("device_id", ""),
        otp_renewal_token=cached.get("otp_renewal_token", ""),
        interactive=True,
    )

    cached.update(session_data)
    save_cached_state(args.out, cached)

    print(f"[+] Logged in as '{session_data.get('username') or email}'")
    print(f"[+] Saved session & renewal tokens to {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
