"""Join-to-create voice channels, tracked separately from permanent channels."""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import BASE_DIR, load_json, save_json

logger = logging.getLogger(__name__)
VOICE_CONFIG_FILE = BASE_DIR / "voice_rooms.json"


class VoiceRooms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._locks = {}
        self._last_created = {}
        self._last_renamed = {}

    def lock(self, guild_id):
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def config(self, guild_id):
        return load_json(VOICE_CONFIG_FILE).get(str(guild_id), {})

    def save(self, guild_id, config):
        data = load_json(VOICE_CONFIG_FILE)
        data[str(guild_id)] = config
        save_json(VOICE_CONFIG_FILE, data)

    async def cog_load(self):
        self.cleanup_loop.start()

    async def cog_unload(self):
        task = self.cleanup_loop.get_task()
        self.cleanup_loop.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    @app_commands.command(name="개인음성설정", description="입장하면 개인 음성방을 만드는 채널을 지정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="사진의 통화방 생성처럼 입장용으로 사용할 음성채널", enabled="자동 생성을 켜거나 끕니다")
    async def configure(self, interaction: discord.Interaction, channel: discord.VoiceChannel, enabled: bool = True):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock(interaction.guild.id):
            cfg = self.config(interaction.guild.id)
            if str(channel.id) in cfg.get("rooms", {}):
                return await interaction.followup.send("자동 생성된 개인방을 입장 채널로 지정할 수 없어요.", ephemeral=True)
            permissions = channel.permissions_for(interaction.guild.me)
            if enabled and not (permissions.manage_channels and permissions.move_members and permissions.connect):
                return await interaction.followup.send("봇에게 채널 관리·멤버 이동·연결 권한을 부여해주세요.", ephemeral=True)
            cfg.update(trigger_channel_id=channel.id, enabled=enabled)
            self.save(interaction.guild.id, cfg)
        await interaction.followup.send(
            f"✅ {channel.mention} 개인 음성방 생성을 {'켰어요' if enabled else '껐어요'}.\n"
            "입장하면 같은 카테고리에 개인방을 만들고 이동합니다. 방 주인은 `/내음성방이름`으로 이름을 바꿀 수 있어요. 아무도 없으면 자동 정리됩니다.",
            ephemeral=True,
        )

    @app_commands.command(name="내음성방이름", description="내가 만든 개인 음성방의 이름을 변경합니다.")
    @app_commands.guild_only()
    @app_commands.describe(name="새 음성방 이름 (1~100자)")
    async def rename(self, interaction: discord.Interaction, name: str):
        name = " ".join(name.split())
        if not 1 <= len(name) <= 100:
            return await interaction.response.send_message("방 이름은 1~100자로 입력해주세요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock(interaction.guild.id):
            channel = interaction.user.voice.channel if interaction.user.voice else None
            cfg = self.config(interaction.guild.id)
            owner = cfg.get("rooms", {}).get(str(channel.id)) if channel else None
            if owner != interaction.user.id:
                return await interaction.followup.send("내가 만든 개인 음성방에 입장한 상태에서 사용해주세요.", ephemeral=True)
            remaining = 310 - (time.monotonic() - self._last_renamed.get(channel.id, -1000))
            if remaining > 0:
                return await interaction.followup.send(f"잦은 이름 변경을 막기 위해 {int(remaining) + 1}초 후 다시 변경할 수 있어요.", ephemeral=True)
            # Bound API waits so a rate-limited rename cannot freeze all room operations.
            async with asyncio.timeout(20):
                await channel.edit(name=name, reason=f"개인 음성방 소유자 {interaction.user.id}의 이름 변경")
            self._last_renamed[channel.id] = time.monotonic()
        await interaction.followup.send(f"✅ 음성방 이름을 **{discord.utils.escape_markdown(name)}**으로 변경했어요.", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        try:
            async with self.lock(member.guild.id):
                cfg = self.config(member.guild.id)
                trigger_id = cfg.get("trigger_channel_id")
                if cfg.get("enabled") and after.channel and after.channel.id == trigger_id:
                    await self.create_room(member, after.channel)
                if before.channel:
                    await self.remove_empty(member.guild, before.channel.id)
        except Exception:
            logger.exception("개인 음성방 처리 실패: guild=%s user=%s", member.guild.id, member.id)

    async def create_room(self, member, trigger):
        if not member.voice or member.voice.channel != trigger:
            return
        guild = member.guild
        cfg = self.config(guild.id)
        rooms = cfg.setdefault("rooms", {})
        for channel_id, owner_id in rooms.items():
            existing = guild.get_channel(int(channel_id))
            if owner_id == member.id and existing:
                await member.move_to(existing, reason="기존 개인 음성방으로 이동")
                return
        key = (guild.id, member.id)
        if time.monotonic() - self._last_created.get(key, -1000) < 30:
            return
        self._last_created[key] = time.monotonic()
        self._last_created = {k: t for k, t in self._last_created.items() if time.monotonic() - t < 60}
        if len(rooms) >= 50:
            logger.warning("개인 음성방 한도(50개): guild=%s", guild.id)
            return
        # Preserve channel-specific restrictions as well as category defaults.
        overwrites = trigger.overwrites.copy()
        owner_permissions = overwrites.get(member, discord.PermissionOverwrite())
        owner_permissions.update(view_channel=True, connect=True, speak=True, send_messages=True)
        overwrites[member] = owner_permissions
        bot_permissions = overwrites.get(guild.me, discord.PermissionOverwrite())
        bot_permissions.update(view_channel=True, connect=True, manage_channels=True, move_members=True, send_messages=True)
        overwrites[guild.me] = bot_permissions
        channel = await guild.create_voice_channel(
            name=f"🔊 {member.display_name}의 음성방"[:100], category=trigger.category,
            overwrites=overwrites, user_limit=trigger.user_limit,
            bitrate=min(trigger.bitrate, guild.bitrate_limit), reason=f"개인 음성방 생성: {member.id}",
        )
        try:
            rooms[str(channel.id)] = member.id
            self.save(guild.id, cfg)
            # The member may have disconnected during channel creation.
            if not member.voice or member.voice.channel != trigger:
                await self.remove_empty(guild, channel.id)
                return
            await member.move_to(channel, reason="개인 음성방으로 이동")
        except Exception:
            if not channel.members:
                try:
                    await channel.delete(reason="개인 음성방 생성 실패 정리")
                    latest = self.config(guild.id)
                    latest.get("rooms", {}).pop(str(channel.id), None)
                    self.save(guild.id, latest)
                except Exception:
                    logger.exception("개인 음성방 실패 후 정리 오류: channel=%s", channel.id)
            raise
        try:
            await channel.send("🔊 개인 음성방이 생성됐어요.\n방을 만든 사람은 `/내음성방이름 이름`으로 이름을 바꿀 수 있어요.\n아무도 남지 않으면 이 방은 자동으로 삭제됩니다.")
        except discord.HTTPException:
            logger.warning("개인 음성방 안내 전송 실패: channel=%s", channel.id)

    async def remove_empty(self, guild, channel_id):
        cfg = self.config(guild.id)
        rooms = cfg.get("rooms", {})
        if str(channel_id) not in rooms or channel_id == cfg.get("trigger_channel_id"):
            return
        channel = guild.get_channel(channel_id)
        if channel and channel.members:
            return
        if channel:
            await channel.delete(reason="비어 있는 자동 생성 개인 음성방 정리")
        rooms.pop(str(channel_id), None)
        self._last_renamed.pop(channel_id, None)
        self.save(guild.id, cfg)

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        for guild in self.bot.guilds:
            try:
                async with self.lock(guild.id):
                    for channel_id in list(self.config(guild.id).get("rooms", {})):
                        try:
                            await self.remove_empty(guild, int(channel_id))
                        except discord.HTTPException:
                            logger.exception("빈 개인 음성방 정리 실패: channel=%s", channel_id)
            except Exception:
                logger.exception("개인 음성방 정리 실패: guild=%s", guild.id)

    @cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction, error):
        logger.error("개인 음성방 명령 오류", exc_info=error)
        message = "⚠️ 처리하지 못했어요. 봇 권한을 확인하고 잠시 후 다시 시도해주세요."
        if isinstance(error, app_commands.MissingPermissions):
            message = "관리자만 입장 채널을 설정할 수 있어요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(VoiceRooms(bot))
