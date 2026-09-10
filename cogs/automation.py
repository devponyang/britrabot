import asyncio
from functools import wraps
import logging
import os
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aion2_scraper
import verify
from storage import load_json, save_json
from alarm_settings import ALARM_ROLE_GUILD_ID, ALARM_ROLE_IDS, ALARM_ROLE_GROUPS
from discord_helpers import SafeView, SafeModal, open_ticket, TicketCloseView, toggle_role, purge_messages, send_embed_pages

logger = logging.getLogger(__name__)

KST = datetime.timezone(datetime.timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "guild_config.json")
ARTIFACT_RECORDS_FILE = os.path.join(BASE_DIR, "artifact_records.json")
OFFICIAL_NOTICE_GUILD_ID = 1545016047332237332
OFFICIAL_NOTICE_CHANNEL_IDS = {
    "공지": 1546379509513723905,
    "이벤트": 1547049610021965854,
    "기타": 1546894380311519252,
}


ALARM_SCHEDULE = (
    ("카이라", {0, 1, 2, 3, 4, 5, 6}, ((1, 0), (5, 0), (9, 0), (13, 0), (17, 0), (21, 0)), (30, 10)),
    ("나흐마", {0, 4}, ((22, 0),), (30, 10)),
    ("시공쟁탈전", {0, 3, 5}, ((20, 0), (23, 0)), (30, 10)),
    ("어비스 균열지대", {1, 3}, ((22, 0),), (30, 10)),
    ("아티팩트쟁", {2, 5}, tuple((hour, 0) for hour in range(16, 23)), ()),
    ("어비스 필드보스", {2, 5}, ((22, 30),), ()),
)

EVENT_NAMES = [name for name, *_ in ALARM_SCHEDULE]
ALARM_ROLE_CHANNEL_ID = 1547039122018017300


def get_alarm_role(guild: discord.Guild, event_name: str) -> discord.Role | None:
    return guild.get_role(ALARM_ROLE_IDS[event_name])


def get_due_alarm_messages(now: datetime.datetime) -> list[tuple[str, str]]:
    """현재 시각에 전송할 알람의 중복 방지 키와 메시지를 반환합니다."""
    due = []
    current = now.replace(second=0, microsecond=0)
    for name, weekdays, times, lead_minutes in ALARM_SCHEDULE:
        if current.weekday() not in weekdays:
            continue
        for hour, minute in times:
            scheduled = current.replace(hour=hour, minute=minute)
            minutes_before = round((scheduled - current).total_seconds() / 60)
            if minutes_before == 0:
                label = f"{name} 알람"
            elif minutes_before in lead_minutes:
                label = f"{name} {minutes_before}분 전 알람"
            else:
                continue

            if name == "아티팩트쟁" and (hour, minute) == (22, 0):
                label = "아티팩트쟁 + 어비스 필드보스 알람 (아티팩트쟁 22:00 / 필드보스 22:30)"
            elif name == "어비스 필드보스":
                label = "아티팩트쟁 + 어비스 필드보스 알람 (필드보스 22:30)"
            due.append((f"{name}:{hour:02d}:{minute:02d}:{minutes_before}", label))
    return due


# 이벤트별로 눈에 띄는 이모지/색상을 지정합니다. 목록에 없는 이벤트는 기본값(🔔, 파랑)을 씁니다.
ALARM_STYLE = {
    "카이라": {"emoji": "🐉", "color": discord.Color.red()},
    "나흐마": {"emoji": "🦑", "color": discord.Color.dark_teal()},
    "시공쟁탈전": {"emoji": "⏳", "color": discord.Color.purple()},
    "어비스 균열지대": {"emoji": "🌌", "color": discord.Color.dark_purple()},
    "아티팩트쟁": {"emoji": "🏺", "color": discord.Color.gold()},
    "어비스 필드보스": {"emoji": "👹", "color": discord.Color.orange()},
}
DEFAULT_ALARM_STYLE = {"emoji": "🔔", "color": discord.Color.blurple()}


def build_alarm_embed(alarm_message: str, schedule_key: str, custom_text: str | None = None) -> discord.Embed:
    """알람 메시지를 이벤트에 맞는 이모지/색상의 임베드로 꾸며줍니다."""
    event_name = schedule_key.split(":", 1)[0]
    minutes_before = int(schedule_key.rsplit(":", 1)[1])
    style = ALARM_STYLE.get(event_name, DEFAULT_ALARM_STYLE)

    status = "🚨 지금 시작!" if minutes_before == 0 else f"⏰ {minutes_before}분 전!"

    embed = discord.Embed(
        title=f"{style['emoji']} {alarm_message}",
        description=f"## {status}",
        color=style["color"],
        timestamp=datetime.datetime.now(KST),
    )
    if custom_text:
        embed.add_field(name="📋 안내", value=custom_text, inline=False)
    embed.set_footer(text="놓치지 말고 참여하세요! 🙌")
    return embed


def load_config() -> dict:
    return load_json(CONFIG_FILE)


def save_config(data: dict):
    save_json(CONFIG_FILE, data)


def get_guild_config(guild_id: int) -> dict:
    data = load_config()
    return data.get(str(guild_id), {})


def set_guild_config(guild_id: int, key: str, value):
    data = load_config()
    data.setdefault(str(guild_id), {})[key] = value
    save_config(data)


def save_artifact_history(history: dict):
    """서버 쌍별 아티팩트 상세 기록을 JSON으로 누적 저장합니다."""
    data = load_json(ARTIFACT_RECORDS_FILE)
    previous = data.get(history["pair"], {})
    records = {(item["date"], item["round"]): item for item in previous.get("records", [])}
    records.update({(item["date"], item["round"]): item for item in history["records"]})
    saved_history = {
        **previous,
        "updated_at": datetime.datetime.now(KST).isoformat(),
        "source_url": history["source_url"],
        "records": sorted(records.values(), key=lambda item: (item["date"], item["round"])),
    }
    if history.get("record"):
        saved_history["record"] = history["record"]
    data[history["pair"]] = saved_history
    save_json(ARTIFACT_RECORDS_FILE, data)


def load_artifact_history(pair: str) -> dict | None:
    if not os.path.exists(ARTIFACT_RECORDS_FILE):
        return None
    data = load_json(ARTIFACT_RECORDS_FILE)
    history = data.get(pair)
    if history is not None:
        return history

    servers = [server.strip() for server in pair.split(" VS ", 1)]
    if len(servers) != 2:
        return None
    reverse_pair = f"{servers[1]} VS {servers[0]}"
    history = data.get(reverse_pair)
    if history is None:
        return None

    normalized = dict(history)
    normalized["records"] = [
        {
            **record,
            "scores": [
                ":".join(reversed(score.split(":"))) if ":" in score else score
                for score in record.get("scores", [])
            ],
        }
        for record in history.get("records", [])
    ]
    summary = history.get("record")
    if summary:
        normalized_summary = dict(summary)
        normalized_summary["opponent_server"] = servers[1]
        normalized_summary["breitra_capture_count"] = summary.get("opponent_capture_count")
        normalized_summary["opponent_capture_count"] = summary.get("breitra_capture_count")
        matchup = summary.get("matchup")
        if matchup:
            normalized_summary["matchup"] = {
                **matchup,
                "breitra_result": matchup.get("opponent_result"),
                "opponent_result": matchup.get("breitra_result"),
                "breitra_round": matchup.get("opponent_round"),
                "opponent_round": matchup.get("breitra_round"),
                "breitra_total": matchup.get("opponent_total"),
                "opponent_total": matchup.get("breitra_total"),
            }
        normalized["record"] = normalized_summary
    return normalized


def load_artifact_chapter_history(server: str) -> list[dict]:
    """직접 매칭이 없는 서버의 챕터별 과거 기록을 반환합니다."""
    if not os.path.exists(ARTIFACT_RECORDS_FILE):
        return []
    data = load_json(ARTIFACT_RECORDS_FILE)

    history = []
    for chapter, chapter_data in data.items():
        if not chapter.startswith("챕터"):
            continue
        matches = [
            item for item in chapter_data.get("records", [])
            if server in item.get("matchup", "").split(" VS ")
        ]
        if matches:
            history.append({"chapter": chapter, "records": matches})
    return history


def load_artifact_chapter_matchups(pair: str) -> list[dict]:
    """챕터 기록에서 지정한 서버 쌍의 양방향 직접 매칭을 반환합니다."""
    if not os.path.exists(ARTIFACT_RECORDS_FILE):
        return []
    servers = [server.strip() for server in pair.split(" VS ", 1)]
    if len(servers) != 2:
        return []
    expected = set(servers)
    data = load_json(ARTIFACT_RECORDS_FILE)

    matchups = []
    for chapter, chapter_data in data.items():
        if not chapter.startswith("챕터"):
            continue
        for item in chapter_data.get("records", []):
            matchup_servers = [
                server.strip() for server in item.get("matchup", "").split(" VS ", 1)
            ]
            if len(matchup_servers) != 2 or set(matchup_servers) != expected:
                continue
            score = item.get("score", "")
            if matchup_servers != servers and ":" in score:
                score = ":".join(reversed(score.split(":")))
            matchups.append({"chapter": chapter, "matchup": pair, "score": score})
    return matchups


class AlarmMessageModal(SafeModal):
    """특정 알람에 표시할 안내 문구를 입력받는 모달"""

    def __init__(self, event_name: str, guild_id: int, current: str = ""):
        super().__init__(title=f"{event_name} 알람 문구 설정")
        self.event_name = event_name
        self.guild_id = guild_id
        self.message_input = discord.ui.TextInput(
            label="알람에 표시할 안내 문구 (비우면 삭제)",
            style=discord.TextStyle.paragraph,
            placeholder="예) 카이라 직후 모두 뿌리로 달려서 키스크작 후 바로 전투 이어질 예정입니다.",
            default=current,
            required=False,
            max_length=1000,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.message_input.value.strip()
        config = get_guild_config(self.guild_id)
        custom_messages = config.get("alarm_messages", {})
        if text:
            custom_messages[self.event_name] = text
        else:
            custom_messages.pop(self.event_name, None)
        set_guild_config(self.guild_id, "alarm_messages", custom_messages)
        await interaction.response.send_message(
            f"✅ `{self.event_name}` 알람 문구를 {'설정' if text else '초기화'}했어요.",
            ephemeral=True,
        )


class AlarmMessageButton(discord.ui.Button):
    def __init__(self, event_name: str):
        super().__init__(
            label=event_name,
            style=discord.ButtonStyle.secondary,
            custom_id=f"alarmmsg:{event_name}",
        )
        self.event_name = event_name

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "❌ 관리자만 알람 문구를 설정할 수 있어요.", ephemeral=True
            )
        current = get_guild_config(interaction.guild.id).get("alarm_messages", {}).get(
            self.event_name, ""
        )
        await interaction.response.send_modal(
            AlarmMessageModal(self.event_name, interaction.guild.id, current)
        )


class AlarmMessagePanelView(SafeView):
    """알람 종류별 버튼을 눌러 문구를 설정하는 영구 View"""

    def __init__(self):
        super().__init__(timeout=None)
        for name in EVENT_NAMES:
            self.add_item(AlarmMessageButton(name))


class AlarmRoleButton(discord.ui.Button):
    def __init__(self, group_name: str, emoji: str, role_event_name: str):
        super().__init__(
            label=group_name,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"alarmrole:{group_name}",
        )
        self.group_name = group_name
        self.role_event_name = role_event_name

    async def callback(self, interaction: discord.Interaction):
        await toggle_role(interaction, ALARM_ROLE_GUILD_ID, ALARM_ROLE_IDS[self.role_event_name])


class AlarmRolePanelView(SafeView):
    """알람 종류별 멘션 역할을 이모지 버튼으로 선택하는 영구 View"""

    def __init__(self):
        super().__init__(timeout=None)
        for group_name, emoji, role_event_name in ALARM_ROLE_GROUPS:
            self.add_item(AlarmRoleButton(group_name, emoji, role_event_name))


def build_admin_panel_embed(guild: discord.Guild) -> discord.Embed:
    config = get_guild_config(guild.id)

    def channel_name(key: str) -> str:
        channel = guild.get_channel(config.get(key)) if config.get(key) else None
        return channel.mention if channel else "미설정"

    opponent = config.get("artifact_opponent_server", "미설정")
    verification = verify.get_guild_config(guild.id)
    verify_role = guild.get_role(verification["role_id"]) if verification.get("role_id") else None
    embed = discord.Embed(
        title="⚙️ 서버 관리자 패널",
        description="아래 선택 메뉴와 버튼으로 봇 설정을 관리할 수 있어요.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="알림 채널",
        value=(
            f"게임 일정: {channel_name('alarm_channel')}\n"
            f"관리 로그: {channel_name('log_channel')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="아티팩트 상대 서버",
        value=opponent,
        inline=True,
    )
    embed.add_field(
        name="인증 설정",
        value=(
            f"역할: {verify_role.mention if verify_role else '미설정'}\n"
            f"대상 서버: {verification.get('target_server', '미설정')}"
        ),
        inline=False,
    )
    embed.set_footer(text="관리자 권한이 있는 멤버만 사용할 수 있어요.")
    return embed


class AdminPanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, setting_key: str, label: str, custom_id: str):
        super().__init__(
            placeholder=f"{label} 채널 선택",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            custom_id=custom_id,
            row=0 if setting_key == "alarm_channel" else 1,
        )
        self.setting_key = setting_key
        self.label = label

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 설정을 변경할 수 있어요.", ephemeral=True)
            return
        channel = self.values[0]
        set_guild_config(interaction.guild.id, self.setting_key, channel.id)
        await interaction.response.edit_message(embed=build_admin_panel_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"✅ {self.label} 채널을 {channel.mention} 으로 설정했어요.", ephemeral=True)


class AdminPanelRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="알람 멘션 역할 선택",
            min_values=1,
            max_values=1,
            custom_id="adminpanel:alarm_ping_role",
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 설정을 변경할 수 있어요.", ephemeral=True)
            return
        role = self.values[0]
        set_guild_config(interaction.guild.id, "alarm_ping_role", role.id)
        await interaction.response.edit_message(embed=build_admin_panel_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"✅ 알람 멘션 역할을 {role.mention} 으로 설정했어요.", ephemeral=True)


class ArtifactOpponentModal(SafeModal, title="아티팩트 상대 서버 설정"):
    server = discord.ui.TextInput(
        label="브리트라의 상대 서버 이름",
        placeholder="예: 루드라",
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
        server = self.server.value.strip()
        if not 1 <= len(server) <= 30 or server == "브리트라":
            await interaction.response.send_message(
                "❌ 브리트라가 아닌 상대 서버 이름을 입력해주세요.", ephemeral=True
            )
            return
        set_guild_config(interaction.guild.id, "artifact_opponent_server", server)
        await interaction.response.send_message(
            f"✅ 아티팩트 상대 서버를 **{server}**로 설정했어요.", ephemeral=True
        )


class VerificationSettingsModal(SafeModal, title="인증 설정 입력"):
    server = discord.ui.TextInput(
        label="인증 대상 게임 서버",
        placeholder="예: 브리트라",
        max_length=30,
    )
    article_url = discord.ui.TextInput(
        label="인증 게시글 URL",
        placeholder="https://aion2.plaync.com/...",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        config = verify.get_guild_config(guild_id)
        self.server.default = config.get("target_server", "브리트라")
        self.article_url.default = config.get("article_url", "")

    async def on_submit(self, interaction: discord.Interaction):
        server = self.server.value.strip()
        article_url = self.article_url.value.strip()
        if not server or not aion2_scraper.is_official_url(article_url):
            await interaction.response.send_message(
                "❌ 인증 대상 서버와 아이온2 공식 HTTPS 게시글 URL을 입력해주세요.", ephemeral=True
            )
            return
        verify.set_guild_config(self.guild_id, "target_server", server)
        verify.set_guild_config(self.guild_id, "article_url", article_url)
        await interaction.response.send_message("✅ 인증 서버와 게시글 URL을 저장했어요.", ephemeral=True)


class VerificationRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="인증 완료 시 부여할 역할 선택",
            min_values=1,
            max_values=1,
            custom_id="adminpanel:verification_role",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        problem = verify.role_error(interaction.guild, role)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        verify.set_guild_config(interaction.guild.id, "role_id", role.id)
        await interaction.response.edit_message(embed=build_verification_admin_embed(interaction.guild), view=self.view)
        await interaction.followup.send(f"✅ 인증 완료 역할을 {role.mention} 으로 설정했어요.", ephemeral=True)


def build_verification_admin_embed(guild: discord.Guild) -> discord.Embed:
    config = verify.get_guild_config(guild.id)
    role = guild.get_role(config["role_id"]) if config.get("role_id") else None
    embed = discord.Embed(
        title="🛡️ 인증 관리자 도구",
        description="인증 성공 조건과 인증 패널을 관리합니다.",
        color=discord.Color.blue(),
    )
    embed.add_field(name="인증 역할", value=role.mention if role else "미설정", inline=True)
    embed.add_field(name="대상 서버", value=config.get("target_server", "미설정"), inline=True)
    embed.add_field(name="인증 게시글", value="설정됨" if config.get("article_url") else "미설정", inline=True)
    return embed


class MessagePurgeModal(SafeModal, title="메시지 일괄 삭제"):
    amount = discord.ui.TextInput(
        label="삭제할 메시지 수 (1~100)",
        placeholder="예: 20",
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("1부터 100 사이의 숫자를 입력해주세요.", ephemeral=True)
        await purge_messages(interaction, amount)


class VerificationAdminView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerificationRoleSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 이 패널을 사용할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="서버·게시글 설정", style=discord.ButtonStyle.secondary, emoji="📝", custom_id="adminpanel:verify_settings", row=1)
    async def set_verification_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerificationSettingsModal(interaction.guild.id))

    @discord.ui.button(label="인증 패널 게시", style=discord.ButtonStyle.primary, emoji="🛡️", custom_id="adminpanel:verify_panel", row=1)
    async def create_verification_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = verify.get_guild_config(interaction.guild.id)
        embed = discord.Embed(
            title="🛡️ 서버 전용 인증 센터",
            description="가입 또는 재인증을 위해 아래 버튼을 눌러주세요.",
            color=discord.Color.blue(),
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(embed=embed, view=verify.VerifyPanelView())
        await interaction.followup.send(
            f"✅ 인증 패널을 게시했어요. 대상 서버: **{config['target_server']}**", ephemeral=True
        )


    @discord.ui.button(label="메시지 삭제", style=discord.ButtonStyle.danger, emoji="🧹", custom_id="adminpanel:purge", row=1)
    async def purge_messages(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MessagePurgeModal())


class AdminPanelView(SafeView):
    """관리자 설정을 한 채널에서 처리하는 영구 패널"""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AdminPanelChannelSelect("alarm_channel", "게임 일정 알림", "adminpanel:alarm_channel"))
        self.add_item(AdminPanelChannelSelect("log_channel", "관리 로그", "adminpanel:log_channel"))
        self.add_item(AdminPanelRoleSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 이 패널을 사용할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="인증 관리", style=discord.ButtonStyle.primary, custom_id="adminpanel:verify_open", row=2)
    async def verification_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_verification_admin_embed(interaction.guild), view=VerificationAdminView(), ephemeral=True)

    @discord.ui.button(label="아티팩트 상대 서버", style=discord.ButtonStyle.secondary, emoji="🏺", custom_id="adminpanel:artifact_opponent", row=4)
    async def set_artifact_opponent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ArtifactOpponentModal())

    @discord.ui.button(label="알람 문구 패널 게시", style=discord.ButtonStyle.primary, emoji="📋", custom_id="adminpanel:alarm_panel", row=4)
    async def create_alarm_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📋 알람 문구 설정",
            description="변경할 알람 버튼을 눌러 안내 문구를 설정해주세요.",
            color=discord.Color.blurple(),
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(embed=embed, view=AlarmMessagePanelView())
        await interaction.followup.send("✅ 알람 문구 패널을 게시했어요.", ephemeral=True)


    @discord.ui.button(label="설정 새로고침", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="adminpanel:refresh", row=4)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_admin_panel_embed(interaction.guild), view=self)


class TicketView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="티켓 열기",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="ticket:open",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_ticket(interaction)


def resilient_loop(callback):
    @wraps(callback)
    async def run(self):
        try:
            await callback(self)
        except Exception:
            logger.exception("백그라운드 작업 실패: %s (다음 주기에 재시도)", callback.__name__)
    return run


class Automation(commands.Cog):
    """자동 역할 부여, 입장/퇴장 알림, 로그 채널 등 자동화 기능 (전부 슬래시(/) 명령어 전용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._background_tasks = []
        self._artifact_sync_lock = asyncio.Lock()
        bot.add_view(TicketView())
        bot.add_view(TicketCloseView())
        bot.add_view(AlarmMessagePanelView())
        bot.add_view(AlarmRolePanelView())
        bot.add_view(AdminPanelView())
        bot.add_view(VerificationAdminView())

    async def cog_load(self):
        self.alarm_loop.start()
        self.official_notice_loop.start()
        self.artifact_result_loop.start()
        self.artifact_history_loop.start()
        for callback in (self._refresh_admin_panel_messages, self._ensure_alarm_role_panel):
            self._background_tasks.append(asyncio.create_task(self._startup_task(callback)))

    async def _startup_task(self, callback):
        try:
            await callback()
        except Exception:
            logger.exception("초기화 작업 실패: %s", callback.__name__)

    async def cog_unload(self):
        loops = (self.alarm_loop, self.official_notice_loop, self.artifact_result_loop, self.artifact_history_loop)
        pending = [loop.get_task() for loop in loops if loop.get_task()]
        for loop in loops:
            loop.cancel()
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*pending, *self._background_tasks, return_exceptions=True)

    async def _refresh_admin_panel_messages(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            config = get_guild_config(guild.id)
            channel_id = config.get("admin_panel_channel_id")
            message_id = config.get("admin_panel_message_id")
            if not channel_id or not message_id:
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(
                    embed=build_admin_panel_embed(guild),
                    view=AdminPanelView(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("관리자 패널 갱신 실패: guild=%s message=%s", guild.id, message_id)

    async def _ensure_alarm_role_panel(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(ALARM_ROLE_GUILD_ID)
        channel = guild.get_channel(ALARM_ROLE_CHANNEL_ID) if guild else None
        if not isinstance(channel, discord.TextChannel):
            logger.warning("알람 역할 패널 채널을 찾을 수 없습니다: %s", ALARM_ROLE_CHANNEL_ID)
            return

        embed = discord.Embed(
            title="🔔 알람 설정",
            description=(
                "원하는 보스/이벤트 알람만 골라서 받을 수 있어요.\n"
                "받고 싶은 알람의 버튼을 누르면 해당 역할이 부여되고, 이후 그 알람이 뜰 때 \n**#알람-채널**에서 멘션(핑)을 받아요.\n\n"
                "- ✅ 버튼을 누르면 → 역할 부여 (알림 받기 시작)\n"
                "- ❌ 같은 버튼을 다시 누르면 → 역할 해제 (알림 그만 받기)\n"
                "- 여러 개 동시에 선택 가능해요. 필요한 것만 골라서 받으세요!\n\n"
                "> 💡 아티쟁 전략 공유는 **어비스**를 클릭하여 권한을 받아주세요."
            ),
            color=discord.Color.gold(),
        )
        config = get_guild_config(guild.id)
        message_id = config.get("alarm_role_panel_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=AlarmRolePanelView())
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            message = await channel.send(embed=embed, view=AlarmRolePanelView())
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("알람 역할 패널 게시 실패: channel=%s", ALARM_ROLE_CHANNEL_ID)
            return
        set_guild_config(guild.id, "alarm_role_panel_message_id", message.id)

    @tasks.loop(minutes=30)
    @resilient_loop
    async def artifact_history_loop(self):
        await self._sync_artifact_records()

    @artifact_history_loop.before_loop
    async def before_artifact_history_loop(self):
        await self.bot.wait_until_ready()

    async def _sync_artifact_records(self):
        async with self._artifact_sync_lock:
            await self._sync_artifact_records_unlocked()

    async def _sync_artifact_records_unlocked(self):
        opponents = {
            config.get("artifact_opponent_server")
            for guild in self.bot.guilds
            for config in [get_guild_config(guild.id)]
            if config.get("artifact_opponent_server")
        }
        for opponent_server in opponents:
            try:
                history = await aion2_scraper.get_artifact_server_history(opponent_server)
                if not history or not history.get("records"):
                    continue
                try:
                    record = await aion2_scraper.get_artifact_server_record(opponent_server)
                    if record:
                        history["record"] = record
                except Exception:
                    logger.exception("아티팩트 요약 조회 실패: 기존 요약 보존")
                save_artifact_history(history)
            except Exception:
                logger.exception("아티팩트 %s 기록 저장 실패", opponent_server)

    @tasks.loop(minutes=5)
    @resilient_loop
    async def official_notice_loop(self):
        articles = await aion2_scraper.get_latest_official_articles()
        if not articles:
            return
        for guild in self.bot.guilds:
            if guild.id != OFFICIAL_NOTICE_GUILD_ID:
                continue
            cfg = get_guild_config(guild.id)
            seen = list(dict.fromkeys(cfg.get("official_seen_articles", [])))
            initialized = set(cfg.get("official_initialized_categories", []))
            if cfg.get("official_notice_initialized") and "official_initialized_categories" not in cfg:
                initialized.update(aion2_scraper.OFFICIAL_BOARD_URLS)
            categories = {article["category"] for article in articles}
            for category in categories - initialized:
                seen.extend(article["url"] for article in articles if article["category"] == category)
                initialized.add(category)
            # Save the initial baseline even when the guild had no configuration yet.
            set_guild_config(guild.id, "official_initialized_categories", sorted(initialized))
            set_guild_config(guild.id, "official_seen_articles", list(dict.fromkeys(seen))[-1000:])
            for article in reversed(articles):
                if article["url"] in seen:
                    continue
                category = article["category"] if article["category"] in {"공지", "이벤트"} else "기타"
                channel = guild.get_channel(OFFICIAL_NOTICE_CHANNEL_IDS[category])
                if not isinstance(channel, discord.TextChannel):
                    continue
                embed = discord.Embed(title=f"📢 {article['category']} 새 글",
                                      description=f"**{article['title']}**"[:4096], url=article["url"],
                                      color=discord.Color.blurple(), timestamp=datetime.datetime.now(KST))
                embed.set_footer(text="AION2 공식 홈페이지")
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("공지 전송 실패: guild=%s category=%s", guild.id, category)
                    continue
                seen.append(article["url"])
                seen = list(dict.fromkeys(seen))[-1000:]
                set_guild_config(guild.id, "official_seen_articles", seen)

    @official_notice_loop.before_loop
    async def before_official_notice_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=20)
    @resilient_loop
    async def alarm_loop(self):
        now = datetime.datetime.now(KST)
        start = now - datetime.timedelta(minutes=5)
        current = start.replace(second=0, microsecond=0)
        due_alarms = []
        while current <= now:
            due_alarms.extend((current, key, message) for key, message in get_due_alarm_messages(current))
            current += datetime.timedelta(minutes=1)

        for guild in self.bot.guilds:
            channel_id = get_guild_config(guild.id).get("alarm_channel")
            channel = guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue

            guild_config = get_guild_config(guild.id)
            ping_role_id = guild_config.get("alarm_ping_role")
            default_ping_role = guild.get_role(ping_role_id) if ping_role_id else None
            custom_messages = guild_config.get("alarm_messages", {})

            sent = guild_config.get("sent_alarm_keys", [])
            for alarm_time, schedule_key, alarm_message in due_alarms:
                alarm_key = f"{alarm_time:%Y-%m-%d %H:%M}:{schedule_key}"
                if alarm_key in sent:
                    continue
                try:
                    event_name = schedule_key.split(":", 1)[0]
                    embed = build_alarm_embed(
                        alarm_message, schedule_key, custom_messages.get(event_name)
                    )
                    if alarm_time < now.replace(second=0, microsecond=0):
                        embed.set_footer(text=f"{alarm_time:%H:%M} 예정 알림 · 지연 전달")
                    if guild.id == ALARM_ROLE_GUILD_ID:
                        ping_role = get_alarm_role(guild, event_name)
                    else:
                        ping_role = default_ping_role
                    content = ping_role.mention if ping_role else None
                    await channel.send(
                        content=content,
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                    sent.append(alarm_key)
                    sent = sent[-500:]
                    set_guild_config(guild.id, "sent_alarm_keys", sent)
                except discord.HTTPException:
                    logger.exception("알람 전송 실패: guild=%s", guild.id)
                    continue


    @alarm_loop.before_loop
    async def before_alarm_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2)
    @resilient_loop
    async def artifact_result_loop(self):
        await self._check_artifact_result(datetime.datetime.now(KST))

    @artifact_result_loop.before_loop
    async def before_artifact_result_loop(self):
        await self.bot.wait_until_ready()

    async def _check_artifact_result(self, now: datetime.datetime):
        expected_date = now.date()
        if now.hour < 3:
            expected_date -= datetime.timedelta(days=1)
        elif (now.hour, now.minute) < (22, 30):
            return
        if expected_date.weekday() not in {2, 5}:
            return

        try:
            result = await aion2_scraper.get_latest_artifact_result(expected_date)
        except Exception:
            logger.exception("아티팩트쟁 결과 수집 실패")
            return
        if not result:
            return

        result_saved = False
        for guild in self.bot.guilds:
            config = get_guild_config(guild.id)
            if guild.id == OFFICIAL_NOTICE_GUILD_ID:
                channel_id = OFFICIAL_NOTICE_CHANNEL_IDS["기타"]
            else:
                channel_id = config.get("alarm_channel")
            channel = guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                continue

            result_key = f"{expected_date.isoformat()}:{result['completion']}"
            sent = config.get("sent_artifact_result_keys", [])
            if result_key in sent:
                continue
            embed = discord.Embed(
                title="🏺 아티팩트쟁 결과 집계완료",
                description=f"✅ **{result['completion']} 결과 집계완료**",
                url=result["url"],
                color=discord.Color.gold(),
                timestamp=now,
            )
            record = result.get("record")
            if record:
                embed.add_field(
                    name="아티쟁 결과",
                    value=(
                        f"브리트라 **{record['breitra_total']}** : "
                        f"**{record['opponent_total']}** {record['opponent_server']}"
                    ),
                    inline=False,
                )
                territories = record.get("territories", [])
                if territories:
                    territory_lines = []
                    for layer in ("하층", "중층"):
                        layer_items = [item for item in territories if item["layer"] == layer]
                        if not layer_items:
                            continue
                        territory_lines.append(layer)
                        territory_lines.append("")
                        territory_lines.extend(
                            f"{item['name']}: **{item['server']}**" for item in layer_items
                        )
                        territory_lines.append("")
                    embed.add_field(
                        name="지역별 점령 결과",
                        value="\n".join(territory_lines).rstrip(),
                        inline=False,
                    )
            embed.set_footer(text="아툴 비공식 참고용 통계 · 원본 보기")
            try:
                await channel.send(embed=embed)
                set_guild_config(guild.id, "sent_artifact_result_keys", (sent + [result_key])[-100:])
                result_saved = True
            except discord.HTTPException:
                logger.exception("아티팩트 결과 전송 실패: guild=%s", guild.id)
                continue
        if result_saved:
            await self._sync_artifact_records()

    # ---------- 설정 명령어 ----------
    @app_commands.command(name="알람채널설정", description="게임 일정 알람을 보낼 채널을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="게임 일정 알람을 보낼 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_alarm_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_config(interaction.guild.id, "alarm_channel", channel.id)
        await interaction.response.send_message(
            f"✅ 게임 일정 알람 채널을 {channel.mention} 으로 설정했어요."
        )

    @app_commands.command(name="아티설정", description="브리트라의 아티팩트쟁 상대 서버를 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(server="브리트라의 상대 서버 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_artifact_opponent(self, interaction: discord.Interaction, server: str):
        server = server.strip()
        if not 1 <= len(server) <= 30 or server == "브리트라":
            await interaction.response.send_message(
                "❌ 브리트라가 아닌 상대 서버 이름을 입력해주세요.", ephemeral=True
            )
            return
        set_guild_config(interaction.guild.id, "artifact_opponent_server", server)
        await interaction.response.send_message(
            f"✅ 아티팩트쟁 상대 서버를 **{server}**로 설정했어요."
        )

    @app_commands.command(name="아티확인", description="브리트라와 설정한 상대 서버의 아티팩트 전적을 확인합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def check_artifact_record(self, interaction: discord.Interaction):
        opponent_server = get_guild_config(interaction.guild.id).get("artifact_opponent_server")
        if not opponent_server:
            await interaction.response.send_message(
                "❌ 먼저 `/아티설정`으로 브리트라의 상대 서버를 설정해주세요.", ephemeral=True
            )
            return

        await interaction.response.defer()
        history = load_artifact_history(f"브리트라 VS {opponent_server}")
        if not history:
            direct_chapter_history = load_artifact_chapter_matchups(
                f"브리트라 VS {opponent_server}"
            )
            if direct_chapter_history:
                chapter_history = load_artifact_chapter_history(opponent_server)
                related_chapter_history = []
                direct_servers = {"브리트라", opponent_server}
                for chapter in chapter_history:
                    related_records = [
                        item
                        for item in chapter["records"]
                        if set(item["matchup"].split(" VS ")) != direct_servers
                    ]
                    if related_records:
                        related_chapter_history.append(
                            {"chapter": chapter["chapter"], "records": related_records}
                        )

                embed = discord.Embed(
                    title=f"🏺 이전 아티팩트 매칭 기록 · 브리트라 VS {opponent_server}",
                    description=(
                        "챕터 기록에서 직접 매칭과 관련 서버 매칭을 모두 찾았어요."
                    ),
                    color=discord.Color.gold(),
                )
                for item in direct_chapter_history:
                    embed.add_field(
                        name=f"{item['chapter']} · 직접 매칭",
                        value=f"{item['matchup']}: **{item['score']}**",
                        inline=False,
                    )
                for chapter in related_chapter_history:
                    lines = [
                        f"{item['matchup']}: **{item['score']}**"
                        for item in chapter["records"]
                    ]
                    embed.add_field(
                        name=f"{chapter['chapter']} · 관련 매칭",
                        value="\n".join(lines),
                        inline=False,
                    )
                embed.set_footer(text="새로운 직접 매칭 기록이 등록되면 누적 전적으로 표시합니다.")
                await send_embed_pages(interaction, embed)
                return

            chapter_history = load_artifact_chapter_history(opponent_server)
            if not chapter_history:
                await interaction.followup.send(
                    f"❌ **{opponent_server}**의 저장된 아티팩트 기록이 없어요. 관리자에게 기록 데이터를 등록해달라고 해주세요."
                )
                return

            embed = discord.Embed(
                title=f"🏺 아티팩트 과거 기록 · {opponent_server}",
                description=(
                    f"브리트라 VS {opponent_server} 직접 매칭 기록은 아직 없어요.\n"
                    f"대신 **{opponent_server}**이 포함된 과거 챕터 기록입니다."
                ),
                color=discord.Color.gold(),
            )
            for chapter in chapter_history:
                lines = [
                    f"{item['matchup']}: **{item['score']}**"
                    for item in chapter["records"]
                ]
                embed.add_field(
                    name=chapter["chapter"],
                    value="\n".join(lines),
                    inline=False,
                )
            embed.set_footer(text="직접 매칭 기록이 등록되면 해당 전적을 우선 표시합니다.")
            await send_embed_pages(interaction, embed)
            return

        record = history.get("record")
        if not record:
            await interaction.followup.send(
                "❌ 저장된 기록에 요약 전적이 아직 없어요. 기록 데이터를 다시 등록해주세요."
            )
            return

        matchup = record.get("matchup")
        if not matchup:
            return await interaction.followup.send("요약 전적이 아직 준비되지 않았어요. 다음 수집 후 다시 확인해주세요.")
        opponent_captures = (
            f"{record['opponent_capture_count']}회"
            if record["opponent_capture_count"] is not None
            else "확인 불가"
        )
        breitra_captures = (
            f"{record['breitra_capture_count']}회"
            if record["breitra_capture_count"] is not None
            else "확인 불가"
        )
        embed = discord.Embed(
            title=f"🏺 아티팩트 전적 · 브리트라 vs {opponent_server}",
            url=record["url"],
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="총 전적",
            value=f"브리트라 점령 **{breitra_captures}** · {opponent_server} 점령 **{opponent_captures}**",
            inline=False,
        )
        embed.add_field(
            name=f"지난 회차 ({matchup['round']})",
            value=f"브리트라 **{matchup['breitra_round']}** {matchup['breitra_result']} · {opponent_server} **{matchup['opponent_round']}** {matchup['opponent_result']}",
            inline=False,
        )
        embed.add_field(
            name="브리트라 상대 누적 전적",
            value=f"브리트라 **{matchup['breitra_total']}** : **{matchup['opponent_total']}** {opponent_server}",
            inline=False,
        )
        if history.get("records"):
            history_lines = [
                f"{item['date']} ({item['round']}): {', '.join(item['scores']) or '점수 확인 불가'}"
                for item in history["records"]
            ]
            embed.add_field(name="회차별 기록", value="\n".join(history_lines), inline=False)
        else:
            embed.add_field(name="회차별 기록", value="상세 기록을 찾지 못했어요.", inline=False)
        if record.get("completion"):
            embed.set_footer(text=f"{record['completion']} · 아툴 비공식 참고용 통계")
        else:
            embed.set_footer(text="아툴 비공식 참고용 통계")
        await send_embed_pages(interaction, embed)

    @app_commands.command(name="관리자패널", description="버튼으로 봇 설정을 관리하는 패널을 게시합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def create_admin_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await interaction.channel.send(
            embed=build_admin_panel_embed(interaction.guild),
            view=AdminPanelView(),
        )
        config = get_guild_config(interaction.guild.id)
        config["admin_panel_channel_id"] = interaction.channel.id
        config["admin_panel_message_id"] = message.id
        data = load_config()
        data[str(interaction.guild.id)] = config
        save_config(data)
        await interaction.followup.send("✅ 관리자 패널을 게시했어요.", ephemeral=True)


    @app_commands.command(name="알람메시지설정", description="특정 알람에 표시할 안내 문구를 입력창(모달)으로 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(event="문구를 설정할 알람 종류")
    @app_commands.choices(
        event=[app_commands.Choice(name=name, value=name) for name in EVENT_NAMES]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def set_alarm_message(
        self, interaction: discord.Interaction, event: app_commands.Choice[str]
    ):
        current = (
            get_guild_config(interaction.guild.id)
            .get("alarm_messages", {})
            .get(event.value, "")
        )
        await interaction.response.send_modal(
            AlarmMessageModal(event.value, interaction.guild.id, current)
        )

    @app_commands.command(
        name="알람메시지설정패널", description="버튼을 눌러 알람 문구를 설정할 수 있는 패널을 게시합니다."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def create_alarm_message_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 알람 문구 설정",
            description="아래 버튼 중 문구를 바꾸고 싶은 알람을 눌러주세요. 입력창이 뜨면 안내 문구를 적고 제출하면 돼요.",
            color=discord.Color.blurple(),
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(embed=embed, view=AlarmMessagePanelView())
        await interaction.followup.send("✅ 알람 문구 설정 패널을 게시했어요.", ephemeral=True)


    @app_commands.command(name="로그채널설정", description="관리 로그(입장/퇴장/삭제 등)를 보낼 채널을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="로그를 보낼 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_config(interaction.guild.id, "log_channel", channel.id)
        await interaction.response.send_message(f"✅ 로그 채널을 {channel.mention} 으로 설정했어요.")

    @app_commands.command(name="티켓패널생성", description="누구나 티켓을 열 수 있는 버튼 패널을 게시합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def create_ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 문의 티켓",
            description=(
                "버그 제보, 인증 문제, 개인적인 신고나 상담이 필요하면 아래 버튼을 눌러주세요.\n"
                "버튼을 누르면 **본인과 운영진만 볼 수 있는 전용 채널**이 자동으로 생성돼요.\n\n"
                "- 🐛 게임 버그, 인증 오류 신고\n"
                "- 🙋 개인적인 문의나 상담\n"
                "- 🚨 다른 멤버 신고 (증거가 있다면 함께 첨부해주세요)\n\n"
                "> 💬 일반적인 질문이나 잡담은 채팅 채널을 이용해주세요. 문의가 끝나면 채널은 정리될 수 있어요."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.channel.send(embed=embed, view=TicketView())
        await interaction.followup.send("✅ 티켓 패널을 게시했어요.", ephemeral=True)


    # ---------- 슬래시 명령어 에러 처리 ----------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ 이 명령어를 실행할 권한이 없어요."
        else:
            logger.error("자동화 명령 실패", exc_info=error)
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
                except discord.HTTPException:
                    logger.exception("자동 역할 부여 실패")

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
            try:
                await channel.send(f"`[{timestamp}]` {text}", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.exception("관리 로그 전송 실패")


async def setup(bot: commands.Bot):
    await bot.add_cog(Automation(bot))