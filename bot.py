import asyncio

from nio import MatrixRoom, RoomMessageText

from constants import ALLOWED_ROOMS, COMMAND_PREFIX
from random import randint
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
