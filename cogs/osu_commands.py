"""Foundation for osu! multiplayer integration.

The actual Bancho IRC client will be implemented separately.  Keeping this
extension loadable prevents the bot startup from failing while that work is
pending and keeps the future osu!-specific features isolated from pool logic.
"""

from discord.ext import commands


class OsuCommands(commands.Cog, name="osu! multiplayer"):
    """Reserved cog for future Bancho multiplayer commands and event handling."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OsuCommands(bot))
