import json
import os
import random
import string
import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands

import aion2_scraper

CODE_FILE = "verify_codes.json"
CONFIG_FILE = "verify_config.json"

DEFAULT_ARTICLE_URL = (
    "https://aion2.plaync.com/ko-kr/board/server/view"
    "?articleId=6a9a6c96feeef62e67566500&categoryId=69094e85a7d3dc347cdf1e18"
)
DEFAULT_TARGET_SERVER = "브리트라"
AUTOMATION_CONFIG_FILE = "guild_config.json"
MAX_VERIFY_ATTEMPTS = 3
VERIFY_COOLDOWN_SECONDS = 10 * 60


# ---------------- 저장소 헬퍼 ----------------
def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_config(guild_id: int) -> dict:
    data = _load(CONFIG_FILE)
    cfg = data.get(str(guild_id), {})
    cfg.setdefault("article_url", DEFAULT_ARTICLE_URL)
    cfg.setdefault("target_server", DEFAULT_TARGET_SERVER)
    cfg.setdefault("role_id", None)
    return cfg


def set_guild_config(guild_id: int, key: str, value):
    data = _load(CONFIG_FILE)
    data.setdefault(str(guild_id), {})[key] = value
    _save(CONFIG_FILE, data)


def generate_code() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=8))


def save_pending_code(user_id: int, guild_id: int, code: str):
    data = _load(CODE_FILE)
    previous = data.get(str(user_id), {})
    data[str(user_id)] = {
        "code": code,
        "guild_id": guild_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "verified": previous.get("verified", False),
        "attempts": previous.get("attempts", 0),
        "cooldown_until": previous.get("cooldown_until", 0),
    }
    _save(CODE_FILE, data)


def get_pending_code(user_id: int):
    data = _load(CODE_FILE)
    return data.get(str(user_id))


def mark_verified(user_id: int):
    data = _load(CODE_FILE)
    if str(user_id) in data:
        data[str(user_id)]["verified"] = True
        _save(CODE_FILE, data)


def get_cooldown_remaining(user_id: int) -> int:
    pending = get_pending_code(user_id)
    if not pending:
        return 0
    return max(0, int(float(pending.get("cooldown_until", 0)) - time.time()))


def record_verification_failure(user_id: int) -> tuple[int, int]:
    data = _load(CODE_FILE)
    pending = data.get(str(user_id), {})
    attempts = int(pending.get("attempts", 0)) + 1
    cooldown_until = 0
    if attempts >= MAX_VERIFY_ATTEMPTS:
        cooldown_until = time.time() + VERIFY_COOLDOWN_SECONDS
    pending["attempts"] = attempts
    pending["cooldown_until"] = cooldown_until
    data[str(user_id)] = pending
    _save(CODE_FILE, data)
    return attempts, int(max(0, cooldown_until - time.time()))


async def log_verification(guild: discord.Guild, message: str):
    config = _load(AUTOMATION_CONFIG_FILE).get(str(guild.id), {})
    channel_id = config.get("log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        await channel.send(f"🔐 {message}")


async def send_verification_failure(
    interaction: discord.Interaction, message: str, reason: str
):
    attempts, cooldown = record_verification_failure(interaction.user.id)
    await log_verification(
        interaction.guild,
        f"실패: {interaction.user} ({interaction.user.id}) - {reason} ({attempts}/{MAX_VERIFY_ATTEMPTS})",
    )
    if cooldown:
        message += "\n⚠️ 실패 횟수를 초과해 10분 동안 재시도할 수 없어요."
    return await interaction.followup.send(message, ephemeral=True)


# ---------------- 버튼 UI ----------------
class VerifyPanelView(discord.ui.View):
    """인증 센터에 올라가는 '인증진행' 버튼 (영구 View)"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="인증진행", style=discord.ButtonStyle.success, custom_id="verify:start"
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = get_pending_code(interaction.user.id)
        if pending and pending.get("verified"):
            return await interaction.response.send_message(
                "✅ 이미 인증된 사용자예요.", ephemeral=True
            )
        cooldown = get_cooldown_remaining(interaction.user.id)
        if cooldown:
            return await interaction.response.send_message(
                f"⚠️ 인증 실패 횟수를 초과했어요. {max(1, (cooldown + 59) // 60)}분 후 다시 시도해주세요.",
                ephemeral=True,
            )
        code = generate_code()
        save_pending_code(interaction.user.id, interaction.guild.id, code)
        config = get_guild_config(interaction.guild.id)

        embed = discord.Embed(title="디스코드 인증 요청", color=discord.Color.blurple())
        embed.description = (
            "아래 인증 코드를 복사하여 홈페이지 인증게시판에 댓글로 작성해주세요.\n"
            "대표 캐릭터를 반드시 확인 부탁드립니다.\n\n"
            "댓글 작성이 완료되면 아래의 **댓글 작성 완료 (다음)** 버튼을 눌러주세요."
        )
        embed.add_field(name="🔑 발급된 인증 코드", value=f"`{code}`", inline=False)

        await interaction.response.send_message(
            embed=embed, view=VerifyCodeView(config["article_url"]), ephemeral=True
        )


class VerifyCodeView(discord.ui.View):
    """코드 발급 후 보여주는 '인증게시판으로 이동' + '댓글 작성 완료' 버튼 (영구 View)"""

    def __init__(self, article_url: str = DEFAULT_ARTICLE_URL):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="인증게시판으로 이동",
                style=discord.ButtonStyle.link,
                url=article_url,
            )
        )

    @discord.ui.button(
        label="댓글 작성 완료 (다음)",
        style=discord.ButtonStyle.primary,
        custom_id="verify:check",
    )
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = get_pending_code(interaction.user.id)
        if not pending:
            return await interaction.response.send_message(
                "❌ 발급된 인증 코드가 없어요. 먼저 **인증진행** 버튼을 눌러주세요.",
                ephemeral=True,
            )
        if pending.get("verified"):
            return await interaction.response.send_message(
                "✅ 이미 인증된 사용자예요.", ephemeral=True
            )
        cooldown = get_cooldown_remaining(interaction.user.id)
        if cooldown:
            return await interaction.response.send_message(
                f"⚠️ 잠시 후 다시 시도해주세요. 남은 시간: {max(1, (cooldown + 59) // 60)}분",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        config = get_guild_config(pending["guild_id"])
        code = pending["code"]

        try:
            comment = await aion2_scraper.find_comment_by_code(config["article_url"], code)
        except NotImplementedError:
            return await interaction.followup.send(
                "⚠️ 아직 조회 기능이 완전히 연결되지 않았어요. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
        except Exception as e:
            return await interaction.followup.send(
                f"⚠️ 게시판 조회 중 오류가 발생했어요: {e}", ephemeral=True
            )

        if not comment:
            return await send_verification_failure(
                interaction,
                "❌ 아직 해당 코드가 적힌 댓글을 찾지 못했어요. 댓글을 작성했는지 확인 후 다시 시도해주세요.",
                "코드 댓글을 찾지 못함",
            )

        try:
            char_info = await aion2_scraper.get_character_info(comment["profile_url"])
        except NotImplementedError:
            return await interaction.followup.send(
                "⚠️ 아직 캐릭터 조회 기능이 완전히 연결되지 않았어요. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
        except Exception as e:
            return await interaction.followup.send(
                f"⚠️ 캐릭터 정보 조회 중 오류가 발생했어요: {e}", ephemeral=True
            )

        if not char_info:
            return await send_verification_failure(
                interaction,
                f"❌ `{comment['nickname']}` 캐릭터 정보를 찾을 수 없어요.",
                "캐릭터 정보를 찾지 못함",
            )

        if char_info["server"] != config["target_server"]:
            return await send_verification_failure(
                interaction,
                f"❌ `{char_info['nickname']}` 님은 **{char_info['server']}** 서버 소속이라 "
                f"인증 대상({config['target_server']} 서버)이 아니에요.",
                "인증 대상 서버와 불일치",
            )

        # ---- 인증 성공: 역할 부여 ----
        role = None
        if config["role_id"]:
            role = interaction.guild.get_role(config["role_id"])

        if role:
            try:
                await interaction.user.add_roles(role, reason="아이온2 서버 인증 성공")
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ 인증은 확인됐지만 봇에게 역할 부여 권한이 없어요. 관리자에게 문의해주세요.",
                    ephemeral=True,
                )

        nickname_changed = True
        discord_nickname = (
            f"{char_info['nickname']}/{char_info.get('class_name', '알 수 없음')}"
            f"[{char_info.get('legion', '없음')}]"
        )[:32]
        try:
            await interaction.user.edit(
                nick=discord_nickname, reason="아이온2 캐릭터 인증 성공"
            )
        except (discord.Forbidden, discord.HTTPException):
            nickname_changed = False

        mark_verified(interaction.user.id)
        await log_verification(
            interaction.guild,
            f"성공: {interaction.user} ({interaction.user.id}) - {discord_nickname}",
        )

        embed = discord.Embed(title="✅ 인증 완료", color=discord.Color.green())
        embed.add_field(name="닉네임", value=char_info["nickname"], inline=True)
        embed.add_field(name="직업", value=char_info.get("class_name", "없음"), inline=True)
        embed.add_field(name="서버", value=char_info["server"], inline=True)
        embed.add_field(name="종족", value=char_info.get("race", "없음"), inline=True)
        embed.add_field(name="레기온", value=char_info.get("legion", "없음"), inline=True)
        status_messages = []
        if role:
            status_messages.append(f"{role.mention} 역할이 부여됐어요.")
        if nickname_changed:
            status_messages.append(f"디스코드 닉네임이 `{discord_nickname}`으로 변경됐어요.")
        else:
            status_messages.append("⚠️ 닉네임 변경 권한이 없어 디스코드 닉네임은 변경하지 못했어요.")
        embed.description = "\n".join(status_messages)
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------- Cog ----------------
class Verify(commands.Cog):
    """서버 인증(캐릭터 확인 후 역할 부여) 기능 (전부 슬래시(/) 명령어 전용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 영구 View 등록 (봇 재시작 후에도 버튼이 계속 작동하도록)
        bot.add_view(VerifyPanelView())
        bot.add_view(VerifyCodeView(DEFAULT_ARTICLE_URL))

    @app_commands.command(name="인증패널생성", description="인증 센터 패널(버튼)을 이 채널에 게시합니다.")
    @app_commands.describe(title="패널 제목", description="패널 설명")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_panel(
        self,
        interaction: discord.Interaction,
        title: str = "🛡️ 서버 전용 인증 센터",
        description: str = "가입 또는 재인증을 위해서 아래의 **인증진행** 버튼을 눌러주세요.",
    ):
        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
        await interaction.channel.send(embed=embed, view=VerifyPanelView())
        await interaction.response.send_message("✅ 인증 패널을 게시했어요.", ephemeral=True)

    @app_commands.command(name="인증역할설정", description="인증 성공 시 부여할 역할을 설정합니다.")
    @app_commands.describe(role="부여할 역할")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
        set_guild_config(interaction.guild.id, "role_id", role.id)
        await interaction.response.send_message(
            f"✅ 인증 성공 시 `{role.name}` 역할을 부여하도록 설정했어요."
        )

    @app_commands.command(name="인증서버설정", description="인증을 통과시킬 게임 서버 이름을 설정합니다.")
    @app_commands.describe(server_name="게임 내 서버 이름 (예: 브리트라)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_server(self, interaction: discord.Interaction, server_name: str):
        set_guild_config(interaction.guild.id, "target_server", server_name)
        await interaction.response.send_message(f"✅ 인증 대상 서버를 `{server_name}` 으로 설정했어요.")

    @app_commands.command(name="인증게시판설정", description="인증 코드를 댓글로 작성할 게시글 URL을 설정합니다.")
    @app_commands.describe(url="게시글 URL")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_article(self, interaction: discord.Interaction, url: str):
        set_guild_config(interaction.guild.id, "article_url", url)
        await interaction.response.send_message("✅ 인증게시판 URL을 설정했어요.")

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
    await bot.add_cog(Verify(bot))
