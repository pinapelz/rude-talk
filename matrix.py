import asyncio
import inspect
import json
import mimetypes
import os
import time
import re
import html
import textwrap
from collections.abc import Callable
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    LoginResponse,
    MatrixRoom,
    RoomMessageMedia,
    RoomMessageText,
)

from constants import COMMAND_PREFIX, CREDENTIALS_FILE, AUTO_REPLY_FILE, DEVICE_NAME
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


def _load_auto_reply_data() -> dict[str, str]:
    if not os.path.exists(AUTO_REPLY_FILE):
        with open(AUTO_REPLY_FILE, "w") as f:
            json.dump({}, f)
    with open(AUTO_REPLY_FILE, "r") as f:
        return json.load(f)

AUTO_REPLY_DATA = {k.lower(): v for k, v in _load_auto_reply_data().items()}
AUTO_REPLY_PATTERN = (
    re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(AUTO_REPLY_DATA, key=len, reverse=True)) + r")(?!\w)",
        flags=re.IGNORECASE,
    )
    if AUTO_REPLY_DATA
    else None
)

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

    async def send_text(self, room_id: str, text: str, reply_to_event_id: str | None = None):
        """Helper method to send plain text messages to a room."""
        content: dict = {
            "msgtype": "m.text",
            "body": text,
        }
        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {
                    "event_id": reply_to_event_id,
                }
            }
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
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

    async def _bridge_mentions_mtx_to_tmspk(self, room: MatrixRoom, event: RoomMessageText):
        """Bridge mentions from Matrix to Teamspeak if any"""
        mentions = event.source.get("content", {}).get("m.mentions", {})
        user_ids = mentions.get("user_ids", [])
        teamspeak_mentions = []
        for user_id in user_ids:
            if "tmspk.net" in user_id:
                member = room.users.get(user_id)
                if not hasattr(member, "display_name"):
                    continue
                teamspeak_mentions.append(f"<@{user_id}|{member.display_name}>")
        if teamspeak_mentions:
            if len(teamspeak_mentions)  == 1:
                await self.send_text(room.room_id, f"{teamspeak_mentions[0]}")
            else:
                await self.send_text(room.room_id, f"{', '.join(teamspeak_mentions)}")
            print(f"Bridged mentions from Matrix to Teamspeak: {teamspeak_mentions}")


    @staticmethod
    def _format_details(match: re.Match) -> str:
        summary, content = match.groups()
        summary_html = f"<summary>{summary.strip()}</summary>" if summary and summary.strip() else ""
        return f"<details>{summary_html}\n{content.strip()}\n</details>"

    @staticmethod
    def _format_align(match: re.Match) -> str:
        _, content = match.groups()
        return f"<div>\n{content.strip()}\n</div>"

    @staticmethod
    def _format_table(match: re.Match) -> str:
        lines = [line.strip() for line in match.group(0).strip().splitlines()]
        if len(lines) < 2:
            return match.group(0)

        def split_row(row: str) -> list[str]:
            inner = re.sub(r"^\|+|\|+$", "", row.strip())
            return [c.strip() for c in re.split(r"(?<!\|)\|(?!\|)", inner)]

        headers = split_row(lines[0])
        seps = split_row(lines[1])
        if not seps or not all(re.match(r"^:?-+:?$", s) for s in seps):
            return match.group(0)

        th_html = "".join(f"<th>{h}</th>" for h in headers)
        rows_html = []
        for row_line in lines[2:]:
            cells = split_row(row_line)
            td_html = "".join(f"<td>{c}</td>" for c in cells)
            rows_html.append(f"<tr>{td_html}</tr>")
        tbody = f"<tbody>{''.join(rows_html)}</tbody>" if rows_html else ""
        return f"<table><thead><tr>{th_html}</tr></thead>{tbody}</table>"

    @staticmethod
    def _format_blockquote(match: re.Match) -> str:
        lines = [re.sub(r"^[ \t]*&gt;[ \t]?", "", line) for line in match.group(0).splitlines()]
        return f"<blockquote>{'<br>'.join(lines)}</blockquote>"

    @staticmethod
    def _format_ul(match: re.Match) -> str:
        items = []
        for line in match.group(0).splitlines():
            item = re.sub(r"^[ \t]*[-*][ \t]+", "", line)
            if item.startswith("[ ] ") or item == "[ ]":
                item = "&#9744; " + item[4:]
            elif item[:4].lower() == "[x] " or item.lower() == "[x]":
                item = "&#9745; " + item[4:]
            items.append(f"<li>{item}</li>")
        return f"<ul>{''.join(items)}</ul>"

    @staticmethod
    def _format_ol(match: re.Match) -> str:
        items = [
            f"<li>{re.sub(r'^[ \t]*\d+\.[ \t]+', '', line)}</li>"
            for line in match.group(0).splitlines()
        ]
        return f"<ol>{''.join(items)}</ol>"

    @staticmethod
    def _format_heading(match: re.Match) -> str:
        hashes, text = match.groups()
        level = len(hashes)
        return f"<h{level}>{text}</h{level}>"

    @staticmethod
    def _format_link(match: re.Match) -> str:
        label, url = match.groups()
        clean_label = re.sub(r"\s*\n\s*", " ", label)
        return f'<a href="{url}">{clean_label}</a>'

    async def _repair_teamspeak_to_matrix_msg(self, room: MatrixRoom, event: RoomMessageText):
        body = event.body or ""
        escaped_body = html.escape(body, quote=False)
        stashed: list[str] = []

        def stash(html_fragment: str) -> str:
            stashed.append(html_fragment)
            return f"\x00STASH_{len(stashed) - 1}\x00"

        def format_codeblock(match: re.Match) -> str:
            language, code = match.groups()
            lang_class = f' class="language-{html.escape(language)}"' if language else ""
            dedented = textwrap.dedent(code.expandtabs(8))
            return stash(f"<pre><code{lang_class}>{dedented}</code></pre>")

        def format_inline_code(match: re.Match) -> str:
            return stash(f"<code>{match.group(1)}</code>")

        def format_mention(match: re.Match) -> str:
            _, nickname = match.groups()
            return stash(f"<code>@{nickname.lstrip('@')}</code>")

        formatters: list[tuple[str, Callable[[re.Match], str] | str, int, str | None]] = [
            # Triple-backtick code blocks & inline code (stashed so inner text is untouched)
            (r"[ \t]*```(\w*)[ \t]*\n(.*?)\n[ \t]*```", format_codeblock, re.DOTALL, None),
            (r"`([^`\n]+)`", format_inline_code, 0, None),
            # TeamSpeak mentions (<@user_id|nickname>) -> inline code fake mention
            (
                r"&lt;@([^|&\n]+)\|([^&\n]+)&gt;",
                format_mention,
                0,
                "Mentions were replaced with inline codeblocks so you don't get pinged multiple times.",
            ),
            # Collapsible details & alignment containers
            (r"^[ \t]*:::details(?:[ \t]+([^\n]*))?\n(.*?)\n[ \t]*:::[ \t]*$", self._format_details, re.MULTILINE | re.DOTALL, None),
            (r"^[ \t]*:::(left|center|right)[ \t]*\n(.*?)\n[ \t]*:::[ \t]*$", self._format_align, re.MULTILINE | re.DOTALL, None),
            # Tables, blockquotes, lists, thematic breaks, headings
            (r"^[ \t]*\|.*\|[ \t]*(?:\n[ \t]*\|.*\|[ \t]*)+", self._format_table, re.MULTILINE, None),
            (r"^[ \t]*&gt;[ \t]?.*(?:\n[ \t]*&gt;[ \t]?.*)*", self._format_blockquote, re.MULTILINE, None),
            (r"^[ \t]*[-*][ \t]+.+(?:\n[ \t]*[-*][ \t]+.+)*", self._format_ul, re.MULTILINE, None),
            (r"^[ \t]*\d+\.[ \t]+.+(?:\n[ \t]*\d+\.[ \t]+.+)*", self._format_ol, re.MULTILINE, None),
            (r"^[ \t]*(?:---+|\*\*\*+|___+)[ \t]*$", "<hr>", re.MULTILINE, None),
            (r"^[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*$", self._format_heading, re.MULTILINE, None),
            # Inline formatting: links, bold, italic, strikethrough, underline, spoilers
            (r"\[([^\]]+)\]\((https?://[^\s)]+)\)", self._format_link, 0, None),
            (r"\*\*(?!\*)(.+?)\*\*", r"<strong>\1</strong>", 0, None),
            (r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", 0, None),
            (r"~~(.+?)~~", r"<del>\1</del>", 0, None),
            (r"\+\+(.+?)\+\+", r"<u>\1</u>", 0, None),
            (r"\|\|(.+?)\|\|", r"<span data-mx-spoiler>\1</span>", 0, None),
        ]

        formatted_body = escaped_body
        footnotes: list[str] = []
        for pattern, repl, flags, footnote in formatters:
            formatted_body, count = re.subn(pattern, repl, formatted_body, flags=flags)
            if count > 0 and footnote:
                footnotes.append(footnote)

        if formatted_body != escaped_body:
            # Remove single newlines immediately adjacent to block tags, convert remaining newlines to <br>
            formatted_body = re.sub(
                r"\n?(</?(?:h[1-6]|ul|ol|table|blockquote|details|summary|div|hr)[^>]*>)\n?",
                r"\1",
                formatted_body,
            )
            formatted_body = re.sub(r"\n[ \t]*", "<br>", formatted_body)

            for idx, html_fragment in enumerate(stashed):
                formatted_body = formatted_body.replace(f"\x00STASH_{idx}\x00", html_fragment)

            plain_body = re.sub(r"<@([^|>]+)\|([^>]+)>", r"`@\2`", body)
            if footnotes:
                footnote_text = " ".join(footnotes)
                plain_body = f"{plain_body}\n{footnote_text}"
                formatted_body = f"{formatted_body}<br><sub>{html.escape(footnote_text)}</sub>"

            content = {
                "msgtype": "m.text",
                "body": plain_body,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted_body,
                "m.mentions": {},
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": event.event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {
                        "event_id": event.event_id,
                    },
                },
            }
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content=content,
            )

    def _try_matching_auto_reply_kwarg(self, message: str) -> str | None:
        if not AUTO_REPLY_PATTERN: # quick exit if no auto reply patterns
            return None
        matches = []
        for m in AUTO_REPLY_PATTERN.finditer(message):
            matches.append(AUTO_REPLY_DATA[m.group(0).lower()])
        return " ".join(matches) if matches else None


    async def on_message(self, room: MatrixRoom, event: RoomMessageText):
        """Handles incoming m.text events and dispatches registered commands."""
        if ( # if is the bot itself
            event.sender == self.client.user_id
            or event.server_timestamp < BOT_START_TIME
            or not self._is_allowed_room(room.room_id)
        ):
            return

        body = event.body.strip()
        # command handler
        if body.startswith(self.command_prefix):
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
        else:
            if "tmspk.net" in event.sender:
                await self._repair_teamspeak_to_matrix_msg(room, event)
            else:
                await self._bridge_mentions_mtx_to_tmspk(room, event)
            auto_match_reply = self._try_matching_auto_reply_kwarg(body)
            if auto_match_reply:
                print(f"[Auto Reply] '{auto_match_reply}' to {event.sender} in {room.display_name}")
                await self.send_text(room.room_id, auto_match_reply, reply_to_event_id=event.event_id)


    @staticmethod
    def _resolve_filename(
        event: RoomMessageMedia,
        file_bytes: bytes,
        download_resp: object = None,
    ) -> str:
        content = event.source.get("content", {}) if isinstance(event.source, dict) else {}
        raw_filename = (
            content.get("filename")
            or getattr(download_resp, "filename", None)
            or event.body
            or "attachment"
        )
        safe_filename = os.path.basename(str(raw_filename).strip()) or "attachment"

        _, ext = os.path.splitext(safe_filename)
        if not ext or not re.match(r"^\.[A-Za-z0-9]{1,10}$", ext):
            ext = ""
            for candidate in (getattr(download_resp, "filename", None), event.body):
                if candidate:
                    _, cand_ext = os.path.splitext(os.path.basename(str(candidate).strip()))
                    if cand_ext and re.match(r"^\.[A-Za-z0-9]{1,10}$", cand_ext):
                        ext = cand_ext
                        break

            if not ext:
                info = content.get("info")
                mimetype = (
                    (info.get("mimetype") if isinstance(info, dict) else None)
                    or getattr(download_resp, "content_type", None)
                )
                if mimetype:
                    ext = mimetypes.guess_extension(mimetype.split(";", 1)[0].strip()) or ""

            if not ext:
                if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    ext = ".png"
                elif file_bytes.startswith(b"\xff\xd8\xff"):
                    ext = ".jpg"
                elif file_bytes.startswith((b"GIF87a", b"GIF89a")):
                    ext = ".gif"
                elif file_bytes.startswith(b"RIFF") and file_bytes[8:12] == b"WEBP":
                    ext = ".webp"
                elif file_bytes.startswith(b"%PDF-"):
                    ext = ".pdf"

            if ext:
                safe_filename = f"{safe_filename}{ext}"

        return safe_filename

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
            download_resp = None
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
                safe_filename = self._resolve_filename(event, file_bytes, download_resp)
                size_str = _format_size(len(file_bytes))

                shared_urls = await asyncio.to_thread(upload_file, file_bytes, safe_filename)
                if len(shared_urls) == 1:
                    url = next(iter(shared_urls.values()))
                    await self.send_text(
                        room.room_id,
                        f"{username} attached [{safe_filename}]({url}) ({size_str})",
                        reply_to_event_id=event.event_id,
                    )
                    print(f"Uploaded {safe_filename} to {url}")
                elif shared_urls: # if multiple items
                    links = " | ".join(f"[{dest}]({url})" for dest, url in shared_urls.items())
                    await self.send_text(
                        room.room_id,
                        f"{username} attached {safe_filename} ({size_str}): {links}",
                        reply_to_event_id=event.event_id,
                    )
                    print(f"Uploaded {safe_filename} to {shared_urls}")
                else:
                    await self.send_text(
                        room.room_id,
                        f"{username} attached {safe_filename} ({size_str})",
                        reply_to_event_id=event.event_id,
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
