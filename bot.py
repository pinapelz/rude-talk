import asyncio

from nio import MatrixRoom, RoomMessageText

from constants import ALLOWED_ROOMS, COMMAND_PREFIX, YAMTRACK, LIST_ID
from random import randint
from matrix import MatrixBot
import food
import list as yamtrack_list

bot = MatrixBot(
    command_prefix=COMMAND_PREFIX,
    allowed_rooms=ALLOWED_ROOMS,
)


@bot.command("help")
async def help_command(room: MatrixRoom, event: RoomMessageText, args: str):
    commands = [
        f"{bot.command_prefix}{name}" + (" (admin only)" if name in bot.admin_commands else "")
        for name in sorted(bot.commands)
    ]
    await bot.send_text(room.room_id, "Available commands: " + ", ".join(commands))


@bot.command()
async def ping(room: MatrixRoom, event: RoomMessageText, args: str):
    await bot.send_text(room.room_id, "Pong!")


@bot.command(admin_only=True)
async def admin_status(room: MatrixRoom, event: RoomMessageText, args: str):
    await bot.send_text(room.room_id, "You are an admin!")

@bot.command()
async def pick_food(room: MatrixRoom, event: RoomMessageText, args: str):
    if len(args) < 1:
        await bot.send_text(room.room_id, "need to tell me if you want a BIG or SMALL bite")
        return
    category = args.upper()
    if category not in set(["BIG", "SMALL"]):
        await bot.send_text(room.room_id, "no idea what size that is. im asking BIG or SMALL")
        return
    food_item = food.pick_choice_from_layer(category)
    await bot.send_text(room.room_id, "how about " + food_item.name + "?" + " for a " + args + " bite:" + food_item.url + "")

@bot.command()
async def gmtfm(room: MatrixRoom, event: RoomMessageText, args: str):
    await bot.send_text(room.room_id, "ok fatty: https://www.google.com/maps/d/u/0/viewer?mid=1Brpw12sBY_C7pPSVHgLYGs879RJ3BAI")


@bot.command()
async def loc(room: MatrixRoom, event: RoomMessageText, args: str):
    await bot.send_text(room.room_id, f"{YAMTRACK}/list/{LIST_ID}")


@bot.command("loc_search")
@bot.command()
async def search_loc(room: MatrixRoom, event: RoomMessageText, args: str):
    valid_types_str = ", ".join(yamtrack_list.VALID_MEDIA_TYPES)
    if not args.strip():
        await bot.send_text(
            room.room_id,
            f"Usage: !search_loc [media_type] <query> (default: {yamtrack_list.DEFAULT_MEDIA_TYPE}). Valid media types: {valid_types_str}",
        )
        return

    try:
        media_type, query = yamtrack_list.parse_media_args(args)
    except yamtrack_list.InvalidMediaTypeError as e:
        await bot.send_text(room.room_id, str(e))
        return

    if not query:
        await bot.send_text(
            room.room_id,
            f"Please provide a search query after media type '{media_type}'. Valid media types: {valid_types_str}",
        )
        return

    try:
        results = await asyncio.to_thread(yamtrack_list.search_media, query, media_type)
    except yamtrack_list.YamtrackAPIError as e:
        await bot.send_text(
            room.room_id,
            f"Search failed: {e} (Valid media types: {valid_types_str})",
        )
        return

    if not results:
        await bot.send_text(
            room.room_id,
            f"No {media_type} results found for '{query}'. Valid media types: {valid_types_str}",
        )
        return

    lines = [f"Top {media_type} results for '{query}':"]
    for idx, item in enumerate(results[:5], start=1):
        year = f" ({item.release_year})" if item.release_year else ""
        lines.append(f"{idx}. {item.title}{year} [{item.source} id:{item.media_id}]")
    await bot.send_text(room.room_id, "\n".join(lines))


@bot.command("loc_add")
@bot.command()
async def loc_add(room: MatrixRoom, event: RoomMessageText, args: str):
    valid_types_str = ", ".join(yamtrack_list.VALID_MEDIA_TYPES)
    if not args.strip():
        await bot.send_text(
            room.room_id,
            f"Usage: !loc_add [media_type] <query or id:MEDIA_ID> (default: {yamtrack_list.DEFAULT_MEDIA_TYPE}). Valid media types: {valid_types_str}",
        )
        return

    try:
        media_type, query = yamtrack_list.parse_media_args(args)
    except yamtrack_list.InvalidMediaTypeError as e:
        await bot.send_text(room.room_id, str(e))
        return

    if not query:
        await bot.send_text(
            room.room_id,
            f"Please provide a query or id:MEDIA_ID after media type '{media_type}'. Valid media types: {valid_types_str}",
        )
        return

    try:
        if query.lower().startswith("id:"):
            media_id = query.split(":", 1)[1].strip()
            res = await asyncio.to_thread(
                yamtrack_list.add_item_by_provider_id, media_id, media_type
            )
        else:
            res = await asyncio.to_thread(
                yamtrack_list.add_item_by_query, query, media_type
            )
    except yamtrack_list.YamtrackAPIError as e:
        await bot.send_text(
            room.room_id,
            f"Failed to add '{query}' ({media_type}): {e}. Valid media types: {valid_types_str}",
        )
        return

    item_url = yamtrack_list.get_item_url(res.item)
    if res.added:
        await bot.send_text(
            room.room_id,
            f"Added '{res.item.title}' ({res.item.media_type}) to the list! {item_url}",
        )
    else:
        await bot.send_text(
            room.room_id,
            f"'{res.item.title}' ({res.item.media_type}) is already in the list: {item_url}",
        )


@bot.command("loc_pick")
@bot.command("random_loc")
@bot.command("loc_random")
@bot.command()
async def pick_loc(room: MatrixRoom, event: RoomMessageText, args: str):
    valid_types_str = ", ".join(yamtrack_list.VALID_MEDIA_TYPES)
    media_type = args.strip().lower() if args.strip() else None

    try:
        item = await asyncio.to_thread(yamtrack_list.pick_random_item, media_type)
    except yamtrack_list.InvalidMediaTypeError as e:
        await bot.send_text(room.room_id, str(e))
        return
    except yamtrack_list.YamtrackAPIError as e:
        await bot.send_text(
            room.room_id,
            f"Failed to pick from list: {e} (Valid media types: {valid_types_str})",
        )
        return

    if not item:
        if media_type:
            await bot.send_text(
                room.room_id,
                f"No '{media_type}' items found in the list.",
            )
        else:
            await bot.send_text(room.room_id, "The list is currently empty.")
        return

    await bot.send_text(
        room.room_id,
        f"How about '{item.title}' ({item.media_type})?",
    )


@bot.command()
async def roll(room: MatrixRoom, event: RoomMessageText, args: str):
    "Roll a D-sided die"
    if not args or not args.isdigit():
        await bot.send_text(room.room_id, f"Usage: {COMMAND_PREFIX}roll <D>")
        return
    d = int(args)
    await bot.send_text(room.room_id, f"You rolled a {randint(1, d)}")


if __name__ == "__main__":
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("Shutting down bot.")
