import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import traceback
from database import (
    create_pool_with_maps, get_pool_maps, get_pool, update_pool_status,
    log_moderation_action, get_pool_logs, get_pool_by_name, list_pools,
    pool_name_in_use_for_review, set_moderation_message, get_pending_moderation_pools,
    get_pools_by_status, delete_pool, update_pool_map, add_pool_map
)
from osu_api import osu_manager
from utils import (
    validate_pool_maps, parse_spaced_category_maps, format_category_requirements,
    CATEGORY_FULL_NAMES
)
from rulesets import get_ruleset


def _maps_from_category_fields(mode: str, **category_fields: str | None) -> tuple[list[tuple[str, int]], str]:
    """Turn slash-command category fields (``NM: 123 456``) into pool slots."""
    parts = []
    for category, ids in category_fields.items():
        if ids and ids.strip():
            parts.append(f"{category}:{ids.strip()}")
    return parse_spaced_category_maps(" ".join(parts), mode)


class PoolSubmitView(discord.ui.View):
    """The pool author can submit a draft directly from its slash-command view."""

    def __init__(self, cog: "PoolCommands", pool_id: int, author_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.pool_id = pool_id
        self.author_id = author_id

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.primary, emoji="📤")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the pool author can submit it for moderation.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        success, message = await self.cog.submit_draft_to_moderation(self.pool_id, interaction.user)
        if success:
            button.disabled = True
            await interaction.message.edit(view=self)
        await interaction.followup.send(message, ephemeral=True)


class ModerationConfirmView(discord.ui.View):
    def __init__(
        self, cog: "PoolCommands", pool_id: int, action: str,
        moderation_message: discord.Message, reason: str | None = None,
    ):
        super().__init__(timeout=120)
        self.cog, self.pool_id, self.action = cog, pool_id, action
        self.moderation_message, self.reason = moderation_message, reason

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.cog.is_moderator(interaction):
            await interaction.response.send_message("❌ Moderation buttons are available to administrators only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.action == "approve":
            success, message = await self.cog.moderate_pool(self.pool_id, interaction.user, "ranked")
        else:
            success, message = await self.cog.moderate_pool(self.pool_id, interaction.user, "unranked", self.reason)
        if success:
            # interaction.message is the ephemeral confirmation, not the
            # original moderator-channel message that must be updated.
            await self.cog.finish_moderation_message(self.moderation_message, self.pool_id)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Action cancelled.", view=None)


class RejectReasonModal(discord.ui.Modal, title="Rejection reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog: "PoolCommands", pool_id: int, moderation_message: discord.Message):
        super().__init__()
        self.cog, self.pool_id, self.moderation_message = cog, pool_id, moderation_message

    async def on_submit(self, interaction: discord.Interaction):
        confirm = ModerationConfirmView(
            self.cog, self.pool_id, "reject", self.moderation_message, str(self.reason)
        )
        await interaction.response.send_message("Confirm rejecting this pool?", view=confirm, ephemeral=True)


class ModerationActionsView(discord.ui.View):
    def __init__(self, cog: "PoolCommands", pool_id: int):
        super().__init__(timeout=None)
        self.cog, self.pool_id = cog, pool_id
        rank_button = discord.ui.Button(
            label="Rank",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"pool_moderation:{pool_id}:rank",
        )
        rank_button.callback = self.rank
        self.add_item(rank_button)

        unrank_button = discord.ui.Button(
            label="Unrank",
            style=discord.ButtonStyle.danger,
            # A red cross is almost invisible on Discord's red danger button.
            emoji="📉",
            custom_id=f"pool_moderation:{pool_id}:unrank",
        )
        unrank_button.callback = self.unrank
        self.add_item(unrank_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.cog.is_moderator(interaction):
            return True
        await interaction.response.send_message("❌ Moderation buttons are available to administrators only.", ephemeral=True)
        return False

    async def rank(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Confirm assigning the Ranked status?",
            view=ModerationConfirmView(self.cog, self.pool_id, "approve", interaction.message),
            ephemeral=True,
        )

    async def unrank(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RejectReasonModal(self.cog, self.pool_id, interaction.message))


class DraftDeleteView(discord.ui.View):
    """Explicit confirmation before an author permanently deletes a draft."""

    def __init__(self, cog: "PoolCommands", pool_id: int, author_id: int):
        super().__init__(timeout=120)
        self.cog, self.pool_id, self.author_id = cog, pool_id, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("❌ Only the pool author can delete a draft.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete pool", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button):
        pool = await get_pool(self.pool_id)
        if not pool:
            await interaction.response.edit_message(content="❌ Pool not found.", view=None)
            return
        if pool['created_by'] != interaction.user.id or pool['status'] != 'draft':
            await interaction.response.edit_message(content="❌ You can only delete your own Draft pool.", view=None)
            return
        success, error = await delete_pool(self.pool_id)
        if not success:
            await interaction.response.edit_message(content=f"❌ Failed to delete the pool: {error}", view=None)
            return
        await interaction.response.edit_message(content=f"🗑️ Draft **{pool['name']}** deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)

class PoolCommands(commands.Cog, name="Pool Commands"):
    """Manage osu! tournament map pools."""

    # Keep a conservative gap between map parses. A parse can make both a
    # beatmap request and a difficulty-attributes request to osu! API.
    PARSE_DELAY_SECONDS = 0.5
    MODERATION_CHANNEL_ID = 1539982952996409386
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def restore_moderation_views(self) -> None:
        """Re-register Rank/Unrank buttons for pending moderator messages after restart."""
        restored = 0
        for pool in await get_pending_moderation_pools():
            self.bot.add_view(
                ModerationActionsView(self, pool['pool_id']),
                message_id=pool['moderation_message_id'],
            )
            restored += 1
        print(f"✅ Restored moderation buttons: {restored}")

    async def _get_moderation_channel(self) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(self.MODERATION_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.MODERATION_CHANNEL_ID)
            except discord.DiscordException:
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def repost_pending_pool(self, pool: dict) -> tuple[bool, str]:
        """Create a fresh moderator post and replace the stored message reference."""
        channel = await self._get_moderation_channel()
        if channel is None:
            return False, "moderation channel is unavailable"
        embed, _ = await self._pool_view_embed(pool['pool_id'])
        if embed is None:
            return False, "could not build the pool data"
        embed.title = f"📥 Pending moderation: {pool['name']}"
        embed.add_field(name="Author", value=f"<@{pool['created_by']}>", inline=True)
        try:
            message = await channel.send(embed=embed, view=ModerationActionsView(self, pool['pool_id']))
        except discord.DiscordException as error:
            return False, str(error)
        if not await set_moderation_message(pool['pool_id'], channel.id, message.id):
            await message.delete()
            return False, "could not save the moderation message link"
        return True, ""

    @staticmethod
    def is_moderator(interaction: discord.Interaction) -> bool:
        return bool(getattr(interaction.user, "guild_permissions", None) and interaction.user.guild_permissions.administrator)

    async def moderate_pool(self, pool_id: int, moderator: discord.abc.User, status: str, reason: str | None = None) -> tuple[bool, str]:
        pool = await get_pool(pool_id)
        if not pool:
            return False, "❌ Pool not found."
        if pool['status'] != 'pending':
            return False, f"❌ Pool already has the `{pool['status']}` status."
        success, error = await update_pool_status(
            pool_id, status, moderator.id if status == 'ranked' else None,
            expected_status='pending',
        )
        if not success:
            return False, f"❌ Failed to update the status: {error}"
        action = 'rank' if status == 'ranked' else 'unrank'
        await log_moderation_action(pool_id, action, moderator.id, reason)

        try:
            author = await self.bot.fetch_user(pool['created_by'])
            if status == 'ranked':
                notification = discord.Embed(title="✅ Your pool was ranked!", description=f"**{pool['name']}**", color=0x00ff00)
            else:
                notification = discord.Embed(title="❌ Your pool was unranked", description=f"**{pool['name']}**", color=0xff0000)
                notification.add_field(name="Reason", value=reason or "No reason provided", inline=False)
            await author.send(embed=notification)
        except discord.DiscordException:
            pass
        status_name = "Ranked" if status == 'ranked' else "Unranked"
        return True, f"✅ Pool status changed to {status_name}. The author was notified by DM if allowed."

    async def finish_moderation_message(self, message: discord.Message, pool_id: int) -> None:
        pool = await get_pool(pool_id)
        if pool is None:
            return
        ranked = pool['status'] == 'ranked'
        embed = discord.Embed(
            title="✅ Pool ranked" if ranked else "❌ Pool unranked",
            description=(
                f"**{pool['name']}**\n"
                f"Status changed to **{'Ranked' if ranked else 'Unranked'}**."
            ),
            color=0x00ff00 if ranked else 0xff0000,
        )
        await message.edit(embed=embed, view=None)

    async def _pool_view_embed(
        self, pool_id: int, *, include_moderation_history: bool = False,
    ) -> tuple[discord.Embed | None, dict | None]:
        """Build the common pool-details embed used by prefix and slash views."""
        pool = await get_pool(pool_id)
        if not pool:
            return None, None

        pool_maps = await get_pool_maps(pool_id)
        status_emojis = {'draft': '✏️', 'pending': '⏳', 'ranked': '✅', 'unranked': '❌'}
        status_colors = {'draft': 0x0099ff, 'pending': 0xffa500, 'ranked': 0x00ff00, 'unranked': 0xff0000}
        embed = discord.Embed(
            title=f"{status_emojis.get(pool['status'], '❓')} {pool['name']}",
            color=status_colors.get(pool['status'], 0x808080),
        )
        embed.add_field(name="Mode", value=pool['mode'].upper(), inline=True)
        embed.add_field(name="Status", value=pool['status'].capitalize(), inline=True)
        embed.add_field(name="Maps", value=f"`{len(pool_maps)}`", inline=True)
        for name, value in await self._format_pool_cards(pool_maps, pool['mode']):
            embed.add_field(name=name, value=value, inline=False)
        if include_moderation_history:
            logs = await get_pool_logs(pool_id)
            if logs:
                action_emoji = {
                    'submit': '📤', 'cancel': '↩️', 'rank': '✅',
                    'unrank': '❌', 'delete': '🗑️', 'edit': '✏️', 'add': '➕',
                }
                lines = []
                for log in logs[:5]:
                    line = f"{action_emoji.get(log['action'], '❓')} `{log['action'].upper()}` — <@{log['moderator_id']}>"
                    if log.get('reason'):
                        line += f"\n└ {log['reason']}"
                    lines.append(line)
                embed.add_field(
                    name=f"📋 Moderation history ({len(logs)})",
                    value="\n".join(lines),
                    inline=False,
                )
        return embed, pool

    async def submit_draft_to_moderation(self, pool_id: int, author: discord.abc.User) -> tuple[bool, str]:
        """Post a draft to the moderator channel, then mark it pending."""
        pool = await get_pool(pool_id)
        if not pool:
            return False, "❌ Pool not found."
        if pool['created_by'] != author.id:
            return False, "❌ Only the pool author can submit it for moderation."
        if pool['status'] != 'draft':
            return False, f"❌ Pool already has the `{pool['status']}` status."
        if await pool_name_in_use_for_review(pool['name'], pool_id):
            return False, "❌ This name is already used by a pending or ranked pool."

        success, error = await update_pool_status(pool_id, 'pending', expected_status='draft')
        if not success:
            return False, f"❌ Failed to update the status: {error}"
        posted, post_error = await self.repost_pending_pool(pool)
        if not posted:
            await update_pool_status(pool_id, 'draft', expected_status='pending')
            return False, f"❌ Could not send the pool to the moderation channel: {post_error}"
        await log_moderation_action(pool_id, 'submit', author.id)
        return True, "✅ Pool sent to the moderation channel and is awaiting review."

    @staticmethod
    def _apply_std_slot_mods(stats: dict, slot: str) -> tuple[dict, str]:
        """Return display stats for a standard pool slot without altering API data."""
        result = stats.copy()
        category = ''.join(filter(str.isalpha, slot)).upper()

        if category == "HR":
            result["cs"] = min(10.0, round(result["cs"] * 1.3, 1))
            result["ar"] = min(10.0, round(result["ar"] * 1.4, 1))
            result["od"] = min(10.0, round(result["od"] * 1.4, 1))
            return result, "HR"

        if category == "DT":
            # AR and OD are calculated through their timing windows, then
            # converted back after the 1.5x clock rate is applied.
            ar = result["ar"]
            preempt = 1800 - 120 * ar if ar <= 5 else 1950 - 150 * ar
            preempt /= 1.5
            result["ar"] = round((1800 - preempt) / 120 if preempt >= 1200 else (1950 - preempt) / 150, 1)

            hit_window_300 = (79.5 - 6 * result["od"]) / 1.5
            result["od"] = round(min(10.0, max(0.0, (79.5 - hit_window_300) / 6)), 1)
            result["bpm"] = round(result["bpm"] * 1.5, 1)
            result["length"] = max(0, round(result["length"] / 1.5))
            return result, "DT"

        labels = {"NM": "NM", "HD": "HD", "FM": "FM", "TB": "TB"}
        return result, labels.get(category, category or "NM")

    @staticmethod
    def _slot_mods(slot: str) -> list[str]:
        """Map a standard pool slot to the mods fixed by that slot."""
        category = ''.join(filter(str.isalpha, slot)).upper()
        return [category] if category in {"HD", "HR", "DT"} else []

    async def _parse_map_snapshot(self, slot: str, beatmap_id: int, mode: str) -> dict:
        """Fetch once from osu! API and return everything needed for later pool views."""
        print(f"🔎 Parsing {slot.upper()} · beatmap {beatmap_id} · mode {mode.upper()}...")
        await asyncio.sleep(self.PARSE_DELAY_SECONDS)
        beatmap = await osu_manager.get_beatmap(beatmap_id)
        ruleset = get_ruleset(mode)
        target_ruleset = ruleset.osu_ruleset
        # `osu_api.get_beatmap()` uses the project's display name `ctb`,
        # whereas the official attributes endpoint calls that ruleset
        # `fruits`.
        target_mode = {'fruits': 'ctb'}.get(target_ruleset, target_ruleset)
        source_mode = beatmap['mode']
        # The beatmap endpoint reports the source difficulty. In stable,
        # `!mp map <id> <mode>` can create a Taiko/CTB/Mania convert from a
        # standard map. Accept only that explicit conversion; a map made for
        # another non-standard ruleset is still invalid for this pool.
        is_std_convert = (
            source_mode == 'osu'
            and source_mode != target_mode
            and ruleset.allow_std_converts
        )
        if source_mode != target_mode and not is_std_convert:
            mode_labels = {
                'std': 'STD', 'taiko': 'Taiko', 'ctb': 'CTB', 'mania': 'Mania', 'mania4k': 'Mania 4K', 'mania7k': 'Mania 7K',
                'osu': 'STD', 'fruits': 'CTB',
            }
            raise ValueError(
                f"Map `{beatmap_id}` is not a **{mode_labels[mode]}** map. "
                f"Its mode is **{mode_labels.get(source_mode, source_mode)}**."
            )
        is_convert = bool(beatmap.get('convert', False) or is_std_convert)
        stats = {
            'cs': beatmap['cs'], 'ar': beatmap['ar'], 'od': beatmap['od'],
            'bpm': beatmap['bpm'], 'length': beatmap['length'],
        }
        mods = self._slot_mods(slot) if mode == 'std' else []
        mod_label = ''
        if mode == 'std':
            stats, mod_label = self._apply_std_slot_mods(stats, slot)
        star_rating = beatmap['stars']
        if mods or is_std_convert:
            # osu!'s attributes endpoint accepts a target ruleset exactly
            # when the source map can be converted to it. That gives this
            # pool entry the correct target-mode SR rather than STD SR.
            star_rating = await osu_manager.get_beatmap_star_rating(
                beatmap_id, mods, ruleset=target_ruleset,
            )
        snapshot = {
            'artist': beatmap['artist'], 'title': beatmap['title'],
            'difficulty_name': beatmap['difficulty'], 'beatmapset_id': beatmap['set_id'],
            'source_mode': beatmap['mode'], 'target_mode': target_mode,
            'is_convert': is_convert, 'url': beatmap['url'], 'star_rating': star_rating,
            'stats': stats, 'mods': mods, 'mod_label': mod_label,
            'parsed_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc),
        }
        print(f"✅ Parsed {slot.upper()} · {beatmap['artist']} — {beatmap['title']}")
        return snapshot

    async def _create_pool_from_maps(
        self, name: str, mode: str, author_id: int, maps: list[tuple[str, int]], progress_target,
    ) -> tuple[int | None, str]:
        """Resolve map snapshots and persist a validated pool for either command type."""
        parsed_maps = []
        for index, (slot, beatmap_id) in enumerate(maps, start=1):
            parsed_maps.append((slot, beatmap_id, await self._parse_map_snapshot(slot, beatmap_id, mode)))
            await progress_target.edit(content=f"⏳ Creating **{name}**: parsing maps {index}/{len(maps)}…")
        pool_id, error = await create_pool_with_maps(name, mode, author_id, parsed_maps)
        return (pool_id if pool_id != -1 else None), error

    async def _create_pool_from_slash_fields(
        self, interaction: discord.Interaction, mode: str, name: str, **category_fields: str | None,
    ) -> None:
        if not 3 <= len(name.strip()) <= 64:
            await interaction.response.send_message("❌ Pool names must be between 3 and 64 characters.", ephemeral=True)
            return
        parsed_maps, parse_error = _maps_from_category_fields(mode, **category_fields)
        if parse_error:
            await interaction.response.send_message(parse_error, ephemeral=True)
            return
        is_valid, validation_error = validate_pool_maps(parsed_maps, mode)
        if not is_valid:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        progress = await interaction.followup.send(
            f"⏳ Creating **{name.strip()}**: parsing maps 0/{len(parsed_maps)}…", wait=True,
        )
        try:
            pool_id, error = await self._create_pool_from_maps(name.strip(), mode, interaction.user.id, parsed_maps, progress)
        except Exception as error:
            await progress.edit(content=f"❌ {error}")
            return
        if pool_id is None:
            await progress.edit(content=f"❌ Database error: {error}")
            return
        pool_maps = await get_pool_maps(pool_id)
        embed = discord.Embed(title="✅ Pool created successfully!", description=f"**{name.strip()}**", color=0x00ff00)
        embed.add_field(name="Mode", value=f"`{mode.upper()}`", inline=True)
        embed.add_field(name="Maps", value=f"`{len(pool_maps)}`", inline=True)
        embed.add_field(name="Status", value="✏️ Draft", inline=True)
        for field_name, value in await self._format_pool_cards(pool_maps, mode):
            embed.add_field(name=field_name, value=value, inline=False)
        # The creation response is the draft's first screen, so expose the
        # author-only Submit action here as well as in /pool_view.
        await progress.edit(
            content=None,
            embed=embed,
            view=PoolSubmitView(self, pool_id, interaction.user.id),
        )

    async def _edit_pool_map(
        self, pool_id: int, slot: str, beatmap_id: int, author: discord.abc.User,
    ) -> tuple[bool, str, discord.Embed | None]:
        """Validate, parse and save one map change for the slash edit command."""
        pool = await get_pool(pool_id)
        if not pool:
            return False, "❌ Pool not found.", None
        if pool['created_by'] != author.id:
            return False, "❌ Only the pool author can edit it.", None
        if pool['status'] not in ('draft', 'unranked'):
            return False, f"❌ A pool in `{pool['status']}` status cannot be edited.", None

        slot_clean = slot.strip().lower()
        category = 'tb' if slot_clean == 'tb' else ''.join(filter(str.isalpha, slot_clean))
        if category not in get_ruleset(pool['mode']).valid_categories:
            categories = ', '.join(f'`{item.upper()}`' for item in get_ruleset(pool['mode']).valid_categories)
            return False, f"❌ Invalid slot `{slot}` for {pool['mode'].upper()}. Allowed categories: {categories}", None
        if category == 'tb' and slot_clean != 'tb':
            return False, "❌ The tiebreaker must use the `TB` slot without a number.", None
        if category != 'tb' and not slot_clean[len(category):].isdigit():
            return False, f"❌ Include a slot number, for example `{category.upper()}1`.", None

        pool_maps = await get_pool_maps(pool_id)
        is_adding = slot_clean not in {item['slot'].lower() for item in pool_maps}
        try:
            snapshot = await self._parse_map_snapshot(slot_clean, beatmap_id, pool['mode'])
        except Exception as error:
            return False, f"❌ {error}", None
        if is_adding:
            success, error = await add_pool_map(pool_id, slot_clean, beatmap_id, snapshot)
        else:
            success, error = await update_pool_map(pool_id, slot_clean, beatmap_id, snapshot)
        if not success:
            return False, f"❌ Failed to save the map: {error}", None

        if pool['status'] == 'unranked':
            await update_pool_status(pool_id, 'draft', expected_status='unranked')
        await log_moderation_action(pool_id, 'add' if is_adding else 'edit', author.id, f"{slot_clean}→{beatmap_id}")
        updated_maps = await get_pool_maps(pool_id)
        valid, _ = validate_pool_maps([(item['slot'], item['beatmap_id']) for item in updated_maps], pool['mode'])
        action = "added" if is_adding else "updated"
        embed = discord.Embed(
            title=f"✅ Map {action} successfully",
            description=f"**{pool['name']}**",
            color=0x00ff00,
        )
        embed.add_field(name="Change", value=f"`{slot_clean.upper()}` → [osu.ppy.sh/b/{beatmap_id}](https://osu.ppy.sh/b/{beatmap_id})", inline=False)
        embed.add_field(name="Validation", value="✅ Pool meets the requirements" if valid else "⚠️ Required maps are still missing", inline=False)
        return True, "", embed

    async def pool_slot_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        pool_name = getattr(interaction.namespace, 'pool_name', None)
        if not pool_name:
            return []
        pool, _ = await self._resolve_pool_name(pool_name, interaction.user)
        if not pool:
            return []
        rules = get_ruleset(pool['mode'])
        existing = [item['slot'].upper() for item in await get_pool_maps(pool['pool_id'])]
        suggestions = [*existing]
        for category in rules.valid_categories:
            suggestions.append('TB' if category == 'tb' else f'{category.upper()}1')
        query = current.upper()
        return [app_commands.Choice(name=item, value=item.lower()) for item in dict.fromkeys(suggestions) if query in item][:25]

    async def pool_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        pools = await self._visible_pools(interaction.user)
        query = current.casefold()
        names = [pool['name'] for pool in pools if query in pool['name'].casefold()]
        return [app_commands.Choice(name=name, value=name) for name in dict.fromkeys(names)][:25]

    async def _visible_pools(
        self, user: discord.abc.User, *, mode: str | None = None, status: str | None = None,
    ) -> list[dict]:
        """Drafts are private; pending pools are visible to their author and moderators."""
        pools = await list_pools(mode, status)
        is_moderator = bool(getattr(user, 'guild_permissions', None) and user.guild_permissions.administrator)
        return [
            pool for pool in pools
            if pool['status'] in ('ranked', 'unranked')
            or pool['created_by'] == user.id
            or (pool['status'] == 'pending' and is_moderator)
        ]

    async def _resolve_pool_name(self, name: str, user: discord.abc.User) -> tuple[dict | None, str]:
        visible_ids = {pool['pool_id'] for pool in await self._visible_pools(user)}
        matches = [pool for pool in await get_pool_by_name(name) if pool['pool_id'] in visible_ids]
        if not matches:
            return None, "❌ Pool not found or unavailable."
        if len(matches) > 1:
            return None, "❌ Multiple accessible drafts have this name. Rename one of them."
        return matches[0], ""
    
    # === SHARED MAP FORMATTER FOR ALL COMMANDS ===
    async def _format_pool_cards(self, pool_maps: list, mode: str) -> list[tuple[str, str]]:
        """
        Format pool maps for embed display.
        Returns a list of (category_name, field_value) tuples.
        """
        category_data = {}
        
        for map_dict in pool_maps:
            slot = map_dict['slot'].upper()
            bm_id = map_dict['beatmap_id']
            try:
                snapshot = map_dict.get('snapshot')
                if not snapshot:
                    raise ValueError('This map has not been parsed yet. Update it with !pool-edit.')
                
                # Extract the category from the slot
                category = 'tb' if slot == 'TB' else ''.join(filter(str.isalpha, slot)).lower()
                
                # Build a readable Discord card. Slot categories are kept as
                # separate embed fields below, so every line can stay compact.
                display_stats = {
                    **snapshot['stats']
                }
                star_rating = snapshot['star_rating']

                minutes, secs = divmod(int(display_stats['length']), 60)
                formatted_length = f"{minutes:02d}:{secs:02d}"
                
                # Artist, title and difficulty match the common tournament
                # presentation; stats are deliberately compact for Discord.
                base_line = (
                    f"`{slot}` · [{snapshot['artist']} — {snapshot['title']} "
                    f"[{snapshot['difficulty_name']}]]({snapshot['url']})\n"
                    f"★ {star_rating:.2f} · {formatted_length} · {display_stats['bpm']:g} BPM · "
                    f"CS {display_stats['cs']:.1f} · AR {display_stats['ar']:.1f} · OD {display_stats['od']:.1f}"
                )
                
                card_line = base_line
                category_data.setdefault(category, []).append(card_line)
                
            except Exception as e:
                error_line = f"⚠️ `{slot}`: {str(e)[:60]}"
                category_data.setdefault('error', []).append(error_line)
        
        # Order is defined by the corresponding mode ruleset.  Do not keep a
        # second hard-coded order here: it would silently override rulesets.
        category_order = [*get_ruleset(mode).category_order, 'error']
        sorted_categories = [cat for cat in category_order if cat in category_data]
        sorted_categories.extend([cat for cat in category_data if cat not in sorted_categories])
        
        # Build embed fields
        fields = []
        for category in sorted_categories:
            cards = category_data[category]
            name = f"**{CATEGORY_FULL_NAMES.get(category, category.upper())}** ({len(cards)})"
            value = "\n".join(cards)
            if len(value) > 1024:
                value = value[:1021] + "..."
            fields.append((name, value))
        
        return fields
    
    @app_commands.command(name="pool_create", description="Create a STD, Taiko, or CTB pool")
    @app_commands.choices(mode=[
        app_commands.Choice(name="STD", value="std"),
        app_commands.Choice(name="Taiko", value="taiko"),
        app_commands.Choice(name="CTB", value="ctb"),
    ])
    @app_commands.describe(
        name="Pool name",
        nomod="NM — NoMod: beatmap IDs separated by spaces",
        hidden="HD — Hidden: beatmap IDs separated by spaces",
        hardrock="HR — Hard Rock: beatmap IDs separated by spaces",
        doubletime="DT — Double Time: beatmap IDs separated by spaces",
        freemods="FM — FreeMods: beatmap IDs separated by spaces",
        tiebreaker="TB — Tiebreaker: beatmap ID",
    )
    async def pool_create_slash(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str], name: str,
        nomod: str, hidden: str, hardrock: str, doubletime: str,
        tiebreaker: str, freemods: str | None = None,
    ):
        await self._create_pool_from_slash_fields(
            interaction, mode.value, name,
            nm=nomod, hd=hidden, hr=hardrock, dt=doubletime,
            fm=freemods, tb=tiebreaker,
        )

    @app_commands.command(name="pool_create_mania", description="Create a 4K or 7K Mania pool")
    @app_commands.choices(key_mode=[
        app_commands.Choice(name="4K", value="mania4k"),
        app_commands.Choice(name="7K", value="mania7k"),
    ])
    @app_commands.describe(
        key_mode="Mania key mode",
        name="Pool name",
        rice="RC — Rice: beatmap IDs separated by spaces",
        hybrids="HB — Hybrids: beatmap IDs separated by spaces",
        longnotes="LN — Long Notes: beatmap IDs separated by spaces",
        speedvariations="SV — Speed Variations: beatmap IDs separated by spaces",
        extreme="EX — Extreme: beatmap IDs separated by spaces",
        tiebreaker="TB — Tiebreaker: beatmap ID",
    )
    async def pool_create_mania(
        self, interaction: discord.Interaction, key_mode: app_commands.Choice[str], name: str, rice: str,
        hybrids: str, longnotes: str, tiebreaker: str,
        speedvariations: str | None = None, extreme: str | None = None,
    ):
        await self._create_pool_from_slash_fields(
            interaction, key_mode.value, name, rc=rice, hb=hybrids,
            ln=longnotes, sv=speedvariations, ex=extreme, tb=tiebreaker,
        )

    @app_commands.command(name="pool_edit", description="Add or replace a map in a pool")
    @app_commands.describe(pool_name="Exact pool name", slot="Slot, for example NM1 or TB", beatmap_id="osu! beatmap ID")
    @app_commands.autocomplete(pool_name=pool_name_autocomplete)
    @app_commands.autocomplete(slot=pool_slot_autocomplete)
    async def pool_edit_slash(self, interaction: discord.Interaction, pool_name: str, slot: str, beatmap_id: int):
        await interaction.response.defer(thinking=True, ephemeral=True)
        pool, error = await self._resolve_pool_name(pool_name, interaction.user)
        if pool is None:
            await interaction.followup.send(error, ephemeral=True)
            return
        success, message, embed = await self._edit_pool_map(pool['pool_id'], slot, beatmap_id, interaction.user)
        if not success:
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="pool_delete", description="Delete your own draft")
    @app_commands.describe(pool_name="Exact draft name")
    @app_commands.autocomplete(pool_name=pool_name_autocomplete)
    async def pool_delete_slash(self, interaction: discord.Interaction, pool_name: str):
        pool, error = await self._resolve_pool_name(pool_name, interaction.user)
        if pool is None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if pool['created_by'] != interaction.user.id:
            await interaction.response.send_message("❌ Only the pool author can delete a draft.", ephemeral=True)
            return
        if pool['status'] != 'draft':
            await interaction.response.send_message("❌ Only pools in Draft status can be deleted.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Delete draft **{pool['name']}** permanently?",
            view=DraftDeleteView(self, pool['pool_id'], interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="pool_list", description="List available pools")
    @app_commands.describe(mode="Filter by mode", status="Filter by status")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="STD", value="std"), app_commands.Choice(name="Taiko", value="taiko"),
            app_commands.Choice(name="CTB", value="ctb"),
            app_commands.Choice(name="Mania 4K", value="mania4k"),
            app_commands.Choice(name="Mania 7K", value="mania7k"),
        ],
        status=[
            app_commands.Choice(name="Draft", value="draft"), app_commands.Choice(name="Pending", value="pending"),
            app_commands.Choice(name="Ranked", value="ranked"), app_commands.Choice(name="Unranked", value="unranked"),
        ],
    )
    async def pool_list_slash(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None,
        status: app_commands.Choice[str] | None = None,
    ):
        pools = await self._visible_pools(interaction.user, mode=mode.value if mode else None, status=status.value if status else None)
        if not pools:
            await interaction.response.send_message("📭 No matching pools found.", ephemeral=True)
            return
        labels = {'draft': '✏️ Draft', 'pending': '⏳ Pending', 'ranked': '✅ Ranked', 'unranked': '❌ Unranked'}
        embed = discord.Embed(title="📋 Pools", color=0x0099ff)
        for pool in pools[:25]:
            embed.add_field(
                name=pool['name'],
                value=f"{pool['mode'].upper()} · {labels.get(pool['status'], pool['status'])} · {len(pool.get('maps', []))} maps",
                inline=False,
            )
        if len(pools) > 25:
            embed.set_footer(text=f"Showing the first 25 of {len(pools)} pools.")
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="pool_view", description="View pool details")
    @app_commands.describe(pool_name="Exact pool name")
    @app_commands.autocomplete(pool_name=pool_name_autocomplete)
    async def pool_view_slash(self, interaction: discord.Interaction, pool_name: str):
        """Slash-command pool view; drafts include a private author-only Submit button."""
        # Slash commands must acknowledge Discord within three seconds.  The
        # actual pool read can involve the database, so defer first.
        await interaction.response.defer(thinking=True)
        try:
            pool, error = await self._resolve_pool_name(pool_name, interaction.user)
            if pool is None:
                await interaction.followup.send(error, ephemeral=True)
                return
            is_moderator_channel = interaction.channel_id == self.MODERATION_CHANNEL_ID
            embed, pool = await self._pool_view_embed(
                pool['pool_id'], include_moderation_history=is_moderator_channel,
            )
            if embed is None or pool is None:
                await interaction.followup.send("❌ Pool not found.", ephemeral=True)
                return

            view = PoolSubmitView(self, pool['pool_id'], pool['created_by']) if pool['status'] == 'draft' else None
            private_view = pool['status'] in ('draft', 'pending')
            if view is None:
                await interaction.followup.send(embed=embed, ephemeral=private_view)
            else:
                await interaction.followup.send(embed=embed, view=view, ephemeral=private_view)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "❌ Could not open the pool. The error was logged.", ephemeral=True
            )
    
    @app_commands.command(name="pool_formats", description="Show current pool requirements")
    @app_commands.choices(mode=[
        app_commands.Choice(name="STD", value="std"),
        app_commands.Choice(name="Taiko", value="taiko"),
        app_commands.Choice(name="CTB", value="ctb"),
            app_commands.Choice(name="Mania 4K", value="mania4k"),
            app_commands.Choice(name="Mania 7K", value="mania7k"),
    ])
    async def pool_formats_slash(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        full_names = {'std': 'osu! Standard', 'taiko': 'osu! Taiko', 'ctb': 'osu! Catch', 'mania': 'osu! Mania 4K', 'mania4k': 'osu! Mania 4K', 'mania7k': 'osu! Mania 7K'}
        embed = discord.Embed(
            title=f"📋 Pool requirements: {full_names[mode.value]}",
            description=format_category_requirements(mode.value),
            color=0x0099ff,
        )
        # Keep private Draft/Pending entries out of public channels.
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pool_help", description="Show pool command help")
    async def pool_help_slash(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛠 Pool management",
            description="Pools start as **Draft** and can be submitted for moderation with the Submit button.",
            color=0x0099ff,
        )
        embed.add_field(
            name="Creation",
            value="`/pool_create` — STD, Taiko, or CTB\n"
                  "`/pool_create_mania` — Mania",
            inline=False,
        )
        embed.add_field(
            name="Pool actions",
            value="`/pool_list` — list pools and filter them\n"
                  "`/pool_view` — view a pool by name\n"
                  "`/pool_edit` — add or replace a map\n"
                  "`/pool_delete` — delete your own Draft",
            inline=False,
        )
        embed.add_field(
            name="Moderation",
            value="Open a Draft with `/pool_view` and press **Submit**.\n"
                  "Moderators use the **Rank** and **Unrank** buttons.\n"
                  "Moderation buttons survive bot restarts.",
            inline=False,
        )
        embed.add_field(
            name="Rules",
            value="`/pool_formats` — current categories and minimum mode requirements.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pool_repost_pending", description="Repost pending pools to the moderation channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def pool_repost_pending_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        pending_pools = await get_pools_by_status('pending')
        if not pending_pools:
            await interaction.followup.send("📭 No pending pools found.", ephemeral=True)
            return
        succeeded, failures = [], []
        for pool in pending_pools:
            success, error = await self.repost_pending_pool(pool)
            (succeeded if success else failures).append(pool['name'] if success else f"{pool['name']} — {error}")
        message = f"✅ Reposted: {len(succeeded)}."
        if failures:
            message += f"\n❌ Errors ({len(failures)}):\n" + "\n".join(failures[:10])
        await interaction.followup.send(message, ephemeral=True)
    
async def setup(bot: commands.Bot):
    await bot.add_cog(PoolCommands(bot))
