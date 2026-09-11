import os
import asyncio
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
from storage import BASE_DIR
import aion2_scraper
from server_scope import ALLOWED_GUILD_ID, allowed_guild

# .env 파일에서 환경변수 로드 (봇 토큰 등)
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")

# ----- 로깅 설정 -----
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# ----- 인텐트 설정 -----
# 서버 관리 봇은 멤버 입장/퇴장, 메시지 내용 등을 감지해야 하므로
# 디스코드 개발자 포털에서 아래 인텐트를 반드시 활성화해야 합니다.
# (Bot 설정 > Privileged Gateway Intents > SERVER MEMBERS INTENT, MESSAGE CONTENT INTENT)
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

class ServerCommandTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction):
        return interaction.guild_id == ALLOWED_GUILD_ID


class ServerBot(commands.Bot):
    @property
    def guilds(self):
        # Background jobs only iterate over the allowed guild, even if leaving fails.
        return [guild for guild in super().guilds if allowed_guild(guild)]

    def dispatch(self, event, /, *args, **kwargs):
        if event in {"member_join", "member_remove", "message_delete", "voice_state_update", "message"}:
            if not args or not allowed_guild(getattr(args[0], "guild", None)):
                return
        super().dispatch(event, *args, **kwargs)

    async def enforce_server_scope(self):
        for guild in list(super().guilds):
            if not allowed_guild(guild):
                await self.leave_unapproved_guild(guild)

    async def leave_unapproved_guild(self, guild):
        try:
            await guild.leave()
            logger.warning("허용하지 않은 서버에서 탈퇴: %s (%s)", guild.name, guild.id)
        except discord.HTTPException:
            logger.exception("서버 탈퇴 실패 (해당 서버 기능은 차단됨): %s", guild.id)

    async def setup_hook(self):
        await load_extensions()
        target = discord.Object(id=ALLOWED_GUILD_ID)
        self.tree.copy_global_to(guild=target)
        synced = await self.tree.sync(guild=target)
        self.tree.clear_commands(guild=None)
        await self.tree.sync()  # Remove previously registered global commands.
        logger.info("허용 서버 %s에 슬래시 명령어 %s개 동기화 완료", ALLOWED_GUILD_ID, len(synced))

    async def close(self):
        try:
            for extension in list(self.extensions):
                await self.unload_extension(extension)
            await super().close()
        finally:
            await aion2_scraper.close_browser()


bot = ServerBot(command_prefix="!", intents=intents, help_command=None,
                tree_cls=ServerCommandTree,
                allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_ready():
    await bot.enforce_server_scope()
    logger.info(f"{bot.user} 로 로그인 완료 (ID: {bot.user.id})")
    logger.info("허용 서버 %s에서만 작동합니다 (연결된 허용 서버: %s개).", ALLOWED_GUILD_ID, len(bot.guilds))


@bot.event
async def on_guild_join(guild):
    if not allowed_guild(guild):
        await bot.leave_unapproved_guild(guild)


@bot.event
async def on_command_error(ctx, error):
    """일반 명령어 처리 중 발생하는 에러를 사용자에게 보기 좋게 전달합니다."""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 이 명령어를 실행할 권한이 없어요.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ 봇에게 이 작업을 수행할 권한이 없어요. 서버 설정에서 권한을 확인해주세요.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ 필수 인자가 빠졌어요. `!help {ctx.command}` 로 사용법을 확인하세요.")
    elif isinstance(error, commands.CommandNotFound):
        return  # 존재하지 않는 명령어는 조용히 무시
    else:
        logger.exception("처리되지 않은 에러", exc_info=error)
        await ctx.send("❌ 알 수 없는 오류가 발생했어요.")


async def load_extensions():
    """cogs 폴더와 루트의 인증 확장을 불러옵니다."""
    for filename in sorted(os.listdir(BASE_DIR / "cogs")):
        if filename.endswith(".py") and not filename.startswith("_"):
            extension = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(extension)
                logger.info(f"✅ 로드됨: {extension}")
            except Exception as e:
                logger.exception("확장 로딩 실패: %s", extension)
                raise

    try:
        await bot.load_extension("verify")
        logger.info("✅ 로드됨: verify")
    except Exception as e:
        logger.exception("인증 확장 로딩 실패")
        raise


async def main():
    if not TOKEN:
        logger.error("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        return
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
