import asyncio
import datetime as dt
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from discord_helpers import SafeView, respond
from server_scope import allowed_guild, ALLOWED_GUILD_ID
from storage import BASE_DIR, load_json, save_json
from cogs.coupons import KST, parse_expiry

FILE = BASE_DIR / 'recruitment.json'
logger = logging.getLogger(__name__)
LABELS = {'party': '파티 모집', 'legion': '레기온 홍보'}
PANEL_CHANNEL_ID = 1548041064961810482
POST_CHANNEL_IDS = {'party': 1547063601611804803, 'legion': 1547063786320568350}


def is_closed(row):
    return row.get('closed', False) or (row['kind'] == 'party' and dt.datetime.fromisoformat(row['starts']) <= dt.datetime.now(KST))


def post_embed(row):
    closed = is_closed(row)
    full = row['kind'] == 'party' and len(row['members']) >= row['size']
    status = '모집 종료' if closed else '정원 마감' if full else '모집 중'
    embed = discord.Embed(title=f"{LABELS[row['kind']]} · {row['title']}", description=row['details'], color=discord.Color.greyple() if closed else discord.Color.green())
    embed.add_field(name='모집 상태', value=status)
    embed.add_field(name='작성자 / 문의', value=f"<@{row['owner']}>")
    if row['kind'] == 'party':
        timestamp = int(dt.datetime.fromisoformat(row['starts']).timestamp())
        embed.add_field(name='출발 시각', value=f'<t:{timestamp}:f> (<t:{timestamp}:R>)', inline=False)
        embed.add_field(name=f"참여 인원 {len(row['members'])}/{row['size']} (모집자 포함)", value=' '.join(f'<@{uid}>' for uid in row['members']), inline=False)
    embed.set_footer(text='버튼으로 조작하세요. 수정·종료는 작성자와 관리자만 가능합니다.')
    return embed


class RecruitModal(discord.ui.Modal):
    def __init__(self, cog, kind, row=None, message_id=None):
        super().__init__(title=LABELS[kind] + (' 수정' if row else ' 작성'), timeout=300)
        self.cog, self.kind, self.message_id = cog, kind, message_id
        self.subject = discord.ui.TextInput(label='콘텐츠 / 레기온 이름', max_length=70, default=row['title'] if row else None)
        self.details = discord.ui.TextInput(label='조건·소개·주 활동 시간·문의 방법', style=discord.TextStyle.paragraph, max_length=1200, default=row['details'] if row else None)
        self.add_item(self.subject)
        self.add_item(self.details)
        if kind == 'party':
            self.capacity = discord.ui.TextInput(label='모집자 포함 정원 (2~24명)', default=str(row['size']) if row else '6', max_length=2)
            self.starts = discord.ui.TextInput(label='출발 시각: YYYY-MM-DD HH:MM (한국 시간)', default=dt.datetime.fromisoformat(row['starts']).strftime('%Y-%m-%d %H:%M') if row else None, max_length=16)
            self.add_item(self.capacity)
            self.add_item(self.starts)

    async def interaction_check(self, interaction):
        return allowed_guild(interaction.guild)

    async def on_submit(self, interaction):
        title, details = self.subject.value.strip(), self.details.value.strip()
        if not title or not details:
            return await respond(interaction, '이름과 소개를 입력해주세요.')
        changes = {'title': title, 'details': details}
        if self.kind == 'party':
            try:
                size = int(self.capacity.value)
                starts = parse_expiry(self.starts.value)
                if len(self.starts.value.strip()) != 16 or not 2 <= size <= 24 or starts <= dt.datetime.now(KST):
                    raise ValueError()
                changes.update(size=size, starts=starts.isoformat())
            except ValueError:
                return await respond(interaction, '정원은 2~24명, 출발은 미래 시각을 YYYY-MM-DD HH:MM 형식으로 입력해주세요.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.lock:
            data = load_json(FILE)
            posts = data.setdefault('posts', {})
            if self.message_id:
                row = posts.get(str(self.message_id))
                if not row or not self.cog.can_manage(interaction, row):
                    return await respond(interaction, '작성자와 관리자만 수정할 수 있어요.')
                if self.kind == 'party' and changes['size'] < len(row['members']):
                    return await respond(interaction, '정원을 현재 참가 인원보다 줄일 수 없어요.')
                if not row.get('closed') and any(other is not row and other['kind'] == self.kind and not is_closed(other) and (other['owner'] == row['owner'] or self.kind == 'legion' and other['title'].casefold() == title.casefold()) for other in posts.values()):
                    return await respond(interaction, '이미 진행 중인 모집글이 있어요. 먼저 해당 글을 종료해주세요.')
                row.update(changes)
            else:
                panel = data.get('panel')
                if not panel or interaction.channel_id != PANEL_CHANNEL_ID or panel['channel_id'] != PANEL_CHANNEL_ID:
                    return await respond(interaction, '관리자가 지정한 최신 패널 채널에서 이용해주세요.')
                if any(row['owner'] == interaction.user.id and row['kind'] == self.kind and not is_closed(row) for row in posts.values()):
                    return await respond(interaction, '진행 중인 모집글이 있어요. 기존 글을 수정하거나 종료해주세요.')
                if self.kind == 'legion' and any(row['kind'] == 'legion' and not is_closed(row) and row['title'].casefold() == title.casefold() for row in posts.values()):
                    return await respond(interaction, '같은 이름의 레기온 홍보글이 이미 있어요.')
                destination = interaction.guild.get_channel(POST_CHANNEL_IDS[self.kind])
                if destination is None:
                    return await respond(interaction, '모집글 게시 채널을 찾을 수 없어요. 관리자에게 문의해주세요.')
                row = dict(changes, kind=self.kind, owner=interaction.user.id, members=[interaction.user.id], closed=False, channel_id=destination.id)
                message = await destination.send(embed=post_embed(row), view=PostView(self.cog, self.kind), allowed_mentions=discord.AllowedMentions.none())
                posts[str(message.id)] = row
            save_json(FILE, data)
            if self.message_id:
                await self.cog.refresh_post(interaction.guild, self.message_id, row)
                save_json(FILE, data)
        await respond(interaction, f"✅ <#{row['channel_id']}>에 모집글을 반영했어요.")

    async def on_error(self, interaction, error):
        logger.error('모집 입력 처리 실패', exc_info=error)
        await respond(interaction, '처리에 실패했어요. 잠시 후 다시 시도해주세요.')


class PanelView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        for kind in LABELS:
            button = discord.ui.Button(label=LABELS[kind] + ' 작성', style=discord.ButtonStyle.primary, custom_id=f'recruit:panel:{kind}')
            async def clicked(interaction, kind=kind):
                panel = load_json(FILE).get('panel')
                if interaction.channel_id != PANEL_CHANNEL_ID or not panel or panel['message_id'] != interaction.message.id:
                    return await respond(interaction, f'통합 패널 <#{PANEL_CHANNEL_ID}>을 이용해주세요.')
                await interaction.response.send_modal(RecruitModal(cog, kind))
            button.callback = clicked
            self.add_item(button)


class PostView(SafeView):
    def __init__(self, cog, kind):
        super().__init__(timeout=None)
        actions = [('join', '참가'), ('leave', '참가 취소')] if kind == 'party' else []
        actions += [('edit', '모집글 수정'), ('close', '모집 종료'), ('reopen', '모집 재개')]
        for action, label in actions:
            button = discord.ui.Button(label=label, custom_id=f'recruit:{kind}:{action}', style=discord.ButtonStyle.secondary)
            async def clicked(interaction, action=action):
                await cog.action(interaction, action)
            button.callback = clicked
            self.add_item(button)


class Recruitment(commands.Cog):
    def __init__(self, bot):
        self.bot, self.lock = bot, asyncio.Lock()
        self._last_audit = -1000.0

    async def cog_load(self):
        self.bot.add_view(PanelView(self))
        for kind in LABELS:
            self.bot.add_view(PostView(self, kind))
        self.refresh.start()

    async def cog_unload(self):
        task = self.refresh.get_task()
        self.refresh.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def can_manage(interaction, row):
        return interaction.user.id == row['owner'] or interaction.user.guild_permissions.administrator

    async def refresh_post(self, guild, message_id, row, *, audit=False):
        embed = post_embed(row)
        signature = str(embed.to_dict())
        unchanged = row.get('signature') == signature
        if unchanged and not audit:
            return
        channel = guild.get_channel(row['channel_id'])
        if channel is None:
            if not audit:
                return
            channel = await guild.fetch_channel(row['channel_id'])
        message = await channel.fetch_message(int(message_id))
        if unchanged:
            return
        await message.edit(content=None, embed=embed, view=PostView(self, row['kind']))
        row['signature'] = signature

    async def action(self, interaction, action):
        if action == 'edit':
            row = load_json(FILE).get('posts', {}).get(str(interaction.message.id))
            if not row or not self.can_manage(interaction, row):
                return await respond(interaction, '작성자와 관리자만 수정할 수 있어요.')
            return await interaction.response.send_modal(RecruitModal(self, row['kind'], row, interaction.message.id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            row = data.get('posts', {}).get(str(interaction.message.id))
            if not row:
                return await respond(interaction, '등록된 모집글이 아니에요.')
            uid = interaction.user.id
            if action in ('close', 'reopen'):
                if not self.can_manage(interaction, row):
                    return await respond(interaction, '작성자와 관리자만 모집 상태를 변경할 수 있어요.')
                if action == 'reopen':
                    if any(other is not row and other['kind'] == row['kind'] and not is_closed(other) and (other['owner'] == row['owner'] or row['kind'] == 'legion' and other['title'].casefold() == row['title'].casefold()) for other in data['posts'].values()):
                        return await respond(interaction, '이미 진행 중인 모집글이 있어요.')
                    if row['kind'] == 'party' and dt.datetime.fromisoformat(row['starts']) <= dt.datetime.now(KST):
                        return await respond(interaction, '먼저 출발 시각을 미래로 수정해주세요.')
                row['closed'] = action == 'close'
            elif row['kind'] == 'party' and action == 'join':
                if is_closed(row) or len(row['members']) >= row['size']:
                    return await respond(interaction, '모집이 마감됐어요.')
                if uid not in row['members']:
                    row['members'].append(uid)
            elif row['kind'] == 'party' and action == 'leave':
                if uid == row['owner']:
                    return await respond(interaction, '모집자는 참가 취소 대신 모집 종료를 눌러주세요.')
                if uid in row['members']:
                    row['members'].remove(uid)
            save_json(FILE, data)
            await self.refresh_post(interaction.guild, interaction.message.id, row)
            save_json(FILE, data)
        await respond(interaction, '✅ 모집글을 갱신했어요.')

    @app_commands.command(name='모집패널설정', description='지정된 채널에 파티·레기온 통합 모집 패널 하나를 설치합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            channel = interaction.guild.get_channel(PANEL_CHANNEL_ID)
            if channel is None:
                return await respond(interaction, '통합 패널 채널을 찾을 수 없어요. 채널 보기 권한을 확인해주세요.')
            existing = data.get('panel')
            embed = discord.Embed(title='📋 파티 모집 · 레기온 홍보', description=(
                '아래 버튼을 눌러 모집글을 작성해주세요.\n\n'
                f'⚔️ **파티 모집** → <#{POST_CHANNEL_IDS["party"]}>\n'
                f'🛡️ **레기온 홍보** → <#{POST_CHANNEL_IDS["legion"]}>\n\n'
                '작성한 글은 해당 채널에 게시됩니다. 참가·수정·종료는 게시된 글의 버튼을 이용해주세요.'), color=discord.Color.blurple())
            message = None
            if existing and existing['channel_id'] == PANEL_CHANNEL_ID:
                try:
                    message = await channel.fetch_message(existing['message_id'])
                    await message.edit(content=None, embed=embed, view=PanelView(self))
                except discord.NotFound:
                    pass
            if message is None:
                message = await channel.send(embed=embed, view=PanelView(self))
            data['panel'] = {'channel_id': channel.id, 'message_id': message.id}
            save_json(FILE, data)
            # Remove only the old panel messages tracked by this feature.
            for kind, old in list(data.get('panels', {}).items()):
                old_channel = interaction.guild.get_channel(old['channel_id'])
                if old_channel is None:
                    continue
                try:
                    old_message = await old_channel.fetch_message(old['message_id'])
                    if old_message.id != message.id:
                        await old_message.delete()
                    del data['panels'][kind]
                except discord.NotFound:
                    del data['panels'][kind]
                except discord.HTTPException:
                    logger.exception('기존 모집 패널 정리 실패: %s', old['message_id'])
            save_json(FILE, data)
            try:
                await message.pin(reason='통합 모집 안내 패널')
            except discord.HTTPException:
                logger.warning('모집 패널 고정 실패: %s', message.id)
        pending = bool(data.get('panels'))
        await respond(interaction, f'✅ <#{PANEL_CHANNEL_ID}>에 통합 패널을 설치했어요.' + ('\n기존 패널 일부를 정리하지 못했어요. 권한 확인 후 명령을 다시 실행해주세요. 이전 버튼은 사용할 수 없습니다.' if pending else ''))

    @tasks.loop(minutes=1)
    async def refresh(self):
        guild = self.bot.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return
        try:
            async with self.lock:
                data = load_json(FILE)
                audit = time.monotonic() - self._last_audit >= 300
                if audit:
                    self._last_audit = time.monotonic()
                for message_id, row in list(data.get('posts', {}).items()):
                    try:
                        await self.refresh_post(guild, message_id, row, audit=audit)
                    except discord.NotFound:
                        del data['posts'][message_id]
                    except discord.HTTPException:
                        logger.exception('모집글 갱신 실패: %s', message_id)
                save_json(FILE, data)
        except Exception:
            logger.exception('모집글 자동 갱신 실패')

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction, error):
        logger.error('모집 패널 설치 실패', exc_info=error)
        await respond(interaction, '관리자 권한과 봇의 채널 관리·메시지 보내기·임베드 링크·기록 보기 권한을 확인해주세요.')


async def setup(bot):
    await bot.add_cog(Recruitment(bot))
