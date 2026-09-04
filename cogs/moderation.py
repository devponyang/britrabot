import datetime
import json
import os

import discord
from discord import app_commands
from discord.ext import commands

WARN_FILE = "warnings.json"


def load_warnings() -> dict:
    if not os.path.exists(WARN_FILE):
        return {}
    with open(WARN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_warnings(data: dict):
    with open(WARN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Moderation(commands.Cog):
    """서버 관리(모더레이션) 관련 명령어 모음 (전부 슬래시 명령어(/)로만 작동)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- 경고 부여 ----------
    @app_commands.command(name="warn", description="멤버에게 경고를 부여합니다.")
    @app_commands.describe(member="대상 멤버", reason="경고 사유")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "사유 없음",
    ):
        data = load_warnings()
        guild_id = str(interaction.guild.id)
        user_id = str(member.id)
        data.setdefault(guild_id, {}).setdefault(user_id, [])
        data[guild_id][user_id].append(
            {
                "reason": reason,
                "moderator": str(interaction.user),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            }
        )
        save_warnings(data)
        count = len(data[guild_id][user_id])
        embed = discord.Embed(title="⚠️ 경고 부여", color=discord.Color.yellow())
        embed.add_field(name="대상", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="사유", value=reason, inline=False)
        embed.add_field(name="누적 경고", value=f"{count}회", inline=False)
        await interaction.response.send_message(embed=embed)

    # ---------- 경고 내역 조회 ----------
    @app_commands.command(name="warnings", description="멤버의 경고 내역을 확인합니다.")
    @app_commands.describe(member="대상 멤버")
    async def warnings_cmd(self, interaction: discord.Interaction, member: discord.Member):
        data = load_warnings()
        guild_id = str(interaction.guild.id)
        user_id = str(member.id)
        records = data.get(guild_id, {}).get(user_id, [])
        if not records:
            return await interaction.response.send_message(
                f"{member.mention} 님은 경고 내역이 없어요.", ephemeral=True
            )
        embed = discord.Embed(
            title=f"{member} 님의 경고 내역 ({len(records)}건)", color=discord.Color.yellow()
        )
        for i, r in enumerate(records, start=1):
            embed.add_field(
                name=f"#{i} - {r['timestamp'][:10]}",
                value=f"사유: {r['reason']}\n담당: {r['moderator']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # ---------- 경고 초기화 ----------
    @app_commands.command(name="clearwarnings", description="멤버의 경고 내역을 모두 삭제합니다.")
    @app_commands.describe(member="대상 멤버")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        data = load_warnings()
        guild_id = str(interaction.guild.id)
        user_id = str(member.id)
        if guild_id in data and user_id in data[guild_id]:
            del data[guild_id][user_id]
            save_warnings(data)
        await interaction.response.send_message(
            f"✅ {member.mention} 님의 경고 내역을 초기화했어요."
        )

    # ---------- 메시지 대량 삭제 ----------
    @app_commands.command(name="purge", description="최근 메시지를 지정한 개수만큼 삭제합니다.")
    @app_commands.describe(amount="삭제할 메시지 개수(1~100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int):
        if not interaction.guild.me.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                "❌ 봇에게 메시지 관리 권한이 없어요.", ephemeral=True
            )
        amount = max(1, min(amount, 100))
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🧹 메시지 {len(deleted)}개를 삭제했어요.", ephemeral=True)

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))