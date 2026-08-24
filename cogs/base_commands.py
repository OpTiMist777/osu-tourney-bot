# cogs/base_commands.py
import discord
from discord.ext import commands
from database import get_pool_count, get_recent_pools

class BaseCommands(commands.Cog, name="Базовые команды"):
    """Базовые команды бота"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @commands.command(name="ping")
    async def ping(self, ctx):
        """Проверка работоспособности бота"""
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! Задержка: {latency}ms")
    
    @commands.command(name="dbtest")
    @commands.is_owner()
    async def dbtest(self, ctx):
        """Проверка подключения к БД"""
        try:
            count = await get_pool_count()
            await ctx.send(f"✅ База данных работает! Всего пулов: {count}")
        except Exception as e:
            await ctx.send(f"❌ Ошибка БД: {type(e).__name__} - {e}")
    
    @commands.command(name="pools")
    @commands.is_owner()
    async def pools(self, ctx):
        """Просмотр всех пулов"""
        try:
            pools_list = await get_recent_pools(10)
            
            if not pools_list:
                await ctx.send("📭 Нет созданных пулов")
                return
            
            embed = discord.Embed(title="📋 Список пулов", color=0x00ff00)
            for pool in pools_list:
                status_emoji = {"draft": "✏️", "pending": "⏳", "approved": "✅", "rejected": "❌"}.get(pool['status'], "❓")
                embed.add_field(
                    name=f"{status_emoji} ID {pool['pool_id']} | {pool['name']}",
                    value=f"Режим: `{pool['mode'].upper()}` | Статус: `{pool['status']}`",
                    inline=False
                )
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Ошибка: {type(e).__name__} - {e}")
    
    @commands.command(name="help")
    async def help_command(self, ctx):
        """Основная справка по боту"""
        embed = discord.Embed(
            title="🎯 Osu! Tourney Bot — Справка",
            description="Бот для управления турнирными пулами карт в osu!",
            color=0x0099ff
        )
        embed.add_field(
            name="📚 Категории команд",
            value="`/pool_help` — справка по пулам карт\n"
                  "`/pool_create` — создать пул\n"
                  "`/pool_view` — открыть пул и отправить Draft на модерацию",
            inline=False
        )
        embed.add_field(
            name="⚙️ Базовые команды",
            value="`!ping` — проверка работоспособности\n"
                  "`!dbtest` — проверка базы данных (владелец)\n"
                  "`!pools` — список всех пулов (владелец)",
            inline=False
        )
        embed.set_footer(text="Используйте /pool_help для подробной справки по пулам")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(BaseCommands(bot))
