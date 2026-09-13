import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import discord
from cogs import coupons as c, recruitment as r
from server_scope import ALLOWED_GUILD_ID


class CommunityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for module in (c, r):
            override = patch.object(module, 'FILE', Path(folder.name) / (module.__name__ + '.json'))
            override.start()
            self.addCleanup(override.stop)
        self.message = Mock(id=100, edit=AsyncMock())
        self.channel = Mock(id=10, fetch_message=AsyncMock(return_value=self.message), send=AsyncMock(return_value=self.message))
        self.guild = Mock(id=ALLOWED_GUILD_ID)
        self.guild.get_channel.return_value = self.channel
        self.bot = Mock(get_guild=Mock(return_value=self.guild))
        self.interaction = SimpleNamespace(guild=self.guild, guild_id=self.guild.id, channel=self.channel, channel_id=10,
            message=self.message, user=Mock(id=2, guild_permissions=Mock(administrator=False)),
            response=Mock(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=True)), followup=Mock(send=AsyncMock()))
        self.future = (dt.datetime.now(c.KST) + dt.timedelta(days=10)).strftime('%Y-%m-%d %H:%M')

    def row(self):
        return dict(kind='party', title='파티', details='소개', owner=1, members=[1], size=2, starts=c.parse_expiry(self.future).isoformat(), channel_id=10, closed=False)

    def test_date_validation_and_expired_display(self):
        self.assertEqual(c.parse_expiry('2026-09-30').hour, 23)
        self.assertEqual(c.parse_expiry('2026-09-30').second, 59)
        with self.assertRaises(ValueError):
            c.parse_expiry('2026-02-30')
        embed = c.coupon_embed({'coupons': {'HELLO': '2020-01-01T00:00:00+09:00'}})
        self.assertEqual(embed.fields[0].name, '사용 기간 종료')

    async def test_coupon_registration_updates_all_existing_messages(self):
        c.save_json(c.FILE, {'messages': [{'channel_id': 10, 'message_id': 100}, {'channel_id': 11, 'message_id': 101}]})
        cog = c.Coupons(self.bot)
        await c.Coupons.enter.callback(cog, self.interaction, 'NEWCODE', self.future)
        self.assertEqual(self.message.edit.await_count, 2)
        self.channel.send.assert_not_awaited()
        self.assertIn('NEWCODE', c.load_json(c.FILE)['coupons'])
        self.assertIsNone(self.message.edit.await_args.kwargs['content'])

    async def test_coupon_info_reuses_message_after_restart(self):
        await c.Coupons.info.callback(c.Coupons(self.bot), self.interaction)
        await c.Coupons.info.callback(c.Coupons(self.bot), self.interaction)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(c.load_json(c.FILE)['messages']), 1)

    async def test_same_coupon_updates_expiry_without_duplicate(self):
        cog = c.Coupons(self.bot)
        await c.Coupons.enter.callback(cog, self.interaction, 'CODE', self.future)
        await c.Coupons.enter.callback(cog, self.interaction, 'code', self.future)
        self.assertEqual(len(c.load_json(c.FILE)['coupons']), 1)

    async def test_failed_coupon_edit_is_retried(self):
        data = {'coupons': {}, 'messages': [{'channel_id': 10, 'message_id': 100}]}
        self.message.edit.side_effect = discord.Forbidden(Mock(status=403, reason='Forbidden'), 'denied')
        cog = c.Coupons(self.bot)
        with self.assertLogs(c.logger, level='ERROR'):
            self.assertEqual(await cog.update_messages(data), 1)
        self.message.edit.side_effect = None
        self.assertEqual(await cog.update_messages(data), 0)
        self.assertEqual(self.message.edit.await_count, 2)

    async def test_concurrent_party_join_does_not_overbook(self):
        r.save_json(r.FILE, {'posts': {'100': self.row()}})
        cog = r.Recruitment(self.bot)
        second = SimpleNamespace(**vars(self.interaction))
        second.user = Mock(id=3, guild_permissions=Mock(administrator=False))
        await asyncio.gather(cog.action(self.interaction, 'join'), cog.action(second, 'join'))
        self.assertEqual(len(r.load_json(r.FILE)['posts']['100']['members']), 2)
        self.channel.send.assert_not_awaited()

    async def test_cancel_reopens_full_party_and_nonowner_cannot_close(self):
        row = self.row()
        row['members'].append(2)
        r.save_json(r.FILE, {'posts': {'100': row}})
        cog = r.Recruitment(self.bot)
        await cog.action(self.interaction, 'close')
        self.assertFalse(r.load_json(r.FILE)['posts']['100']['closed'])
        await cog.action(self.interaction, 'leave')
        saved = r.load_json(r.FILE)['posts']['100']
        self.assertEqual(saved['members'], [1])
        self.assertEqual(r.post_embed(saved).fields[0].value, '🟢 모집 중')

    def test_past_party_is_closed(self):
        row = self.row()
        row['starts'] = '2020-01-01T00:00:00+09:00'
        self.assertTrue(r.is_closed(row))

    def test_new_views_are_persistent(self):
        cog = r.Recruitment(self.bot)
        for kind in r.LABELS:
            self.assertTrue(r.PanelView(cog).is_persistent())
            self.assertTrue(r.PostView(cog, kind).is_persistent())

    def test_coupon_embed_stays_within_discord_limits(self):
        data = {'coupons': {str(i) + 'A' * 62: c.parse_expiry(self.future).isoformat() for i in range(25)}}
        embed = c.coupon_embed(data)
        self.assertLessEqual(len(embed), 6000)
        self.assertEqual(len(embed.fields), 25)

    async def test_unified_panel_has_two_buttons_and_reuses_message(self):
        self.channel.id = r.PANEL_CHANNEL_ID
        self.message.pin = AsyncMock()
        cog = r.Recruitment(self.bot)
        await r.Recruitment.panel.callback(cog, self.interaction)
        await r.Recruitment.panel.callback(cog, self.interaction)
        self.channel.send.assert_awaited_once()
        view = self.channel.send.await_args.kwargs['view']
        self.assertEqual([button.label for button in view.children], ['내 모집글 관리', '파티 모집 작성', '레기온 홍보 작성'])
        self.assertEqual(r.load_json(r.FILE)['panel']['channel_id'], r.PANEL_CHANNEL_ID)

    async def test_posts_route_to_separate_channels_from_unified_panel(self):
        cog = r.Recruitment(self.bot)
        self.interaction.channel_id = r.PANEL_CHANNEL_ID
        for kind, destination_id in r.POST_CHANNEL_IDS.items():
            r.save_json(r.FILE, {'panel': {'channel_id': r.PANEL_CHANNEL_ID, 'message_id': 100}})
            destination = Mock(id=destination_id, send=AsyncMock(return_value=self.message))
            self.guild.get_channel.return_value = destination
            modal = r.RecruitModal(cog, kind)
            modal.subject = SimpleNamespace(value='모집 제목')
            modal.details = SimpleNamespace(value='모집 설명')
            if kind == 'party':
                modal.capacity = SimpleNamespace(value='6')
                modal.start_date = SimpleNamespace(value='1231')
                modal.start_time = SimpleNamespace(value='2359')
            await modal.on_submit(self.interaction)
            self.guild.get_channel.assert_called_with(destination_id)
            destination.send.assert_awaited_once()
            self.assertEqual(r.load_json(r.FILE)['posts']['100']['channel_id'], destination_id)
        self.channel.send.assert_not_awaited()

    async def test_old_panel_button_cannot_open_modal(self):
        r.save_json(r.FILE, {'panel': {'channel_id': r.PANEL_CHANNEL_ID, 'message_id': 200}})
        self.interaction.response.send_modal = AsyncMock()
        self.interaction.channel_id = r.PANEL_CHANNEL_ID
        await r.PanelView(r.Recruitment(self.bot)).children[0].callback(self.interaction)
        self.interaction.response.send_modal.assert_not_awaited()

    async def test_deleted_post_removed_even_when_content_unchanged(self):
        row = self.row()
        row['signature'] = str(r.post_embed(row).to_dict())
        r.save_json(r.FILE, {'posts': {'100': row}})
        self.channel.fetch_message.side_effect = discord.NotFound(Mock(status=404, reason='Not Found'), 'gone')
        cog = r.Recruitment(self.bot)
        await cog.refresh()
        self.assertEqual(r.load_json(r.FILE)['posts'], {})

    async def test_audit_reads_but_does_not_edit_unchanged_post(self):
        row = self.row()
        row['signature'] = str(r.post_embed(row).to_dict())
        cog = r.Recruitment(self.bot)
        await cog.refresh_post(self.guild, 100, row, audit=True)
        self.channel.fetch_message.assert_awaited_once()
        self.message.edit.assert_not_awaited()

    async def test_coupon_delete_updates_existing_embed(self):
        c.save_json(c.FILE, {'coupons': {'CODE': c.parse_expiry(self.future).isoformat()}, 'messages': [{'channel_id': 10, 'message_id': 100}]})
        await c.Coupons.remove.callback(c.Coupons(self.bot), self.interaction, 'code')
        self.assertEqual(c.load_json(c.FILE)['coupons'], {})
        self.message.edit.assert_awaited_once()
        self.assertIn('등록된 쿠폰이 없습니다', self.message.edit.await_args.kwargs['embed'].description)

    async def test_role_double_click_is_ignored_and_lock_released(self):
        from discord_helpers import toggle_role, ROLE_UPDATES_IN_FLIGHT
        started, release = asyncio.Event(), asyncio.Event()
        role = Mock(id=123, mention='<@&123>', is_assignable=Mock(return_value=True))
        self.guild.get_role.return_value = role
        self.interaction.user.roles = []
        async def add(*args, **kwargs):
            started.set()
            await release.wait()
        self.interaction.user.add_roles = AsyncMock(side_effect=add)
        self.interaction.user.remove_roles = AsyncMock()
        first = asyncio.create_task(toggle_role(self.interaction, self.guild.id, 123))
        await started.wait()
        await toggle_role(self.interaction, self.guild.id, 123)
        release.set()
        await first
        self.interaction.user.add_roles.assert_awaited_once()
        self.assertFalse(ROLE_UPDATES_IN_FLIGHT)

    async def test_role_failure_releases_lock(self):
        from discord_helpers import toggle_role, ROLE_UPDATES_IN_FLIGHT
        self.guild.get_role.return_value = Mock(id=123, is_assignable=Mock(return_value=True))
        self.interaction.user.roles = []
        self.interaction.user.add_roles = AsyncMock(side_effect=RuntimeError('failed'))
        with self.assertRaises(RuntimeError):
            await toggle_role(self.interaction, self.guild.id, 123)
        self.assertFalse(ROLE_UPDATES_IN_FLIGHT)

    async def test_coupon_info_does_not_claim_success_on_failed_edit(self):
        c.save_json(c.FILE, {'messages': [{'channel_id': 10, 'message_id': 100}]})
        self.message.edit.side_effect = discord.Forbidden(Mock(status=403, reason='Forbidden'), 'denied')
        with self.assertLogs(c.logger, level='ERROR'):
            await c.Coupons.info.callback(c.Coupons(self.bot), self.interaction)
        self.assertIn('갱신하지 못했어요', self.interaction.followup.send.await_args.args[0])
        self.channel.send.assert_not_awaited()

    async def test_management_list_is_private_and_only_contains_own_posts(self):
        own = {**self.row(), 'owner': 2}
        r.save_json(r.FILE, {'posts': {'100': own, '200': self.row()}})
        self.interaction.channel_id = r.PANEL_CHANNEL_ID
        await r.PanelView(r.Recruitment(self.bot)).manage.callback(self.interaction)
        kwargs = self.interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertEqual([option.value for option in kwargs['view'].children[0].options], ['100'])
        self.interaction.user.id = 9
        self.assertFalse(await kwargs['view'].interaction_check(self.interaction))

    def test_public_posts_only_have_participation_or_inquiry(self):
        cog = r.Recruitment(self.bot)
        self.assertEqual([b.label for b in r.PostView(cog, 'party').children], ['참가', '참가 취소'])
        self.assertEqual([b.label for b in r.PostView(cog, 'legion').children], ['문의하기'])

    async def test_legion_inquiry_private_permissions_and_reuse(self):
        r.save_json(r.FILE, {'posts': {'100': {**self.row(), 'kind': 'legion'}}})
        self.guild.text_channels = []
        owner = Mock(id=1)
        self.guild.get_member.return_value = owner
        channel = Mock(id=300, mention='<#300>', send=AsyncMock(return_value=Mock(id=301)))
        self.guild.create_text_channel = AsyncMock(return_value=channel)
        self.guild.fetch_channel = AsyncMock(return_value=channel)
        cog = r.Recruitment(self.bot)
        await cog.inquiry(self.interaction)
        await cog.inquiry(self.interaction)
        self.guild.create_text_channel.assert_awaited_once()
        channel.send.assert_awaited_once()
        notice = channel.send.await_args.kwargs
        self.assertEqual(notice['content'], '<@1> <@2>')
        self.assertEqual(notice['allowed_mentions'].users, [owner, self.interaction.user])
        self.assertFalse(notice['allowed_mentions'].everyone)
        self.assertFalse(notice['allowed_mentions'].roles)
        self.assertIn('/10/100', notice['embed'].url)
        overwrites = self.guild.create_text_channel.await_args.kwargs['overwrites']
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertTrue(overwrites[owner].view_channel)
        self.assertTrue(overwrites[self.interaction.user].view_channel)

    async def test_private_delete_targets_public_post_not_private_message(self):
        row = {**self.row(), 'owner': 2}
        r.save_json(r.FILE, {'posts': {'400': row}})
        self.message.delete = AsyncMock()
        await r.Recruitment(self.bot).action(self.interaction, 'delete', message_id='400')
        self.channel.fetch_message.assert_awaited_with(400)
        self.message.delete.assert_awaited_once()
        self.assertEqual(r.load_json(r.FILE)['posts'], {})

    def test_party_separate_date_time_formats(self):
        expected = dt.datetime(2026, 9, 12, 16, 0, tzinfo=c.KST)
        for date in ('0912', '912', '9/12'):
            for time in ('16:00', '1600'):
                self.assertEqual(r.parse_party_start(date, time), expected)
        self.assertEqual(r.parse_party_start('92', '900'), dt.datetime(2026, 9, 2, 9, 0, tzinfo=c.KST))
        for date, time in [('0229', '1600'), ('0931', '1600'), ('1301', '1600'), ('912', '2400'), ('912', '1660'), ('912', '16')]:
            with self.assertRaises(ValueError):
                r.parse_party_start(date, time)

    def test_party_modal_has_five_fields_and_edit_defaults(self):
        row = {**self.row(), 'starts': '2026-09-12T16:00:00+09:00'}
        modal = r.RecruitModal(r.Recruitment(self.bot), 'party', row, 100)
        self.assertEqual(len(modal.children), 5)
        self.assertEqual(modal.start_date.default, '0912')
        self.assertEqual(modal.start_time.default, '16:00')

    def test_party_role_allowlist_and_duplicates(self):
        role = Mock(id=500, mentionable=True)
        role.name = 'PvE'
        self.guild.roles = [role]
        self.assertEqual(r.resolve_party_roles(self.guild, ['PvE', 'PvE']), [role])
        for names in (['everyone'], ['어비스']):
            with self.assertRaises(ValueError):
                r.resolve_party_roles(self.guild, names)
        self.assertEqual(r.resolve_party_roles(self.guild, []), [])

    async def test_party_optional_role_is_pinged_only_on_initial_post(self):
        self.interaction.channel_id = r.PANEL_CHANNEL_ID
        self.channel.id = r.POST_CHANNEL_IDS['party']
        r.save_json(r.FILE, {'panel': {'channel_id': r.PANEL_CHANNEL_ID, 'message_id': 100}})
        role = Mock(id=500, mentionable=True)
        role.name = '어비스'
        self.guild.roles = [role]
        cog = r.Recruitment(self.bot)
        modal = r.RecruitModal(cog, 'party', ping_names=['어비스'])
        modal.subject = SimpleNamespace(value='파티')
        modal.details = SimpleNamespace(value='소개')
        modal.capacity = SimpleNamespace(value='6')
        modal.start_date = SimpleNamespace(value='1231')
        modal.start_time = SimpleNamespace(value='2359')
        await modal.on_submit(self.interaction)
        sent = self.channel.send.await_args.kwargs
        self.assertEqual(sent['content'], '<@&500>')
        self.assertEqual(sent['allowed_mentions'].roles, [role])
        self.assertFalse(sent['allowed_mentions'].everyone)
        row = r.load_json(r.FILE)['posts']['100']
        await cog.refresh_post(self.guild, 100, row)
        edited = self.message.edit.await_args.kwargs
        self.assertEqual(edited['content'], '<@&500>')
        self.assertFalse(edited['allowed_mentions'].roles)

    async def test_skip_role_selection_opens_unpinged_modal(self):
        self.interaction.response.send_modal = AsyncMock()
        view = r.PartyRolesView(r.Recruitment(self.bot), 2)
        view.names = ['시공']
        await view.skip.callback(self.interaction)
        self.assertEqual(self.interaction.response.send_modal.await_args.args[0].ping_names, ())

    async def test_party_voice_sharing_and_twenty_member_limit(self):
        self.interaction.channel_id = r.PANEL_CHANNEL_ID
        self.channel.id = r.POST_CHANNEL_IDS['party']
        self.interaction.user.voice = SimpleNamespace(channel=Mock(id=987))
        r.save_json(r.FILE, {'panel': {'channel_id': r.PANEL_CHANNEL_ID, 'message_id': 100}})
        cog = r.Recruitment(self.bot)
        modal = r.RecruitModal(cog, 'party', include_voice=True)
        modal.subject = SimpleNamespace(value='파티')
        modal.details = SimpleNamespace(value='소개')
        modal.capacity = SimpleNamespace(value='21')
        modal.start_date = SimpleNamespace(value='1231')
        modal.start_time = SimpleNamespace(value='2359')
        await modal.on_submit(self.interaction)
        self.channel.send.assert_not_awaited()
        modal.capacity.value = '20'
        await modal.on_submit(self.interaction)
        row = r.load_json(r.FILE)['posts']['100']
        self.assertEqual(row['size'], 20)
        self.assertEqual(row['members'], [2])
        self.assertEqual(row['voice_channel_id'], 987)
        self.assertIn('<#987>', str(self.channel.send.await_args.kwargs['embed'].to_dict()))

    async def test_voice_selection_is_optional_and_carried_into_modal(self):
        self.interaction.response.edit_message = AsyncMock()
        self.interaction.response.send_modal = AsyncMock()
        view = r.PartyRolesView(r.Recruitment(self.bot), 2)
        self.assertFalse(view.include_voice)
        await view.toggle_voice.callback(self.interaction)
        await view.skip.callback(self.interaction)
        self.assertTrue(self.interaction.response.send_modal.await_args.args[0].include_voice)
        self.assertEqual(self.interaction.response.send_modal.await_args.args[0].ping_names, ())

    def test_field_boss_in_role_choices(self):
        view = r.PartyRolesView(r.Recruitment(self.bot), 2)
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        self.assertIn('필드보스', [option.value for option in select.options])
        self.assertEqual(select.max_values, 5)

    async def test_guide_confirmation_grants_entry_role_and_private_link(self):
        from cogs import verification_entry as entry
        role = Mock(id=entry.ENTRY_ROLE, is_assignable=Mock(return_value=True))
        self.guild.get_role.return_value = role
        self.interaction.channel_id = entry.GUIDE_CHANNEL
        self.interaction.user.roles = []
        self.interaction.user.add_roles = AsyncMock()
        with patch.object(entry.verify, 'get_guild_config', return_value={'role_id': 999}):
            await entry.EntryView().confirm.callback(self.interaction)
        self.interaction.user.add_roles.assert_awaited_once_with(role, reason='인증 방법 안내 확인 (인증 완료 아님)')
        reply = self.interaction.followup.send.await_args.kwargs
        self.assertTrue(reply['ephemeral'])
        self.assertIn(str(entry.VERIFY_CHANNEL), reply['view'].children[0].url)
        self.assertFalse(entry.PENDING)

    async def test_guide_cannot_grant_final_verification_role(self):
        from cogs import verification_entry as entry
        self.interaction.channel_id = entry.GUIDE_CHANNEL
        self.interaction.user.add_roles = AsyncMock()
        with patch.object(entry.verify, 'get_guild_config', return_value={'role_id': entry.ENTRY_ROLE}):
            await entry.EntryView().confirm.callback(self.interaction)
        self.interaction.user.add_roles.assert_not_awaited()
        self.assertFalse(entry.PENDING)
