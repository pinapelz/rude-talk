import asyncio

from nio import MatrixRoom, RoomMessageText

from constants import ALLOWED_ROOMS, COMMAND_PREFIX
from matrix import MatrixBot

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

if __name__ == "__main__":
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("Shutting down bot.")
