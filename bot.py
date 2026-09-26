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

# Project events use one concise console format.  Do not log environment
# values, raw IRC authentication, or Discord tokens.
app_logger = logging.getLogger("osu_tourney")
if not app_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    app_logger.addHandler(handler)
app_logger.setLevel(logging.INFO)
app_logger.propagate = False

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Create the bot
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    case_insensitive=True,
    help_command=None
)
commands_synced = False
osu_token_refresh_task: asyncio.Task | None = None
moderation_views_restored = False
live_matches_restored = False


async def keep_osu_token_fresh() -> None:
    """Refresh the osu! OAuth token before expiry without interrupting commands."""
    while not bot.is_closed():
        try:
            await osu_manager.warm_up()
            # _get_token stores expiry one hour early, so this waits until the
            # next safe refresh point instead of refreshing on every loop.
            await asyncio.sleep(max(60, osu_manager.seconds_until_refresh()))
        except Exception as error:
            print(f"⚠️ Could not refresh the osu! API token: {error}")
            await asyncio.sleep(300)

@bot.event
async def on_ready():
    """Handle bot readiness."""
    print(f"✅ Bot is ready as {bot.user}")
    guild_count = len(bot.guilds)
    guild_word = "server" if guild_count == 1 else "servers"
    print(f"📡 Connected to {guild_count} {guild_word}")
    await bot.change_presence(
        activity=discord.Game(name="osu! ladders | /pool_help")
    )
    # Initialize the database
    await init_db()
    global moderation_views_restored
    if not moderation_views_restored:
        pool_cog = bot.get_cog("Pool Commands")
        if pool_cog is not None:
            await pool_cog.restore_moderation_views()
            moderation_views_restored = True
    global live_matches_restored
    if not live_matches_restored:
        osu_cog = bot.get_cog("osu! multiplayer")
        if osu_cog is not None:
            await osu_cog.restore_live_matches()
        live_matches_restored = True
    global osu_token_refresh_task
    if osu_token_refresh_task is None or osu_token_refresh_task.done():
        osu_token_refresh_task = asyncio.create_task(keep_osu_token_fresh())
        print("🔑 Started background osu! API token refresh")
    global commands_synced
    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True
    print("✅ Slash commands synchronized")

async def load_cogs():
    """Load all cogs from the cogs directory."""
    cog_files = [
        "pool_commands",
        "osu_commands"
    ]
    
    for cog in cog_files:
        try:
            await bot.load_extension(f"cogs.{cog}")
            print(f"✅ Loaded cog: {cog}")
        except Exception as e:
            print(f"❌ Failed to load cog {cog}: {e}")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("❌ Token not found. Check your .env file.")
    
    print("🚀 Starting bot...")
    asyncio.run(load_cogs())
    bot.run(token, log_handler=None)
