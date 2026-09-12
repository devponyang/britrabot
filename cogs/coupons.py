import asyncio
import datetime as dt
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import BASE_DIR, load_json, save_json
from server_scope import ALLOWED_GUILD_ID

logger = logging.getLogger(__name__)
FILE = BASE_DIR / 'coupons.json'
KST = dt.timezone(dt.timedelta(hours=9))


def parse_expiry(value):
    value = value.strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            result = dt.datetime.strptime(value, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        else:
            result = dt.datetime.strptime(value, '%Y-%m-%d %H:%M')
        return result.replace(tzinfo=KST)
    except ValueError:
        raise ValueError('유효기한은 2026-09-30 또는 2026-09-30 23:59 형식으로 입력해주세요. 한국 시간 기준입니다.')


def coupon_embed(data, now=None):
    now = now or dt.datetime.now(KST)
    embed = discord.Embed(title='🎁 쿠폰 정보', description='새 쿠폰이 등록되면 이 메시지가 자동 갱신됩니다.\n유효기한은 한국 시간(KST) 기준입니다.', color=discord.Color.gold())
    for code, expiry in sorted(data.get('coupons', {}).items(), key=lambda item: item[1]):
        until = dt.datetime.fromisoformat(expiry)
        state = '사용 기간 종료' if until <= now else '사용 가능'
        embed.add_field(name=state, value=f'`{code}`\n유효기한: {until:%Y-%m-%d %H:%M} (KST)', inline=False)
    if not embed.fields:
        embed.description += '\n\n등록된 쿠폰이 없습니다.'
    embed.set_footer(text='쿠폰별 사용 조건과 실제 사용 가능 여부는 게임 내 안내를 확인해주세요.')
    return embed


class Coupons(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def cog_load(self):
        self.refresh.start()

    async def cog_unload(self):
        task = self.refresh.get_task()
        self.refresh.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    async def update_messages(self, data):
        guild = self.bot.get_guild(ALLOWED_GUILD_ID)
        if not guild:
            return 0
        failed = 0
        embed = coupon_embed(data)
        signature = str(embed.to_dict())
        for ref in list(data.setdefault('messages', [])):
            if ref.get('signature') == signature:
                continue
            channel = guild.get_channel(ref['channel_id'])
            if channel is None:
                failed += 1
                continue
            try:
                message = await channel.fetch_message(ref['message_id'])
                await message.edit(content=None, embed=embed)
                ref['signature'] = signature
            except discord.NotFound:
                data['messages'].remove(ref)
            except discord.HTTPException:
                failed += 1
                logger.exception('쿠폰 정보 메시지 갱신 실패: %s', ref['message_id'])
        save_json(FILE, data)
        return failed

    @app_commands.command(name='쿠폰입력', description='쿠폰 번호와 유효기한을 등록하고 기존 정보 메시지를 갱신합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(code='쿠폰 번호 (영문·숫자·하이픈·밑줄, 최대 64자)', expires='한국 시간: 2026-09-30 또는 2026-09-30 23:59')
    async def enter(self, interaction: discord.Interaction, code: str, expires: str):
        code = code.strip()
        try:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', code):
                raise ValueError('쿠폰 번호는 영문·숫자·하이픈·밑줄로 1~64자 입력해주세요.')
            until = parse_expiry(expires)
            if until <= dt.datetime.now(KST):
                raise ValueError('유효기한은 현재보다 이후로 입력해주세요.')
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            coupons = data.setdefault('coupons', {})
            # Retain valid codes; expired entries are removed when registering a new one.
            coupons = {key: value for key, value in coupons.items() if dt.datetime.fromisoformat(value) > dt.datetime.now(KST)}
            existing = next((key for key in coupons if key.casefold() == code.casefold()), None)
            if len(coupons) >= 25 and existing is None:
                return await interaction.followup.send('사용 가능한 쿠폰은 최대 25개까지 등록할 수 있어요. 만료 후 새 쿠폰을 등록해주세요.', ephemeral=True)
            if existing:
                del coupons[existing]
            coupons[code] = until.isoformat()
            data['coupons'] = coupons
            save_json(FILE, data)
            failed = await self.update_messages(data)
        await interaction.followup.send(f'✅ `{code}` 쿠폰을 등록했어요. 유효기한: {until:%Y-%m-%d %H:%M} (KST).'
                                        + (f'\n정보 메시지 {failed}개는 갱신하지 못했어요. 채널 권한을 확인해주세요. 1분마다 재시도합니다.' if failed else '\n게시된 쿠폰 정보 메시지도 갱신했어요.'), ephemeral=True)

    @app_commands.command(name='쿠폰정보', description='이 채널에 자동 갱신되는 쿠폰 정보 임베드를 게시합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            for ref in data.get('messages', []):
                if ref['channel_id'] == interaction.channel_id:
                    ref.pop('signature', None)
            await self.update_messages(data)
            ref = next((ref for ref in data['messages'] if ref['channel_id'] == interaction.channel_id), None)
            if ref:
                if ref.get('signature') != str(coupon_embed(data).to_dict()):
                    return await interaction.followup.send('기존 쿠폰 메시지를 갱신하지 못했어요. 봇의 메시지·임베드·기록 보기 권한을 확인해주세요. 1분마다 재시도합니다.', ephemeral=True)
                return await interaction.followup.send(f"✅ 이 채널의 기존 쿠폰 정보 메시지를 사용합니다: https://discord.com/channels/{interaction.guild_id}/{ref['channel_id']}/{ref['message_id']}", ephemeral=True)
            embed = coupon_embed(data)
            message = await interaction.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            data['messages'].append({'channel_id': interaction.channel_id, 'message_id': message.id, 'signature': str(embed.to_dict())})
            save_json(FILE, data)
        await interaction.followup.send('✅ 쿠폰 정보 메시지를 게시했어요. 이후 등록 내용은 이 메시지에 반영됩니다.', ephemeral=True)

    @app_commands.command(name='쿠폰삭제', description='잘못 등록한 쿠폰을 삭제하고 기존 정보 메시지를 갱신합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(code='삭제할 쿠폰 번호')
    async def remove(self, interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            coupons = data.setdefault('coupons', {})
            existing = next((key for key in coupons if key.casefold() == code.strip().casefold()), None)
            if existing is None:
                return await interaction.followup.send('등록된 쿠폰 번호를 찾을 수 없어요.', ephemeral=True)
            del coupons[existing]
            save_json(FILE, data)
            failed = await self.update_messages(data)
        await interaction.followup.send('✅ 쿠폰을 삭제했어요.' + (' 일부 메시지는 갱신에 실패해 1분마다 재시도합니다.' if failed else ' 기존 정보 메시지도 갱신했어요.'), ephemeral=True)

    @tasks.loop(minutes=1)
    async def refresh(self):
        try:
            async with self.lock:
                await self.update_messages(load_json(FILE))
        except Exception:
            logger.exception('쿠폰 자동 갱신 실패')

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction, error):
        logger.error('쿠폰 명령 오류', exc_info=error)
        message = '관리자만 사용할 수 있어요.' if isinstance(error, app_commands.MissingPermissions) else '처리에 실패했어요. 봇의 메시지 보내기·임베드 링크·기록 보기 권한을 확인해주세요.'
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Coupons(bot))
