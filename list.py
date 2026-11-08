import os
import random
import re
import unicodedata
from collections import namedtuple
from urllib.parse import quote
from dotenv import load_dotenv
import requests

from constants import LIST_ID, YAMTRACK

load_dotenv()

VALID_MEDIA_TYPES = (
    "tv",
    "movie",
    "anime",
    "manga",
    "game",
    "book",
    "comic",
    "boardgame",
)
DEFAULT_MEDIA_TYPE = "game"

SearchResult = namedtuple(
    "SearchResult",
    ["media_id", "source", "media_type", "title", "image", "release_year"],
)
ListItem = namedtuple(
    "ListItem",
    [
        "id",
        "media_id",
        "source",
        "media_type",
        "title",
        "image",
        "season_number",
        "episode_number",
        "date_added",
    ],
)
AddResult = namedtuple("AddResult", ["added", "list_id", "item"])


class InvalidMediaTypeError(ValueError):
    def __init__(self, media_type: str):
        self.media_type = media_type
        valid_str = ", ".join(VALID_MEDIA_TYPES)
        super().__init__(
            f"Unknown media type '{media_type}'. Valid media types are: {valid_str}"
        )


class YamtrackAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


def _get_headers() -> dict[str, str]:
    api_key = os.getenv("YAMTRACK_API_KEY")
    if not api_key:
        raise YamtrackAPIError("YAMTRACK_API_KEY environment variable is not set.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        data = response.json()
        err_msg = data.get("error") or response.text
    except ValueError:
        err_msg = response.text or f"HTTP {response.status_code}"
    raise YamtrackAPIError(err_msg, status_code=response.status_code)


def validate_media_type(media_type: str) -> str:
    normalized = media_type.strip().lower()
    if normalized not in VALID_MEDIA_TYPES:
        raise InvalidMediaTypeError(media_type)
    return normalized


def parse_media_args(args: str) -> tuple[str, str]:
    """Parse optional [media_type] and <query> from command args.

    Supports:
    - `type:<media_type> <query>` or `--type <media_type> <query>`
    - `<media_type> <query>` if the first word is a valid media type
    - `<query>` (defaults to DEFAULT_MEDIA_TYPE = 'game')
    """
    stripped = args.strip()
    if not stripped:
        return DEFAULT_MEDIA_TYPE, ""

    parts = stripped.split(maxsplit=1)
    first = parts[0].lower()

    if first.startswith("type:") or first.startswith("type="):
        raw_type = first.split(":", 1)[1] if ":" in first else first.split("=", 1)[1]
        media_type = validate_media_type(raw_type)
        query = parts[1].strip() if len(parts) > 1 else ""
        return media_type, query

    if first in ("--type", "-t"):
        if len(parts) < 2:
            raise InvalidMediaTypeError("")
        subparts = parts[1].split(maxsplit=1)
        media_type = validate_media_type(subparts[0])
        query = subparts[1].strip() if len(subparts) > 1 else ""
        return media_type, query

    if first in VALID_MEDIA_TYPES:
        query = parts[1].strip() if len(parts) > 1 else ""
        return first, query

    return DEFAULT_MEDIA_TYPE, stripped


def search_media(
    query: str,
    media_type: str = DEFAULT_MEDIA_TYPE,
    source: str | None = None,
    page: int = 1,
) -> list[SearchResult]:
    """Search external media providers via GET /api/search."""
    media_type = validate_media_type(media_type)
    params: dict[str, str | int] = {
        "q": query,
        "media_type": media_type,
        "page": page,
    }
    if source:
        params["source"] = source

    url = f"{YAMTRACK.rstrip('/')}/api/search"
    response = requests.get(url, headers=_get_headers(), params=params, timeout=15)
    _raise_for_api_error(response)

    data = response.json()
    results = []
    for item in data.get("results", []):
        results.append(
            SearchResult(
                media_id=str(item.get("media_id", "")),
                source=item.get("source", ""),
                media_type=item.get("media_type", media_type),
                title=item.get("title", "Unknown"),
                image=item.get("image", ""),
                release_year=item.get("release_year"),
            )
        )
    return results


def _parse_add_response(data: dict, fallback_list_id: int) -> AddResult:
    item_data = data.get("item", {})
    item = ListItem(
        id=item_data.get("id"),
        media_id=str(item_data.get("media_id", "")),
        source=item_data.get("source", ""),
        media_type=item_data.get("media_type", ""),
        title=item_data.get("title", "Unknown"),
        image=item_data.get("image", ""),
        season_number=item_data.get("season_number"),
        episode_number=item_data.get("episode_number"),
        date_added=item_data.get("date_added"),
    )
    return AddResult(
        added=bool(data.get("added", False)),
        list_id=data.get("list_id", fallback_list_id),
        item=item,
    )


def add_item_by_query(
    query: str,
    media_type: str = DEFAULT_MEDIA_TYPE,
    source: str | None = None,
    list_id: int = LIST_ID,
) -> AddResult:
    """Add the top search match to a list via POST /api/lists/{list_id}/items."""
    media_type = validate_media_type(media_type)
    payload: dict[str, str] = {
        "query": query,
        "media_type": media_type,
    }
    if source:
        payload["source"] = source

    url = f"{YAMTRACK.rstrip('/')}/api/lists/{list_id}/items"
    response = requests.post(url, headers=_get_headers(), json=payload, timeout=15)
    _raise_for_api_error(response)
    return _parse_add_response(response.json(), list_id)


def add_item_by_provider_id(
    media_id: str,
    media_type: str = DEFAULT_MEDIA_TYPE,
    source: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    list_id: int = LIST_ID,
) -> AddResult:
    """Add an item by external provider media_id via POST /api/lists/{list_id}/items."""
    media_type = validate_media_type(media_type)
    payload: dict[str, str | int | None] = {
        "media_id": str(media_id),
        "media_type": media_type,
    }
    if source:
        payload["source"] = source
    if season_number is not None:
        payload["season_number"] = season_number
    if episode_number is not None:
        payload["episode_number"] = episode_number

    url = f"{YAMTRACK.rstrip('/')}/api/lists/{list_id}/items"
    response = requests.post(url, headers=_get_headers(), json=payload, timeout=15)
    _raise_for_api_error(response)
    return _parse_add_response(response.json(), list_id)


def get_list_items(
    media_type: str | None = None,
    list_id: int = LIST_ID,
) -> list[ListItem]:
    """Pull items within a given list via GET /api/lists/{list_id}/items."""
    params: dict[str, str] = {}
    if media_type:
        params["media_type"] = validate_media_type(media_type)

    url = f"{YAMTRACK.rstrip('/')}/api/lists/{list_id}/items"
    response = requests.get(url, headers=_get_headers(), params=params, timeout=15)
    _raise_for_api_error(response)

    data = response.json()
    items = []
    for item_data in data.get("items", []):
        items.append(
            ListItem(
                id=item_data.get("id"),
                media_id=str(item_data.get("media_id", "")),
                source=item_data.get("source", ""),
                media_type=item_data.get("media_type", ""),
                title=item_data.get("title", "Unknown"),
                image=item_data.get("image", ""),
                season_number=item_data.get("season_number"),
                episode_number=item_data.get("episode_number"),
                date_added=item_data.get("date_added"),
            )
        )
    return items


def pick_random_item(
    media_type: str | None = None,
    list_id: int = LIST_ID,
) -> ListItem | None:
    """Pick a random item from the custom list (optionally filtered by media_type)."""
    items = get_list_items(media_type=media_type, list_id=list_id)
    if not items:
        return None
    return random.choice(items)


def _slugify(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", str(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    cleaned = re.sub(r"[^\w\s-]", "", normalized.lower())
    cleaned = re.sub(r"[-\s]+", "-", cleaned).strip("-_")
    return cleaned or quote(str(value), safe="") or "item"


def get_item_url(item: ListItem | SearchResult) -> str:
    """Return the Yamtrack details URL for a media item."""
    base = YAMTRACK.rstrip("/")
    title_slug = _slugify(item.title)
    return f"{base}/details/{item.source}/{item.media_type}/{item.media_id}/{title_slug}"


