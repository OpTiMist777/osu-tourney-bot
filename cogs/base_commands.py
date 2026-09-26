# cogs/base_commands.py
import discord
from discord.ext import commands
from database import get_pool_count, get_recent_pools

class BaseCommands(commands.Cog, name="Base Commands"):
    """Basic bot commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @commands.command(name="ping")
    async def ping(self, ctx):
        """Check whether the bot is responding."""
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! Latency: {latency} ms")
    
    @commands.command(name="dbtest")
    @commands.is_owner()
    async def dbtest(self, ctx):
        """Check the database connection."""
        try:
            count = await get_pool_count()
            await ctx.send(f"✅ Database is working! Total pools: {count}")
        except Exception as e:
            await ctx.send(f"❌ Database error: {type(e).__name__} - {e}")
    
    @commands.command(name="pools")
    @commands.is_owner()
    async def pools(self, ctx):
        """View recent pools."""
        try:
            pools_list = await get_recent_pools(10)
            
            if not pools_list:
                await ctx.send("📭 No pools have been created yet.")
                return
            
            embed = discord.Embed(title="📋 Pool list", color=0x00ff00)
            for pool in pools_list:
                status_emoji = {"draft": "✏️", "pending": "⏳", "ranked": "✅", "unranked": "❌"}.get(pool['status'], "❓")
                embed.add_field(
                    name=f"{status_emoji} {pool['name']}",
                    value=f"Mode: `{pool['mode'].upper()}` | Status: `{pool['status']}`",
                    inline=False
                )
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error: {type(e).__name__} - {e}")
    
    @commands.command(name="help")
    async def help_command(self, ctx):
        """Show the main bot help."""
        embed = discord.Embed(
            title="🎯 osu! Tourney Bot — Help",
            description="Manage osu! tournament map pools and matches.",
            color=0x0099ff
        )
        embed.add_field(
            name="📚 Commands",
            value="`/pool_help` — pool command help\n"
                  "`/pool_create` — create a pool\n"
                  "`/pool_view` — view a pool and submit a Draft for moderation\n"
                  "`/match_create` — create a test pick/ban match for linked players",
            inline=False
        )
        embed.add_field(
            name="⚙️ Basic commands",
            value="`!ping` — check bot latency\n"
                  "`!dbtest` — check the database (owner only)\n"
                  "`!pools` — list recent pools (owner only)",
            inline=False
        )
        embed.set_footer(text="Use /pool_help for detailed pool help.")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(BaseCommands(bot))
