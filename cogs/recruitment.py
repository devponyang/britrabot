import asyncio
import datetime as dt
import logging
import time
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from discord_helpers import SafeView, respond
from server_scope import allowed_guild, ALLOWED_GUILD_ID
from storage import BASE_DIR, load_json, save_json
from cogs.coupons import KST

FILE = BASE_DIR / 'recruitment.json'
logger = logging.getLogger(__name__)
LABELS = {'party': '파티 모집', 'legion': '레기온 홍보'}
PANEL_CHANNEL_ID = 1548041064961810482
POST_CHANNEL_IDS = {'party': 1547063601611804803, 'legion': 1547063786320568350}


def parse_party_start(date_text, time_text):
    date_text, time_text = date_text.strip(), time_text.strip()
    if re.fullmatch(r'[0-9]{2,4}', date_text):
        split = 1 if len(date_text) == 2 else len(date_text) - 2
        month, day = int(date_text[:split]), int(date_text[split:])
    elif re.fullmatch(r'[0-9]{1,2}[/.-][0-9]{1,2}', date_text):
        month, day = map(int, re.split(r'[/.-]', date_text))
    else:
        raise ValueError('날짜 형식 오류')
    if re.fullmatch(r'[0-9]{3,4}', time_text):
        hour, minute = int(time_text[:-2]), int(time_text[-2:])
    elif re.fullmatch(r'[0-9]{1,2}:[0-9]{2}', time_text):
        hour, minute = map(int, time_text.split(':'))
    else:
        raise ValueError('시간 형식 오류')
    return dt.datetime(2026, month, day, hour, minute, tzinfo=KST)


def is_closed(row):
    return row.get('closed', False) or (row['kind'] == 'party' and dt.datetime.fromisoformat(row['starts']) <= dt.datetime.now(KST))


def post_embed(row):
    closed = is_closed(row)
    full = row['kind'] == 'party' and len(row['members']) >= row['size']
    status = '🔴 모집 종료' if closed else '🟠 정원 마감' if full else '🟢 모집 중'
    embed = discord.Embed(title=f"{status} | {row['title']}", description=row['details'], color=discord.Color.greyple() if closed else discord.Color.orange() if full else discord.Color.green())
    embed.add_field(name='모집 상태', value=status)
    embed.add_field(name='작성자 / 문의', value=f"<@{row['owner']}>")
    if row['kind'] == 'party':
        timestamp = int(dt.datetime.fromisoformat(row['starts']).timestamp())
        embed.add_field(name='출발 시각', value=f'<t:{timestamp}:f> (<t:{timestamp}:R>)', inline=False)
        embed.add_field(name=f"참여 인원 {len(row['members'])}/{row['size']} (모집자 포함)", value=' '.join(f'<@{uid}>' for uid in row['members']), inline=False)
    embed.set_footer(text='수정·종료·삭제는 관리 채널의 내 모집글 관리에서 이용해주세요.')
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
            previous = dt.datetime.fromisoformat(row['starts']).astimezone(KST) if row else None
            self.start_date = discord.ui.TextInput(label='출발 날짜 (2026년 고정)', placeholder='0912 또는 912 → 9월 12일 / 9/2도 가능', default=previous.strftime('%m%d') if previous else None, max_length=5)
            self.start_time = discord.ui.TextInput(label='출발 시간 (한국 시간 · 24시간제)', placeholder='16:00 또는 1600 / 09:00 또는 900', default=previous.strftime('%H:%M') if previous else None, max_length=5)
            self.add_item(self.capacity)
            self.add_item(self.start_date)
            self.add_item(self.start_time)

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
                starts = parse_party_start(self.start_date.value, self.start_time.value)
                if not 2 <= size <= 24 or starts <= dt.datetime.now(KST):
                    raise ValueError()
                changes.update(size=size, starts=starts.isoformat())
            except ValueError:
                return await respond(interaction, '정원은 2~24명입니다. 날짜는 0912·912 또는 9/12, 시간은 16:00·1600 형식으로 입력해주세요. 2026년의 실제 존재하는 날짜와 현재 이후 시각만 가능합니다.')
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
        self.cog = cog
        for kind in LABELS:
            button = discord.ui.Button(label=LABELS[kind] + ' 작성', style=discord.ButtonStyle.primary, custom_id=f'recruit:panel:{kind}')
            async def clicked(interaction, kind=kind):
                panel = load_json(FILE).get('panel')
                if interaction.channel_id != PANEL_CHANNEL_ID or not panel or panel['message_id'] != interaction.message.id:
                    return await respond(interaction, f'통합 패널 <#{PANEL_CHANNEL_ID}>을 이용해주세요.')
                await interaction.response.send_modal(RecruitModal(cog, kind))
            button.callback = clicked
            self.add_item(button)

    @discord.ui.button(label='내 모집글 관리', style=discord.ButtonStyle.secondary, custom_id='recruit:manage')
    async def manage(self, interaction, button):
        if interaction.channel_id != PANEL_CHANNEL_ID:
            return await respond(interaction, f'<#{PANEL_CHANNEL_ID}>에서 이용해주세요.')
        posts = [(mid, row) for mid, row in load_json(FILE).get('posts', {}).items() if row['owner'] == interaction.user.id]
        if not posts:
            return await respond(interaction, '작성한 모집글이 없어요.')
        await interaction.response.send_message('관리할 글을 선택해주세요. 본인에게만 보입니다.', view=ManageSelectView(self.cog, interaction.user.id, posts), ephemeral=True)


class PostView(SafeView):
    def __init__(self, cog, kind):
        super().__init__(timeout=None)
        actions = [('join', '참가'), ('leave', '참가 취소')] if kind == 'party' else [('inquiry', '문의하기')]
        for action, label in actions:
            button = discord.ui.Button(label=label, custom_id=f'recruit:{kind}:{action}', style=discord.ButtonStyle.secondary)
            async def clicked(interaction, action=action):
                await cog.inquiry(interaction) if action == 'inquiry' else await cog.action(interaction, action)
            button.callback = clicked
            self.add_item(button)


class ManageSelectView(SafeView):
    def __init__(self, cog, owner, posts, page=0):
        super().__init__(timeout=300)
        self.owner = owner
        selected = posts[page*25:(page+1)*25]
        select = discord.ui.Select(placeholder='관리할 내 모집글', options=[discord.SelectOption(label=f"{LABELS[row['kind']]} · {row['title']}"[:100], value=mid) for mid, row in selected])
        async def choose(interaction):
            mid = select.values[0]
            row = load_json(FILE).get('posts', {}).get(mid)
            if not row or row['owner'] != owner:
                return await respond(interaction, '이미 삭제되었거나 관리할 수 없는 글이에요.')
            await interaction.response.edit_message(content=None, embed=post_embed(row), view=ManagePostView(cog, owner, mid))
        select.callback = choose
        self.add_item(select)
        for offset, label in ((-1, '이전'), (1, '다음')):
            if 0 <= page + offset < (len(posts)+24)//25:
                button = discord.ui.Button(label=label)
                async def flip(interaction, offset=offset):
                    await interaction.response.edit_message(view=ManageSelectView(cog, owner, posts, page+offset))
                button.callback = flip
                self.add_item(button)

    async def interaction_check(self, interaction):
        return await super().interaction_check(interaction) and interaction.user.id == self.owner and interaction.channel_id == PANEL_CHANNEL_ID


class ManagePostView(SafeView):
    def __init__(self, cog, owner, mid):
        super().__init__(timeout=300)
        self.owner = owner
        for action, label in [('edit', '수정'), ('close', '모집 종료'), ('reopen', '모집 재개'), ('delete', '글 삭제')]:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.danger if action == 'delete' else discord.ButtonStyle.secondary)
            async def clicked(interaction, action=action):
                await cog.action(interaction, action, message_id=mid)
            button.callback = clicked
            self.add_item(button)

    async def interaction_check(self, interaction):
        return await super().interaction_check(interaction) and interaction.user.id == self.owner and interaction.channel_id == PANEL_CHANNEL_ID


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

    async def action(self, interaction, action, message_id=None):
        message_id = message_id or interaction.message.id
        if action == 'edit':
            row = load_json(FILE).get('posts', {}).get(str(message_id))
            if not row or not self.can_manage(interaction, row):
                return await respond(interaction, '작성자와 관리자만 수정할 수 있어요.')
            return await interaction.response.send_modal(RecruitModal(self, row['kind'], row, message_id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            row = data.get('posts', {}).get(str(message_id))
            if not row:
                return await respond(interaction, '등록된 모집글이 아니에요.')
            uid = interaction.user.id
            if action == 'delete':
                if uid != row['owner']:
                    return await respond(interaction, '작성자만 삭제할 수 있어요.')
                channel = interaction.guild.get_channel(row['channel_id'])
                if channel is None:
                    return await respond(interaction, '게시 채널을 찾을 수 없어요.')
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.delete()
                except discord.NotFound:
                    pass
                del data['posts'][str(message_id)]
                save_json(FILE, data)
                return await respond(interaction, '✅ 모집글을 삭제했어요.')
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
            await self.refresh_post(interaction.guild, message_id, row)
            save_json(FILE, data)
        await respond(interaction, '✅ 모집글을 갱신했어요.')

    async def inquiry(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            row = data.get('posts', {}).get(str(interaction.message.id))
            if not row or row['kind'] != 'legion' or is_closed(row):
                return await respond(interaction, '현재 문의를 받는 레기온 홍보글이 아니에요.')
            if row['owner'] == interaction.user.id:
                return await respond(interaction, '본인 홍보글에는 문의할 수 없어요.')
            guild = interaction.guild
            owner = guild.get_member(row['owner'])
            if owner is None:
                try:
                    owner = await guild.fetch_member(row['owner'])
                except discord.NotFound:
                    return await respond(interaction, '작성자가 서버에 없어 문의할 수 없어요.')
            topic = f"legion-inquiry:{row['owner']}:{interaction.user.id}"
            channel = next((ch for ch in guild.text_channels if ch.topic == topic), None)
            if channel is None and topic in data.get('inquiries', {}):
                try:
                    channel = await guild.fetch_channel(data['inquiries'][topic])
                except discord.NotFound:
                    pass
            if channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                    interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
                }
                channel = await guild.create_text_channel(f'레기온-문의-{interaction.user.id}', topic=topic, overwrites=overwrites, reason='레기온 작성자와 문의자 전용 대화')
            data.setdefault('inquiries', {})[topic] = channel.id
            save_json(FILE, data)
            greetings = data.setdefault('inquiry_greetings', {})
            if str(channel.id) not in greetings:
                embed = discord.Embed(
                    title=f"💬 {row['title']} · 레기온 가입 문의",
                    description=(
                        f"**문의자:** <@{interaction.user.id}>\n"
                        f"**레기온 홍보 작성자:** <@{owner.id}>\n\n"
                        "문의자님은 캐릭터 이름과 궁금한 내용을 남겨주세요.\n"
                        "작성자님은 내용을 확인하고 이 채널에서 답변해주세요."
                    ),
                    url=f'https://discord.com/channels/{guild.id}/{row["channel_id"]}/{interaction.message.id}',
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text='작성자와 문의자 전용 대화 · 서버 관리자도 열람할 수 있습니다.')
                try:
                    greeting = await channel.send(
                        content=f'<@{owner.id}> <@{interaction.user.id}>',
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[owner, interaction.user]),
                    )
                except discord.HTTPException:
                    logger.exception('레기온 문의 안내 전송 실패: %s', channel.id)
                    return await respond(interaction, f'문의 채널은 준비됐지만 멘션 안내를 보내지 못했어요: {channel.mention}\n봇의 메시지 보내기·임베드 링크 권한을 확인한 뒤 문의하기를 다시 눌러주세요.')
                greetings[str(channel.id)] = greeting.id
                save_json(FILE, data)
            await respond(interaction, f'✅ 문의 채널: {channel.mention}\n작성자와 본인이 대화할 수 있어요. 서버 관리자도 열람할 수 있습니다.')

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
                '작성한 글은 해당 채널에 게시됩니다. 파티 참가는 게시된 글에서, 수정·종료·삭제는 아래 내 모집글 관리에서 이용해주세요.'), color=discord.Color.blurple())
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
