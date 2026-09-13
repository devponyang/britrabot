import asyncio
from contextvars import ContextVar
import logging
from functools import wraps

import discord
from discord import app_commands
from discord.ext import commands

import aion2_scraper

from storage import BASE_DIR, load_json as _load, save_json as _save
from verification_state import (generate_code, save_pending_code, get_pending_code,
    mark_verified, get_cooldown_remaining, record_verification_failure, code_expired,
    MAX_VERIFY_ATTEMPTS)

CONFIG_FILE = BASE_DIR / "verify_config.json"
logger = logging.getLogger(__name__)
IN_FLIGHT = set()
VERIFICATION_OUTCOME = ContextVar("verification_outcome", default=None)
VERIFY_SLOTS = asyncio.Semaphore(3)
VERIFY_TIMEOUT_SECONDS = 180

LEGACY_ARTICLE_URL = (
    "https://aion2.plaync.com/ko-kr/board/server/view"
    "?articleId=6a9a6c96feeef62e67566500&categoryId=69094e85a7d3dc347cdf1e18"
)
DEFAULT_ARTICLE_URL = LEGACY_ARTICLE_URL.replace("6a9a6c96feeef62e67566500", "6a9e37619ed1202b9b8fb310")
DEFAULT_TARGET_SERVER = "브리트라"
AUTOMATION_CONFIG_FILE = BASE_DIR / "guild_config.json"
MIN_POWER_LEVEL = 450_000
from alarm_settings import ALARM_ROLE_GUILD_ID, ALARM_ROLE_IDS, ALARM_ROLE_GROUPS
from discord_helpers import SafeView, open_ticket, toggle_role
ACTIVE_VERIFY_MESSAGES: dict[tuple[int, int], discord.WebhookMessage] = {}


# ---------------- 저장소 헬퍼 ----------------
def get_guild_config(guild_id: int) -> dict:
    data = _load(CONFIG_FILE)
    cfg = data.get(str(guild_id), {})
    cfg.setdefault("article_url", DEFAULT_ARTICLE_URL)
    if cfg["article_url"] == LEGACY_ARTICLE_URL:
        cfg["article_url"] = DEFAULT_ARTICLE_URL
    cfg.setdefault("target_server", DEFAULT_TARGET_SERVER)
    cfg.setdefault("role_id", None)
    return cfg


def set_guild_config(guild_id: int, key: str, value):
    data = _load(CONFIG_FILE)
    data.setdefault(str(guild_id), {})[key] = value
    _save(CONFIG_FILE, data)


def build_verification_progress_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⏳ 인증을 진행 중입니다",
        description=(
            "작성하신 댓글과 캐릭터 정보를 확인하고 있습니다.\n\n"
            "**완료 안내가 나올 때까지 버튼을 다시 누르지 마세요.**\n"
            "잠시만 기다려주세요. 순서대로 처리하고 있습니다."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="이 안내는 본인에게만 표시됩니다 · 최대 3분 소요")
    return embed


async def finish_verification(interaction: discord.Interaction, message: str | None = None, *, embed=None):
    """Replace the private progress message for every terminal outcome."""
    if embed is None:
        success = message.startswith("✅")
        embed = discord.Embed(
            title="✅ 인증 완료" if success else "📋 인증 확인 안내",
            description=message,
            color=discord.Color.green() if success else discord.Color.orange(),
        )
    return await interaction.edit_original_response(content=None, embed=embed)


def verification_request(callback):
    @wraps(callback)
    async def guarded(self, interaction, button):
        if interaction.guild is None:
            return await interaction.response.send_message("서버 안에서 이용해주세요.", ephemeral=True)
        key = (interaction.guild.id, interaction.user.id)
        if key in IN_FLIGHT:
            return await interaction.response.send_message("⏳ 이미 인증을 처리 중이에요. 결과를 기다려주세요.", ephemeral=True)
        # Acquire before the first network await, including the button update.
        IN_FLIGHT.add(key)
        checking = button.custom_id == "verify:check"
        request_view = None
        acknowledged = False
        completed = False
        outcome = {"committed": False}
        outcome_token = VERIFICATION_OUTCOME.set(outcome)
        try:
            if checking:
                # Never mutate the shared persistent view: each message gets its own view.
                article_url = get_guild_config(interaction.guild.id)["article_url"]
                request_view = VerifyCodeView(article_url)
                request_view.check_button.disabled = True
                request_view.check_button.label = "인증 확인 중…"
                await interaction.response.edit_message(view=request_view)
                acknowledged = True
                await interaction.edit_original_response(content=None, embed=build_verification_progress_embed())
            else:
                await interaction.response.defer(ephemeral=True, thinking=True)
                acknowledged = True
            async with asyncio.timeout(VERIFY_TIMEOUT_SECONDS):
                async with VERIFY_SLOTS:
                    result = await callback(self, interaction, button)
                    completed = result is True
                    return result
        except asyncio.CancelledError:
            if checking and acknowledged:
                try:
                    await finish_verification(interaction, "✅ 인증과 역할 부여는 완료됐어요. 추가 안내 처리가 중단됐습니다." if outcome["committed"] else "⚠️ 인증 처리가 중단됐어요. 잠시 후 다시 시도해주세요.")
                except discord.HTTPException:
                    logger.exception("인증 중단 안내 전송 실패")
            raise
        except Exception:
            logger.exception("인증 처리 실패: guild=%s user=%s", *key)
            message = "⚠️ 인증 처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요. 실패 횟수는 추가되지 않아요."
            if outcome["committed"]:
                message = "✅ 인증과 역할 부여는 완료됐어요. 닉네임 또는 추가 안내 처리는 완료하지 못했을 수 있어요."
            if checking and acknowledged:
                await finish_verification(interaction, message)
            elif acknowledged:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        finally:
            try:
                if request_view is not None and acknowledged:
                    completed = completed or outcome["committed"]
                    request_view.check_button.disabled = completed
                    request_view.check_button.label = "인증 완료" if completed else "댓글 작성 완료 (다음)"
                    try:
                        await interaction.edit_original_response(view=request_view)
                    except discord.HTTPException:
                        logger.exception("인증 버튼 상태 갱신 실패: user=%s", interaction.user.id)
            finally:
                IN_FLIGHT.discard(key)
                VERIFICATION_OUTCOME.reset(outcome_token)
    return guarded


def role_error(guild, role):
    if role is None:
        return "인증 역할이 설정되지 않았거나 삭제됐어요. 관리자에게 문의해주세요."
    if not guild.me.guild_permissions.manage_roles or not role.is_assignable():
        return "봇이 인증 역할을 부여할 수 없어요. 관리자가 역할 관리 권한과 역할 순서를 확인해야 해요."
    return None


def is_verified(pending, config, member):
    return bool(pending and pending.get("verified") and
                pending.get("role_id") == config.get("role_id") and
                any(role.id == config.get("role_id") for role in member.roles))


async def log_verification(guild: discord.Guild, message: str):
    try:
        config = _load(AUTOMATION_CONFIG_FILE).get(str(guild.id), {})
        channel = guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
        if channel:
            await channel.send(f"🔐 {message}", allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.exception("인증 로그 전송 실패")


async def send_verification_failure(
    interaction: discord.Interaction, message: str, reason: str
):
    attempts, cooldown = record_verification_failure(interaction.user.id, interaction.guild.id)
    await log_verification(
        interaction.guild,
        f"실패: {interaction.user} ({interaction.user.id}) - {reason} ({attempts}/{MAX_VERIFY_ATTEMPTS})",
    )
    if cooldown:
        message += "\n⚠️ 실패 횟수를 초과해 10분 동안 재시도할 수 없어요."
    return await finish_verification(interaction, message)


# ---------------- 버튼 UI ----------------
class VerifyPanelView(SafeView):
    """인증 센터에 올라가는 '인증진행' 버튼 (영구 View)"""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyTicketButton())

    @discord.ui.button(
        label="인증진행", style=discord.ButtonStyle.success, custom_id="verify:start"
    )
    @verification_request
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = get_pending_code(interaction.user.id, interaction.guild.id)
        config = get_guild_config(interaction.guild.id)
        if is_verified(pending, config, interaction.user):
            await interaction.followup.send("✅ 이미 인증된 사용자예요.", ephemeral=True)
            await send_verification_alarm_panel(interaction)
            return True
        cooldown = get_cooldown_remaining(interaction.user.id, interaction.guild.id)
        if cooldown:
            return await interaction.followup.send(
                f"⚠️ 인증 실패 횟수를 초과했어요. {max(1, (cooldown + 59) // 60)}분 후 다시 시도해주세요.",
                ephemeral=True,
            )
        code = pending.get("code") if pending else None
        if not code or code_expired(pending):
            code = generate_code()
            save_pending_code(interaction.user.id, interaction.guild.id, code)
        config = get_guild_config(interaction.guild.id)

        embed = discord.Embed(title="디스코드 인증 요청", color=discord.Color.blurple())
        embed.description = (
            "아래 인증 코드를 복사하여 홈페이지 인증게시판에 댓글로 작성해주세요.\n"
            "대표 캐릭터를 반드시 확인 부탁드립니다.\n\n"
            "**댓글 작성 완료 버튼은 한 번만 눌러주세요.** 인증 중에는 비활성화되며, 실패하면 다시 사용할 수 있어요.\n\n"
            "댓글 작성이 완료되면 아래의 **댓글 작성 완료 (다음)** 버튼을 눌러주세요."
        )
        embed.add_field(name="🔑 발급된 인증 코드 (30분 유효)", value=f"`{code}`", inline=False)

        message_key = (interaction.guild.id, interaction.user.id)
        message_view = VerifyCodeView(config["article_url"])
        previous_message = ACTIVE_VERIFY_MESSAGES.get(message_key)
        if previous_message:
            try:
                await previous_message.delete()
            except discord.DiscordException:
                pass
            finally:
                ACTIVE_VERIFY_MESSAGES.pop(message_key, None)

        message = await interaction.followup.send(
            content=None, embed=embed, view=message_view, ephemeral=True, wait=True
        )
        ACTIVE_VERIFY_MESSAGES[message_key] = message
        def forget():
            if ACTIVE_VERIFY_MESSAGES.get(message_key) is message:
                ACTIVE_VERIFY_MESSAGES.pop(message_key, None)
        asyncio.get_running_loop().call_later(900, forget)


class VerifyTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="문의 티켓",
            style=discord.ButtonStyle.primary,
            emoji="🎫",
            custom_id="ticket:verify_open",
        )

    async def callback(self, interaction: discord.Interaction):
        await open_ticket(interaction)


class VerificationAlarmRoleButton(discord.ui.Button):
    def __init__(self, group_name: str, emoji: str, role_event_name: str):
        super().__init__(
            label=group_name,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"verify:alarmrole:{group_name}",
        )
        self.role_event_name = role_event_name

    async def callback(self, interaction: discord.Interaction):
        await toggle_role(interaction, ALARM_ROLE_GUILD_ID, ALARM_ROLE_IDS[self.role_event_name])


class VerificationAlarmRoleView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)
        for group_name, emoji, role_event_name in ALARM_ROLE_GROUPS:
            self.add_item(VerificationAlarmRoleButton(group_name, emoji, role_event_name))


def build_verification_alarm_embed() -> discord.Embed:
    return discord.Embed(
        title="🔔 알람 설정",
        description=(
            "원하는 보스/이벤트 알람만 골라서 받을 수 있어요. "
            "받고 싶은 알람의 버튼을 누르면 해당 역할이 부여되고, "
            "이후 그 알람이 뜰 때 **#알람-채널**에서 멘션(핑)을 받아요.\n\n"
            "- 버튼을 누르면 → 역할 부여 (알림 받기 시작)\n"
            "- 같은 버튼을 다시 누르면 → 역할 해제 (알림 그만 받기)\n"
            "- 여러 개 동시에 선택 가능해요. 필요한 것만 골라서 받으세요!\n\n"
            "> 아티쟁 전략 공유는 **어비스**를 클릭하여 권한을 받아주세요."
        ),
        color=discord.Color.gold(),
    )


async def send_verification_alarm_panel(interaction: discord.Interaction):
    if interaction.guild.id != ALARM_ROLE_GUILD_ID:
        return
    try:
        await interaction.followup.send(
            content=None,
            embed=build_verification_alarm_embed(),
            view=VerificationAlarmRoleView(),
            ephemeral=True,
        )
    except discord.HTTPException:
        # Delivery failure must not replace an already successful verification result.
        logger.exception("인증 후 알람 설정 패널 전송 실패: user=%s", interaction.user.id)


class VerifyCodeView(SafeView):
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
    @verification_request
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = get_pending_code(interaction.user.id, interaction.guild.id)
        if not pending:
            return await finish_verification(interaction,
                "❌ 발급된 인증 코드가 없어요. 먼저 **인증진행** 버튼을 눌러주세요.",
            )
        config = get_guild_config(interaction.guild.id)
        if is_verified(pending, config, interaction.user):
            await finish_verification(interaction, "✅ 이미 인증된 사용자예요.")
            await send_verification_alarm_panel(interaction)
            return True
        cooldown = get_cooldown_remaining(interaction.user.id, interaction.guild.id)
        if cooldown:
            return await finish_verification(interaction,
                f"⚠️ 잠시 후 다시 시도해주세요. 남은 시간: {max(1, (cooldown + 59) // 60)}분",
            )

        if code_expired(pending):
            return await finish_verification(interaction, "⌛ 인증 코드가 만료됐어요. 인증진행 버튼으로 새 코드를 받아 댓글을 작성해주세요.")
        config = get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(config["role_id"]) if config["role_id"] else None
        problem = role_error(interaction.guild, role)
        if problem:
            return await finish_verification(interaction, f"⚠️ {problem}")
        code = pending["code"]

        try:
            comment = await aion2_scraper.find_comment_by_code(config["article_url"], code)
        except Exception:
            logger.exception("게시판 조회 실패")
            return await finish_verification(interaction, "⚠️ 게시판 조회에 실패했어요. 잠시 후 재시도해주세요. 실패 횟수는 추가되지 않아요.")

        if not comment:
            return await send_verification_failure(
                interaction,
                "❌ 아직 해당 코드가 적힌 댓글을 찾지 못했어요. 댓글을 작성했는지 확인 후 다시 시도해주세요.",
                "코드 댓글을 찾지 못함",
            )

        try:
            char_info = await aion2_scraper.get_character_info(comment["profile_url"])
        except Exception:
            logger.exception("캐릭터 조회 실패")
            return await finish_verification(interaction, "⚠️ 캐릭터 조회에 실패했어요. 잠시 후 재시도해주세요. 실패 횟수는 추가되지 않아요.")

        if not char_info or char_info.get("power_level") is None:
            return await finish_verification(interaction, "⚠️ 캐릭터 정보를 확인하지 못했어요. 잠시 후 재시도해주세요. 실패 횟수는 추가되지 않아요.")

        if char_info["server"] != config["target_server"]:
            return await send_verification_failure(
                interaction,
                f"❌ `{char_info['nickname']}` 님은 **{char_info['server']}** 서버 소속이라 "
                f"인증 대상({config['target_server']} 서버)이 아니에요.",
                "인증 대상 서버와 불일치",
            )

        power_level = char_info.get("power_level")
        if power_level is None or power_level < MIN_POWER_LEVEL:
            displayed_power_level = power_level if power_level is not None else "확인 불가"
            return await send_verification_failure(
                interaction,
                f"❌ `{char_info['nickname']}` 님의 전투력이 **{displayed_power_level}**이라 "
                f"인증 기준({MIN_POWER_LEVEL:,} 이상)을 충족하지 못했어요.",
                "전투력 기준 미달 또는 확인 불가",
            )

        if code_expired(pending):
            return await finish_verification(interaction, "조회 중 인증 코드가 만료됐어요. 새 코드를 발급해주세요.")
        # Settings may have changed while the external pages were loading.
        if get_guild_config(interaction.guild.id) != config:
            return await finish_verification(interaction, "인증 설정이 변경됐어요. 다시 시도해주세요.")
        try:
            await interaction.user.add_roles(role, reason="아이온2 서버 인증 성공")
        except discord.HTTPException:
            logger.exception("인증 역할 부여 실패")
            return await finish_verification(interaction, "⚠️ 인증 역할을 부여하지 못했어요. 관리자에게 권한을 확인한 뒤 다시 시도해주세요.")
        mark_verified(interaction.user.id, interaction.guild.id, role.id, comment["profile_url"])
        outcome = VERIFICATION_OUTCOME.get()
        if outcome is not None:
            outcome["committed"] = True
        ACTIVE_VERIFY_MESSAGES.pop((interaction.guild.id, interaction.user.id), None)

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
        embed.add_field(name="전투력", value=f"{char_info['power_level']:,}", inline=True)
        status_messages = []
        if role:
            status_messages.append(f"{role.mention} 역할이 부여됐어요.")
        if nickname_changed:
            status_messages.append(f"디스코드 닉네임이 `{discord_nickname}`으로 변경됐어요.")
        else:
            status_messages.append("⚠️ 닉네임 변경 권한이 없어 디스코드 닉네임은 변경하지 못했어요.")
        embed.description = "**인증이 완료되었습니다!**\n\n" + "\n".join(status_messages)
        await finish_verification(interaction, embed=embed)

        await send_verification_alarm_panel(interaction)
        return True


# ---------------- Cog ----------------
class Verify(commands.Cog):
    """서버 인증(캐릭터 확인 후 역할 부여) 기능 (전부 슬래시(/) 명령어 전용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 영구 View 등록 (봇 재시작 후에도 버튼이 계속 작동하도록)
        bot.add_view(VerifyPanelView())
        bot.add_view(VerifyCodeView(DEFAULT_ARTICLE_URL))
        bot.add_view(VerificationAlarmRoleView())

    @app_commands.command(name="인증패널생성", description="인증 센터 패널(버튼)을 이 채널에 게시합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(title="패널 제목", description="패널 설명")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_panel(
        self,
        interaction: discord.Interaction,
        title: str | None = None,
        description: str | None = None,
    ):
        target_server = get_guild_config(interaction.guild.id)["target_server"]
        title = title or "🛡️ 브리트라 통합 디스코드 인증 센터"
        description = description or (
            "여기는 **아이온2 브리트라 서버** 유저라면 누구나 모이는 통합 디스코드예요.\n"
            "소속 레기온에 상관없이, 브리트라 서버의 전투력 450 이상 캐릭터라면 인증 후 자유롭게 이용하실 수 있어요.\n\n"
            "## 📋 인증 절차\n\n"
            "**1️⃣** 아래 **[인증진행]** 버튼을 눌러주세요.\n"
            "**2️⃣** 발급된 인증 코드를 확인하세요. (본인만 볼 수 있어요)\n"
            "**3️⃣** **[인증게시판으로 이동]** 버튼을 눌러 아이온2 홈페이지 인증게시판으로 이동, 발급받은 코드를 댓글로 남겨주세요.\n"
            "> ⚠️ 반드시 **브리트라 서버의 대표 캐릭터**로 댓글을 작성해주세요. (다른 서버 캐릭터 또는 전투력 450 미만 캐릭터는 인증이 통과되지 않아요)\n"
            "**4️⃣** 댓글 작성 후 **[댓글 작성 완료]** 버튼을 눌러주세요. 봇이 자동으로 확인 후 역할을 부여해드려요. 사용량에 따라 역할 부여에는 최대 2분이상 소요 될 수도 있습니다.\n\n"
            "## ❗ 주의사항\n"
            "- **댓글 작성 완료 버튼은 한 번만 눌러주세요.** 인증 중에는 버튼이 비활성화됩니다.\n"
            "- 최대 3분 동안 결과를 기다려주세요. 실패 안내가 나오면 버튼이 다시 활성화됩니다.\n"
            "- 실패 3회로 대기시간이 적용된 경우에는 안내된 시간이 지난 뒤 재시도해주세요.\n"
            "- 인증 코드는 **본인만** 사용할 수 있으며, 타인에게 공유하지 마세요.\n"
            "- 브리트라 서버 캐릭터가 아니거나 전투력이 450 미만일 경우 인증이 통과되지 않아요.\n"
            "- 댓글을 작성했는데도 인증이 안 된다면, 댓글이 실제로 게시됐는지 새로고침해서 확인 후 다시 시도해주세요.\n"
            "- 소속 레기온과 무관하게 브리트라 서버의 전투력 450 이상 캐릭터만 인증 가능해요.\n"
            "- 인증 관련 문제가 있다면 **문의 티켓**을 열어 운영진에게 알려주세요.\n\n"
            "인증이 완료되면 통합 디스코드의 모든 채널을 이용하실 수 있어요. 많은 이용 부탁드립니다! 🙏"
        )
        title = title.replace("브리트라", target_server)
        description = description.replace("브리트라", target_server).replace("450", f"{MIN_POWER_LEVEL / 1000:g}K({MIN_POWER_LEVEL:,})")
        embed = discord.Embed(title=title[:256], description=description[:4096], color=discord.Color.blue())
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(content=None, embed=embed, view=VerifyPanelView())
        await interaction.followup.send("✅ 인증 패널을 게시했어요.", ephemeral=True)

    @app_commands.command(name="인증역할설정", description="인증 성공 시 부여할 역할을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="부여할 역할")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
        problem = role_error(interaction.guild, role)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        set_guild_config(interaction.guild.id, "role_id", role.id)
        await interaction.response.send_message(
            f"✅ 인증 성공 시 `{role.name}` 역할을 부여하도록 설정했어요."
        )

    @app_commands.command(name="인증서버설정", description="인증을 통과시킬 게임 서버 이름을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(server_name="게임 내 서버 이름 (예: 브리트라)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_server(self, interaction: discord.Interaction, server_name: str):
        server_name = server_name.strip()
        if not 1 <= len(server_name) <= 30:
            return await interaction.response.send_message("서버 이름은 1~30자로 입력해주세요.", ephemeral=True)
        set_guild_config(interaction.guild.id, "target_server", server_name)
        await interaction.response.send_message(f"✅ 인증 대상 서버를 `{server_name}` 으로 설정했어요.")

    @app_commands.command(name="인증게시판설정", description="인증 코드를 댓글로 작성할 게시글 URL을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(url="게시글 URL")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_article(self, interaction: discord.Interaction, url: str):
        if not aion2_scraper.is_official_url(url):
            return await interaction.response.send_message("아이온2 공식 HTTPS 게시글 주소를 입력해주세요.", ephemeral=True)
        set_guild_config(interaction.guild.id, "article_url", url.strip())
        await interaction.response.send_message("✅ 인증게시판 URL을 설정했어요.")

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ 이 명령어를 실행할 권한이 없어요."
        else:
            logger.error("인증 명령 실패", exc_info=error)
            msg = "❌ 알 수 없는 오류가 발생했어요."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verify(bot))
