# bot.py
import os
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from database import init_db
from osu_api import osu_manager

load_dotenv()

# Do not let discord.py attach its default console logger. Startup and command
# errors are handled explicitly below, keeping the console output concise.
logging.getLogger("discord").setLevel(logging.CRITICAL)

# Настройка intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Создание бота
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    case_insensitive=True,
    help_command=None
)
commands_synced = False
osu_token_refresh_task: asyncio.Task | None = None
moderation_views_restored = False


async def keep_osu_token_fresh() -> None:
    """Refresh the osu! OAuth token before expiry without interrupting commands."""
    while not bot.is_closed():
        try:
            await osu_manager.warm_up()
            # _get_token stores expiry one hour early, so this waits until the
            # next safe refresh point instead of refreshing on every loop.
            await asyncio.sleep(max(60, osu_manager.seconds_until_refresh()))
        except Exception as error:
            print(f"⚠️ Не удалось обновить токен osu! API: {error}")
            await asyncio.sleep(300)

@bot.event
async def on_ready():
    """Обработчик готовности бота"""
    print(f"✅ Бот запущен как {bot.user}")
    guild_count = len(bot.guilds)
    guild_word = "серверу" if guild_count == 1 else "серверам"
    print(f"📡 Подключено к {guild_count} {guild_word}")
    await bot.change_presence(
        activity=discord.Game(name="osu! ladders | /pool_help")
    )
    # Инициализация БД
    await init_db()
    global moderation_views_restored
    if not moderation_views_restored:
        pool_cog = bot.get_cog("Команды пулов")
        if pool_cog is not None:
            await pool_cog.restore_moderation_views()
            moderation_views_restored = True
    global osu_token_refresh_task
    if osu_token_refresh_task is None or osu_token_refresh_task.done():
        osu_token_refresh_task = asyncio.create_task(keep_osu_token_fresh())
        print("🔑 Запущено фоновое обновление токена osu! API")
    global commands_synced
    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True
        print("✅ Slash-команды синхронизированы")

async def load_cogs():
    """Загрузка всех когов из папки cogs"""
    cog_files = [
        "base_commands",
        "pool_commands",
        "osu_commands"
    ]
    
    for cog in cog_files:
        try:
            await bot.load_extension(f"cogs.{cog}")
            print(f"✅ Загружен ког: {cog}")
        except Exception as e:
            print(f"❌ Ошибка загрузки кога {cog}: {e}")

@bot.event
async def on_command_error(ctx, error):
    """Глобальный обработчик ошибок команд"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❌ Команда не найдена. Используйте `!help` для списка команд.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас недостаточно прав для выполнения этой команды.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("❌ Эта команда доступна только владельцу бота.")
    else:
        print(f"⚠️ Необработанная ошибка: {error}")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("❌ Токен не найден! Проверьте файл .env")
    
    print("🚀 Запуск бота...")
    asyncio.run(load_cogs())
    bot.run(token, log_handler=None)
