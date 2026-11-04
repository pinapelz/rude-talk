import asyncio
import inspect
import json
import os
import time
from collections.abc import Callable
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    LoginResponse,
    MatrixRoom,
    RoomMessageMedia,
    RoomMessageText,
)

from constants import COMMAND_PREFIX, CREDENTIALS_FILE, DEVICE_NAME
from teamspeak import fetch_ts_file
from uploaders import upload_file

load_dotenv()

HOMESERVER = os.getenv("HOMESERVER")
USER_ID = os.getenv("USER_ID")
PASSWORD = os.getenv("PASSWORD")

# Track startup time in milliseconds to ignore old messages during initial sync
BOT_START_TIME = int(time.time() * 1000)


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


class MatrixBot:
    def __init__(
        self,
        command_prefix: str = COMMAND_PREFIX,
        allowed_rooms: list[str] | set[str] | None = None,
    ):
        self.client: AsyncClient | None = None
        self.command_prefix = command_prefix
        self.allowed_rooms = set(allowed_rooms) if allowed_rooms is not None else None
        self.commands: dict[str, Callable] = {}
        self.admin_commands: set[str] = set()

    def _is_allowed_room(self, room_id: str) -> bool:
        return self.allowed_rooms is None or room_id in self.allowed_rooms

    def is_admin(self, room: MatrixRoom, user_id: str) -> bool:
        return room.power_levels.get_user_level(user_id) >= 100

    def register_command(self, name: str, func: Callable, admin_only: bool = False):
        """Registers a function as a command handler."""
        cmd = name.lower()
        self.commands[cmd] = func
        if admin_only:
            self.admin_commands.add(cmd)

    def command(self, name_or_func: str | Callable | None = None, *, admin_only: bool = False):
        """Decorator to register a command handler function."""
        if callable(name_or_func):
            self.register_command(name_or_func.__name__, name_or_func, admin_only=admin_only)
            return name_or_func

        def decorator(func: Callable):
            cmd_name = name_or_func if isinstance(name_or_func, str) else func.__name__
            self.register_command(cmd_name, func, admin_only=admin_only)
            return func

        return decorator

    async def login(self) -> AsyncClient:
        """Logs in using saved credentials or falls back to username/password."""
        if os.path.exists(CREDENTIALS_FILE):
            with open(CREDENTIALS_FILE, "r") as f:
                creds = json.load(f)
            self.client = AsyncClient(HOMESERVER)
            self.client.access_token = creds["access_token"]
            self.client.user_id = creds["user_id"]
            self.client.device_id = creds["device_id"]
            print(f"Logged in using stored session for {self.client.user_id}")
        else:
            self.client = AsyncClient(HOMESERVER, USER_ID)
            resp = await self.client.login(PASSWORD, device_name=DEVICE_NAME)

            if isinstance(resp, LoginResponse):
                with open(CREDENTIALS_FILE, "w") as f:
                    json.dump(
                        {
                            "access_token": resp.access_token,
                            "user_id": resp.user_id,
                            "device_id": resp.device_id,
                        },
                        f,
                    )
                print(f"First-time login successful for {resp.user_id}. Session saved.")
            else:
                raise RuntimeError(f"Failed to log in: {resp}")

        return self.client

    async def send_text(self, room_id: str, text: str):
        """Helper method to send plain text messages to a room."""
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": text,
            },
        )

    async def _invoke_command(
        self, handler: Callable, room: MatrixRoom, event: RoomMessageText, args: str
    ):
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())
        available = {
            "bot": self,
            "room": room,
            "event": event,
            "args": args,
        }

        if not params:
            result = handler()
        elif all(p.name in available for p in params):
            kwargs = {p.name: available[p.name] for p in params}
            result = handler(**kwargs)
        elif len(params) == 1:
            result = handler(args)
        elif len(params) == 2:
            result = handler(room, args)
        elif len(params) == 3:
            result = handler(room, event, args)
        else:
            result = handler(self, room, event, args)

        if inspect.isawaitable(result):
            result = await result

        if isinstance(result, str):
            await self.send_text(room.room_id, result)

    async def on_message(self, room: MatrixRoom, event: RoomMessageText):
        """Handles incoming m.text events and dispatches registered commands."""
        if ( # if is the bot itself
            event.sender == self.client.user_id
            or event.server_timestamp < BOT_START_TIME
            or not self._is_allowed_room(room.room_id)
        ):
            return

        body = event.body.strip()
        if not body.startswith(self.command_prefix):
            return

        parts = body[len(self.command_prefix) :].split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        print(f"[Command] '{command}' from {event.sender} in {room.display_name}")

        handler = self.commands.get(command)
        if handler:
            if command in self.admin_commands and not self.is_admin(room, event.sender):
                return
            await self._invoke_command(handler, room, event, args)
        else:
            await self.send_text(room.room_id, f"Unknown command: {command}")

    async def on_file(self, room: MatrixRoom, event: RoomMessageMedia):
        if (
            event.sender == self.client.user_id
            or event.server_timestamp < BOT_START_TIME
            or not self._is_allowed_room(room.room_id)
        ):
            return

        user = room.users.get(event.sender)
        username = (
            (user.display_name if user else None) or event.sender.lstrip("@").split(":", 1)[0]
        ).lstrip("@")

        print(f"[File] '{event.body}' from {username} in {room.display_name}")

        try:
            if "://" not in event.url:
                file_bytes = await asyncio.to_thread(
                    fetch_ts_file,
                    sender=event.sender,
                    room_id=room.room_id,
                    file_id=event.url,
                )
            else:
                download_resp = await self.client.download(mxc=event.url)
                file_bytes = getattr(download_resp, "body", None)

            if isinstance(file_bytes, bytes):
                safe_filename = os.path.basename(event.body)
                size_str = _format_size(len(file_bytes))

                shared_urls = await asyncio.to_thread(upload_file, file_bytes, safe_filename)
                if len(shared_urls) == 1:
                    url = next(iter(shared_urls.values()))
                    await self.send_text(
                        room.room_id,
                        f"{username} sent [{safe_filename}]({url}) ({size_str})",
                    )
                    print(f"Uploaded {safe_filename} to {url}")
                elif shared_urls: # if multiple items
                    links = " | ".join(f"[{dest}]({url})" for dest, url in shared_urls.items())
                    await self.send_text(
                        room.room_id,
                        f"{username} sent {safe_filename} ({size_str}): {links}",
                    )
                    print(f"Uploaded {safe_filename} to {shared_urls}")
                else:
                    await self.send_text(
                        room.room_id,
                        f"{username} sent {safe_filename} ({size_str})",
                    )
                    print(f"Uploaded {safe_filename}")
            else:
                await self.send_text(room.room_id, f"Failed to download '{event.body}'.")
                print(f"Failed to download '{event.body}'")
        except Exception as e:
            print(f"[Error] Failed to process '{event.body}': {e}")
            await self.send_text(room.room_id, f"Failed to upload '{event.body}'.")

    async def run(self):
        await self.login()

        # Register event callbacks
        self.client.add_event_callback(self.on_message, RoomMessageText)
        self.client.add_event_callback(self.on_file, RoomMessageMedia)

        print("Bot is running and listening for events...")
        try:
            # timeout=30000 keeps long-polling open for 30s per request
            await self.client.sync_forever(timeout=30000, full_state=True)
        finally:
            await self.client.close()
