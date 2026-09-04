import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

# .env 파일에서 환경변수 로드 (봇 토큰 등)
load_dotenv()

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
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=commands.DefaultHelpCommand())


@bot.event
async def on_ready():
    logger.info(f"{bot.user} 로 로그인 완료 (ID: {bot.user.id})")
    logger.info(f"현재 {len(bot.guilds)}개 서버에서 작동 중입니다.")
    try:
        synced = await bot.tree.sync()
        logger.info(f"슬래시 명령어 {len(synced)}개 동기화 완료")
    except Exception as e:
        logger.error(f"슬래시 명령어 동기화 실패: {e}")


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
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("_"):
            extension = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(extension)
                logger.info(f"✅ 로드됨: {extension}")
            except Exception as e:
                logger.error(f"❌ 로드 실패: {extension} - {e}")

    try:
        await bot.load_extension("verify")
        logger.info("✅ 로드됨: verify")
    except Exception as e:
        logger.error(f"❌ 로드 실패: verify - {e}")


async def main():
    if not TOKEN:
        logger.error("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        return
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
