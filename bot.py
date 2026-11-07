import asyncio

from nio import MatrixRoom, RoomMessageText

from constants import ALLOWED_ROOMS, COMMAND_PREFIX
from matrix import MatrixBot
import food

bot = MatrixBot(
    command_prefix=COMMAND_PREFIX,
    allowed_rooms=ALLOWED_ROOMS,
)


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

if __name__ == "__main__":
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("Shutting down bot.")
