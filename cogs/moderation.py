import logging
import discord
from discord import app_commands
from discord.ext import commands
from discord_helpers import purge_messages

logger = logging.getLogger(__name__)


class Moderation(commands.Cog):
    """서버 관리(모더레이션) 관련 명령어 모음 (전부 슬래시 명령어(/)로만 작동)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- 메시지 대량 삭제 ----------
    @app_commands.command(name="메시지삭제", description="최근 메시지를 지정한 개수만큼 삭제합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(amount="삭제할 메시지 개수(1~100)")
    @app_commands.checks.has_permissions(administrator=True)
    async def purge(self, interaction: discord.Interaction, amount: int):
        await purge_messages(interaction, amount)

    # ---------- 슬래시 명령어 에러 처리 ----------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ 이 명령어를 실행할 권한이 없어요."
        else:
            logger.error("모더레이션 명령 실패", exc_info=error)
            msg = "❌ 알 수 없는 오류가 발생했어요."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))