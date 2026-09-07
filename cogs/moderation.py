import discord
from discord import app_commands
from discord.ext import commands


class Moderation(commands.Cog):
    """서버 관리(모더레이션) 관련 명령어 모음 (전부 슬래시 명령어(/)로만 작동)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- 메시지 대량 삭제 ----------
    @app_commands.command(name="메시지삭제", description="최근 메시지를 지정한 개수만큼 삭제합니다.")
    @app_commands.describe(amount="삭제할 메시지 개수(1~100)")
    @app_commands.checks.has_permissions(administrator=True)
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