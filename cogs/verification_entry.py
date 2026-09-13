import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from discord_helpers import SafeView, respond
from storage import BASE_DIR, load_json, save_json
import verify

GUIDE_CHANNEL = 1548597564918464613
VERIFY_CHANNEL = 1548597197006831646
ENTRY_ROLE = 1547033362798084116
FILE = BASE_DIR / 'verification_panels.json'
PENDING = set()


class EntryView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='확인', style=discord.ButtonStyle.success, custom_id='verify:guide_confirm')
    async def confirm(self, interaction, button):
        if interaction.channel_id != GUIDE_CHANNEL:
            return await respond(interaction, f'<#{GUIDE_CHANNEL}>의 안내를 확인해주세요.')
        key = (interaction.guild.id, interaction.user.id)
        if key in PENDING:
            return await respond(interaction, '입장 권한을 부여 중입니다. 잠시 기다려주세요.')
        PENDING.add(key)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            role = interaction.guild.get_role(ENTRY_ROLE)
            if verify.get_guild_config(interaction.guild.id).get('role_id') == ENTRY_ROLE:
                return await respond(interaction, '안내 확인 역할과 최종 인증 역할이 같게 설정되어 있어요. 관리자에게 역할 분리를 요청해주세요.')
            problem = verify.role_error(interaction.guild, role)
            if problem:
                return await respond(interaction, problem)
            if not any(item.id == ENTRY_ROLE for item in interaction.user.roles):
                await interaction.user.add_roles(role, reason='인증 방법 안내 확인 (인증 완료 아님)')
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label='인증 채널로 이동', url=f'https://discord.com/channels/{interaction.guild.id}/{VERIFY_CHANNEL}'))
            await interaction.followup.send('✅ 안내를 확인했어요. 아래 버튼을 눌러 인증을 진행해주세요. 아직 게임 캐릭터 인증이 완료된 상태는 아닙니다.', view=view, ephemeral=True)
        finally:
            PENDING.discard(key)


class VerificationEntry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def cog_load(self):
        self.bot.add_view(EntryView())

    @app_commands.command(name='인증안내설정', description='지정된 안내 채널과 실제 인증 채널에 두 단계 인증 패널을 설치합니다.')
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def install(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            data = load_json(FILE)
            guide = discord.Embed(title='📋 인증 방법 안내', description=(
                '**인증 대상: 브리트라 서버 · 전투력 450K(450,000) 이상**\n\n'
                '1. 아래 **확인** 버튼을 눌러 인증 채널 입장 역할을 받으세요.\n'
                '2. 본인에게 표시되는 **인증 채널로 이동** 버튼을 누르세요.\n'
                '3. 인증 채널에서 **인증진행**을 눌러 코드를 발급받으세요.\n'
                '4. 공식 인증게시판에 대표 캐릭터로 코드를 댓글로 작성하세요.\n'
                '5. **댓글 작성 완료**를 한 번 누르고 결과를 기다려주세요.\n\n'
                '코드는 30분간 유효하며 인증에는 최대 3분이 걸릴 수 있습니다. '
                '인증 중에는 버튼이 잠기고 실패하면 재시도할 수 있습니다. 3회 실패 시 10분간 대기합니다.\n'
                '**확인 버튼은 안내 확인용이며, 최종 인증 역할은 캐릭터 검증 후 부여됩니다.**'), color=discord.Color.blurple())
            actual = discord.Embed(title='🛡️ 캐릭터 인증 진행', description='아래 **인증진행** 버튼으로 코드를 발급받으세요.\n브리트라 서버 · 전투력 **450K 이상**인지 확인합니다.\n댓글 작성 완료 버튼은 한 번만 누르고 결과를 기다려주세요.', color=discord.Color.blue())
            for channel_id, embed, view in ((GUIDE_CHANNEL, guide, EntryView()), (VERIFY_CHANNEL, actual, verify.VerifyPanelView())):
                channel = interaction.guild.get_channel(channel_id)
                if channel is None:
                    return await respond(interaction, f'<#{channel_id}>에 접근할 수 없어요. 채널과 봇 권한을 확인해주세요.')
                message = None
                if str(channel_id) in data:
                    try:
                        message = await channel.fetch_message(data[str(channel_id)])
                        await message.edit(content=None, embed=embed, view=view)
                    except discord.NotFound:
                        pass
                if message is None:
                    message = await channel.send(embed=embed, view=view)
                data[str(channel_id)] = message.id
                save_json(FILE, data)
        await respond(interaction, '✅ 안내·인증 패널을 설치했어요. 인증 채널은 안내 확인 역할이 볼 수 있도록 채널 권한을 설정해주세요.')

    async def cog_app_command_error(self, interaction, error):
        await respond(interaction, '설치하지 못했어요. 관리자 권한과 봇의 채널 보기·메시지 보내기·기록 보기·임베드 링크 권한을 확인해주세요.')


async def setup(bot):
    await bot.add_cog(VerificationEntry(bot))
