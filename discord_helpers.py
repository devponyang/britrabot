"""Shared interaction errors, role buttons, moderation and ticket operations."""
import logging
import re

import discord

logger = logging.getLogger(__name__)
TICKETS_IN_FLIGHT = set()


async def respond(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def send_embed_pages(interaction, embed):
    """Split history fields without exceeding Discord's field or embed limits."""
    base = embed.to_dict()
    fields = base.pop("fields", [])
    current = discord.Embed.from_dict(base)
    for field in fields:
        value = field["value"] or "\u200b"
        for offset in range(0, len(value), 1024):
            name = field["name"][:240] + (" (계속)" if offset else "")
            chunk = value[offset:offset + 1024]
            if len(current.fields) >= 25 or len(current) + len(name) + len(chunk) > 6000:
                await interaction.followup.send(embed=current)
                current = discord.Embed.from_dict(base)
            current.add_field(name=name, value=chunk, inline=field.get("inline", False))
    await interaction.followup.send(embed=current)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        logger.error("버튼 처리 실패", exc_info=error)
        await respond(interaction, "⚠️ 작업에 실패했어요. 봇 권한을 확인하거나 잠시 후 다시 시도해주세요.")


class SafeModal(discord.ui.Modal):
    async def interaction_check(self, interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.administrator:
            await respond(interaction, "관리자만 설정을 변경할 수 있어요.")
            return False
        return True

    async def on_error(self, interaction, error):
        logger.error("입력창 처리 실패", exc_info=error)
        await respond(interaction, "⚠️ 설정을 처리하지 못했어요. 봇 권한과 입력 내용을 확인해주세요.")


async def purge_messages(interaction, amount):
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        return await respond(interaction, "관리자만 메시지를 삭제할 수 있어요.")
    if not 1 <= amount <= 100:
        return await respond(interaction, "1부터 100 사이의 숫자를 입력해주세요.")
    permissions = interaction.channel.permissions_for(interaction.guild.me)
    if not (permissions.manage_messages and permissions.read_message_history and permissions.view_channel):
        return await respond(interaction, "봇에게 이 채널의 메시지 관리와 기록 보기 권한이 필요해요.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 메시지 {len(deleted)}개를 삭제했어요.", ephemeral=True)


async def toggle_role(interaction, guild_id, role_id):
    if interaction.guild is None or interaction.guild.id != guild_id:
        return await respond(interaction, "이 서버에서는 사용할 수 없는 버튼이에요.")
    role = interaction.guild.get_role(role_id)
    if role is None or not role.is_assignable() or not interaction.guild.me.guild_permissions.manage_roles:
        return await respond(interaction, "알람 역할을 관리할 수 없어요. 봇 권한과 역할 순서를 확인해주세요.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role, reason="알람 역할 해제")
        message = f"✅ {role.mention} 역할을 해제했어요."
    else:
        await interaction.user.add_roles(role, reason="알람 역할 선택")
        message = f"✅ {role.mention} 역할을 받았어요."
    await interaction.followup.send(message, ephemeral=True)


class TicketCloseView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.secondary, custom_id="ticket:close")
    async def close_ticket(self, interaction, button):
        channel = interaction.channel
        topic = getattr(channel, "topic", "") or ""
        match = re.fullmatch(r"티켓 대상: (\d+)", topic)
        if not match:
            return await respond(interaction, "열린 티켓 채널에서만 사용할 수 있어요.")
        owner_id = int(match.group(1))
        if interaction.user.id != owner_id and not interaction.user.guild_permissions.administrator:
            return await respond(interaction, "티켓 작성자와 관리자만 닫을 수 있어요.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        owner = interaction.guild.get_member(owner_id)
        if owner:
            overwrite = channel.overwrites_for(owner)
            overwrite.send_messages = False
            await channel.set_permissions(owner, overwrite=overwrite, reason="티켓 종료")
        await channel.edit(name=f"종료-{channel.name}"[:100], topic=f"종료 티켓 대상: {owner_id}")
        await interaction.followup.send("✅ 티켓을 닫았어요. 대화 기록은 보관됩니다.", ephemeral=True)


async def open_ticket(interaction):
    if interaction.guild is None:
        return await respond(interaction, "서버에서 이용해주세요.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    key = (guild.id, interaction.user.id)
    if key in TICKETS_IN_FLIGHT:
        return await respond(interaction, "티켓을 생성 중이에요. 잠시 기다려주세요.")
    TICKETS_IN_FLIGHT.add(key)
    try:
        topic = f"티켓 대상: {interaction.user.id}"
        existing = next((c for c in guild.text_channels if c.topic == topic), None)
        if existing:
            return await respond(interaction, f"이미 열려 있는 티켓이 있어요: {existing.mention}")
        safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]", "-", interaction.user.display_name).strip("-")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  read_message_history=True, manage_channels=True, manage_messages=True),
        }
        channel = await guild.create_text_channel(f"티켓-{safe_name or interaction.user.id}"[:100],
                                                  overwrites=overwrites, topic=topic, reason="문의 티켓 생성")
        await channel.send(f"{interaction.user.mention} 님, 문의 내용을 남겨주세요. 서버 관리자가 확인합니다.",
                           view=TicketCloseView(), allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        await respond(interaction, f"✅ 티켓 채널 {channel.mention}을 생성했어요.")
    finally:
        TICKETS_IN_FLIGHT.discard(key)
