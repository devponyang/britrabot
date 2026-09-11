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
        self.assertEqual(r.post_embed(saved).fields[0].value, '모집 중')

    def test_past_party_is_closed(self):
        row = self.row()
        row['starts'] = '2020-01-01T00:00:00+09:00'
        self.assertTrue(r.is_closed(row))

    def test_new_views_are_persistent(self):
        cog = r.Recruitment(self.bot)
        for kind in r.LABELS:
            self.assertTrue(r.PanelView(cog, kind).is_persistent())
            self.assertTrue(r.PostView(cog, kind).is_persistent())

    def test_coupon_embed_stays_within_discord_limits(self):
        data = {'coupons': {str(i) + 'A' * 62: c.parse_expiry(self.future).isoformat() for i in range(25)}}
        embed = c.coupon_embed(data)
        self.assertLessEqual(len(embed), 6000)
        self.assertEqual(len(embed.fields), 25)
