import asyncio
from nio import MatrixRoom, RoomMessageText

from matrix import MatrixBot
from constants import ALLOWED_ROOMS, COMMAND_PREFIX

bot = MatrixBot(command_prefix=COMMAND_PREFIX, allowed_rooms=ALLOWED_ROOMS)


@bot.command()
async def ping(room: MatrixRoom, event: RoomMessageText, args: str):
    await bot.send_text(room.room_id, "Pong!")


if __name__ == "__main__":
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("Shutting down bot.")
