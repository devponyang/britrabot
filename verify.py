import json
import os
import random
import string
import datetime
import time
import re

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
MIN_POWER_LEVEL = 450
ALARM_ROLE_GUILD_ID = 1545016047332237332
ALARM_ROLE_IDS = {
    "카이라": 1547034865885646859,
    "나흐마": 1547034865885646859,
    "시공쟁탈전": 1547035053677092884,
    "어비스 균열지대": 1547035121339736094,
    "아티팩트쟁": 1547035121339736094,
    "어비스 필드보스": 1547034865885646859,
}
ALARM_ROLE_GROUPS = (
    ("필드보스", "🐉", "어비스 필드보스"),
    ("시공", "⏳", "시공쟁탈전"),
    ("어비스", "🌌", "어비스 균열지대"),
)


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
        self.add_item(VerifyTicketButton())

    @discord.ui.button(
        label="인증진행", style=discord.ButtonStyle.success, custom_id="verify:start"
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        pending = get_pending_code(interaction.user.id)
        if pending and pending.get("verified"):
            return await interaction.followup.send(
                "✅ 이미 인증된 사용자예요.", ephemeral=True
            )
        cooldown = get_cooldown_remaining(interaction.user.id)
        if cooldown:
            return await interaction.followup.send(
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

        await interaction.followup.send(
            embed=embed, view=VerifyCodeView(config["article_url"]), ephemeral=True
        )


class VerifyTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="문의 티켓",
            style=discord.ButtonStyle.primary,
            emoji="🎫",
            custom_id="ticket:verify_open",
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        topic = f"티켓 대상: {interaction.user.id}"
        existing = next(
            (channel for channel in guild.text_channels if channel.topic == topic), None
        )
        if existing:
            await interaction.response.send_message(
                f"이미 열려 있는 티켓이 있어요: {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]", "-", interaction.user.display_name).strip("-")
        channel_name = f"티켓-{safe_name or interaction.user.id}"[:100]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        try:
            channel = await guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                topic=topic,
                reason=f"{interaction.user}가 인증 패널에서 티켓 생성",
            )
            await channel.send(
                f"{interaction.user.mention} 님의 티켓이 생성됐어요. 운영진에게 문의 내용을 남겨주세요."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 봇에게 채널 관리 권한이 없어 티켓을 만들 수 없어요.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ 티켓 채널 {channel.mention}을 생성했어요.", ephemeral=True
        )


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
        if interaction.guild is None or interaction.guild.id != ALARM_ROLE_GUILD_ID:
            await interaction.response.send_message(
                "❌ 이 서버에서는 사용할 수 없는 버튼이에요.", ephemeral=True
            )
            return

        role = interaction.guild.get_role(ALARM_ROLE_IDS[self.role_event_name])
        if role is None:
            await interaction.response.send_message(
                "❌ 알람 역할을 준비하지 못했어요. 봇의 역할 설정을 확인해주세요.",
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="알람 역할 해제")
            message = f"✅ {role.mention} 역할을 해제했어요."
        else:
            await interaction.user.add_roles(role, reason="알람 역할 선택")
            message = f"✅ {role.mention} 역할을 받았어요."
        await interaction.response.send_message(message, ephemeral=True)


class VerificationAlarmRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for group_name, emoji, role_event_name in ALARM_ROLE_GROUPS:
            self.add_item(VerificationAlarmRoleButton(group_name, emoji, role_event_name))


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
        await interaction.response.defer(ephemeral=True)
        pending = get_pending_code(interaction.user.id)
        if not pending:
            return await interaction.followup.send(
                "❌ 발급된 인증 코드가 없어요. 먼저 **인증진행** 버튼을 눌러주세요.",
                ephemeral=True,
            )
        if pending.get("verified"):
            return await interaction.followup.send(
                "✅ 이미 인증된 사용자예요.", ephemeral=True
            )
        cooldown = get_cooldown_remaining(interaction.user.id)
        if cooldown:
            return await interaction.followup.send(
                f"⚠️ 잠시 후 다시 시도해주세요. 남은 시간: {max(1, (cooldown + 59) // 60)}분",
                ephemeral=True,
            )

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

        power_level = char_info.get("power_level")
        if power_level is None or power_level < MIN_POWER_LEVEL:
            displayed_power_level = power_level if power_level is not None else "확인 불가"
            return await send_verification_failure(
                interaction,
                f"❌ `{char_info['nickname']}` 님의 전투력이 **{displayed_power_level}**이라 "
                f"인증 기준({MIN_POWER_LEVEL} 이상)을 충족하지 못했어요.",
                "전투력 기준 미달 또는 확인 불가",
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
        embed.add_field(name="전투력", value=f"{char_info['power_level']:,}", inline=True)
        status_messages = []
        if role:
            status_messages.append(f"{role.mention} 역할이 부여됐어요.")
        if nickname_changed:
            status_messages.append(f"디스코드 닉네임이 `{discord_nickname}`으로 변경됐어요.")
        else:
            status_messages.append("⚠️ 닉네임 변경 권한이 없어 디스코드 닉네임은 변경하지 못했어요.")
        embed.description = "\n".join(status_messages)
        await interaction.followup.send(embed=embed, ephemeral=True)

        alarm_embed = discord.Embed(
            title="🔔 알람 설정",
            description=(
                "원하는 보스/이벤트 알람만 선택해서 받을 수 있어요.\n"
                "버튼을 누르면 해당 알람 역할이 부여되거나 해제돼요."
            ),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(
            embed=alarm_embed,
            view=VerificationAlarmRoleView(),
            ephemeral=True,
        )


# ---------------- Cog ----------------
class Verify(commands.Cog):
    """서버 인증(캐릭터 확인 후 역할 부여) 기능 (전부 슬래시(/) 명령어 전용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 영구 View 등록 (봇 재시작 후에도 버튼이 계속 작동하도록)
        bot.add_view(VerifyPanelView())
        bot.add_view(VerifyCodeView(DEFAULT_ARTICLE_URL))

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
            "- 인증 코드는 **본인만** 사용할 수 있으며, 타인에게 공유하지 마세요.\n"
            "- 브리트라 서버 캐릭터가 아니거나 전투력이 450 미만일 경우 인증이 통과되지 않아요.\n"
            "- 댓글을 작성했는데도 인증이 안 된다면, 댓글이 실제로 게시됐는지 새로고침해서 확인 후 다시 시도해주세요.\n"
            "- 소속 레기온과 무관하게 브리트라 서버의 전투력 450 이상 캐릭터만 인증 가능해요.\n"
            "- 인증 관련 문제가 있다면 **문의 티켓**을 열어 운영진에게 알려주세요.\n\n"
            "인증이 완료되면 통합 디스코드의 모든 채널을 이용하실 수 있어요. 많은 이용 부탁드립니다! 🙏"
        )
        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
        await interaction.channel.send(embed=embed, view=VerifyPanelView())
        await interaction.response.send_message("✅ 인증 패널을 게시했어요.", ephemeral=True)

    @app_commands.command(name="인증역할설정", description="인증 성공 시 부여할 역할을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="부여할 역할")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
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
        set_guild_config(interaction.guild.id, "target_server", server_name)
        await interaction.response.send_message(f"✅ 인증 대상 서버를 `{server_name}` 으로 설정했어요.")

    @app_commands.command(name="인증게시판설정", description="인증 코드를 댓글로 작성할 게시글 URL을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
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
