import json
import os
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_FILE = "guild_config.json"


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_config(guild_id: int) -> dict:
    data = load_config()
    return data.get(str(guild_id), {})


def set_guild_config(guild_id: int, key: str, value):
    data = load_config()
    data.setdefault(str(guild_id), {})[key] = value
    save_config(data)


class Automation(commands.Cog):
    """자동 역할 부여, 입장/퇴장 알림, 로그 채널 등 자동화 기능 (전부 슬래시(/) 명령어 전용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- 설정 명령어 ----------
    @app_commands.command(name="환영채널설정", description="환영 메시지를 보낼 채널을 설정합니다.")
    @app_commands.describe(channel="환영 메시지를 보낼 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_config(interaction.guild.id, "welcome_channel", channel.id)
        await interaction.response.send_message(f"✅ 환영 메시지 채널을 {channel.mention} 으로 설정했어요.")

    @app_commands.command(name="퇴장채널설정", description="퇴장 메시지를 보낼 채널을 설정합니다.")
    @app_commands.describe(channel="퇴장 메시지를 보낼 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def setleave(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_config(interaction.guild.id, "leave_channel", channel.id)
        await interaction.response.send_message(f"✅ 퇴장 메시지 채널을 {channel.mention} 으로 설정했어요.")

    @app_commands.command(name="로그채널설정", description="관리 로그(입장/퇴장/삭제 등)를 보낼 채널을 설정합니다.")
    @app_commands.describe(channel="로그를 보낼 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_config(interaction.guild.id, "log_channel", channel.id)
        await interaction.response.send_message(f"✅ 로그 채널을 {channel.mention} 으로 설정했어요.")

    @app_commands.command(name="티켓생성", description="지정한 멤버와 관리자만 볼 수 있는 티켓 채널을 생성합니다.")
    @app_commands.describe(member="티켓을 생성할 멤버", category="티켓 채널을 넣을 카테고리")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_ticket(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        category: discord.CategoryChannel | None = None,
    ):
        safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]", "-", member.display_name).strip("-")
        channel_name = f"티켓-{safe_name or member.id}"[:100]
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        try:
            channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"티켓 대상: {member} ({member.id})",
                reason=f"관리자 {interaction.user}가 티켓 생성",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ 봇에게 채널 관리 권한이 없어 티켓을 만들 수 없어요.", ephemeral=True
            )
        await channel.send(f"{member.mention} 님의 티켓이 생성됐어요. 문의 내용을 남겨주세요.")
        await interaction.response.send_message(
            f"✅ 티켓 채널 {channel.mention}을 생성했어요.", ephemeral=True
        )

    # ---------- 슬래시 명령어 에러 처리 ----------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ 이 명령어를 실행할 권한이 없어요."
        else:
            msg = "❌ 알 수 없는 오류가 발생했어요."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ---------- 이벤트: 멤버 입장 ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = get_guild_config(member.guild.id)

        # 자동 역할 부여
        autorole_id = config.get("autorole")
        if autorole_id:
            role = member.guild.get_role(autorole_id)
            if role:
                try:
                    await member.add_roles(role, reason="자동 역할 부여")
                except discord.Forbidden:
                    pass

        # 환영 메시지
        welcome_channel_id = config.get("welcome_channel")
        if welcome_channel_id:
            channel = member.guild.get_channel(welcome_channel_id)
            if channel:
                embed = discord.Embed(
                    description=f"🎉 {member.mention} 님, **{member.guild.name}** 서버에 오신 것을 환영해요!",
                    color=discord.Color.green(),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"현재 멤버 수: {member.guild.member_count}명")
                await channel.send(embed=embed)

        # 로그
        await self._log(
            member.guild,
            f"📥 **입장** {member} ({member.id}) - 계정 생성일: {member.created_at.strftime('%Y-%m-%d')}",
        )

    # ---------- 이벤트: 멤버 퇴장 ----------
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        config = get_guild_config(member.guild.id)

        leave_channel_id = config.get("leave_channel")
        if leave_channel_id:
            channel = member.guild.get_channel(leave_channel_id)
            if channel:
                embed = discord.Embed(
                    description=f"👋 **{member}** 님이 서버를 떠났어요.",
                    color=discord.Color.dark_grey(),
                )
                embed.set_footer(text=f"현재 멤버 수: {member.guild.member_count}명")
                await channel.send(embed=embed)

        await self._log(member.guild, f"📤 **퇴장** {member} ({member.id})")

    # ---------- 이벤트: 메시지 삭제 로그 ----------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        content = message.content or "(내용 없음/첨부파일)"
        await self._log(
            message.guild,
            f"🗑️ **메시지 삭제** {message.author} in #{message.channel.name}\n> {content[:200]}",
        )

    async def _log(self, guild: discord.Guild, text: str):
        config = get_guild_config(guild.id)
        log_channel_id = config.get("log_channel")
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if channel:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            await channel.send(f"`[{timestamp}]` {text}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Automation(bot))