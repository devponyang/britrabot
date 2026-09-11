import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands
from bot import ServerBot, ServerCommandTree
from discord_helpers import SafeView, SafeModal
from cogs.automation import AdminPanelView, VerificationAdminView
from server_scope import ALLOWED_GUILD_ID


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = ServerBot(command_prefix="!", intents=discord.Intents.none(), tree_cls=ServerCommandTree)
        self.allowed = Mock(id=ALLOWED_GUILD_ID, leave=AsyncMock())
        self.other = Mock(id=123, leave=AsyncMock())
        self.bot._connection._guilds = {ALLOWED_GUILD_ID: self.allowed, 123: self.other}

    async def test_startup_leaves_only_other_guild_and_filters_background_jobs(self):
        self.assertEqual(self.bot.guilds, [self.allowed])
        await self.bot.enforce_server_scope()
        self.allowed.leave.assert_not_awaited()
        self.other.leave.assert_awaited_once()

    async def test_failed_leave_keeps_other_guild_blocked(self):
        self.other.leave.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
        with self.assertLogs("bot", level="ERROR"):
            await self.bot.enforce_server_scope()
        self.assertEqual(self.bot.guilds, [self.allowed])

    async def test_commands_and_all_panel_checks_block_other_guild_and_dm(self):
        for guild in (self.other, None, self.allowed):
            interaction = SimpleNamespace(guild=guild, guild_id=guild.id if guild else None,
                                          user=Mock(guild_permissions=Mock(administrator=True)))
            expected = guild is self.allowed
            self.assertEqual(await self.bot.tree.interaction_check(interaction), expected)
            for view in (SafeView(), AdminPanelView(), VerificationAdminView(), SafeModal(title="Test")):
                self.assertEqual(await view.interaction_check(interaction), expected)

    def test_member_voice_and_message_events_are_scoped(self):
        with patch.object(commands.Bot, "dispatch") as dispatch:
            for event in ("member_join", "member_remove", "voice_state_update", "message", "message_delete"):
                self.bot.dispatch(event, SimpleNamespace(guild=self.other))
                dispatch.assert_not_called()
            self.bot.dispatch("member_join", SimpleNamespace(guild=self.allowed))
            dispatch.assert_called_once()

    async def test_sync_registers_only_allowed_guild_and_removes_global_commands(self):
        with patch("bot.load_extensions", AsyncMock()), patch.object(self.bot.tree, "copy_global_to") as copy, \
             patch.object(self.bot.tree, "clear_commands") as clear, patch.object(self.bot.tree, "sync", AsyncMock(return_value=[])) as sync:
            await self.bot.setup_hook()
        self.assertEqual(copy.call_args.kwargs["guild"].id, ALLOWED_GUILD_ID)
        self.assertEqual(sync.await_args_list[0].kwargs["guild"].id, ALLOWED_GUILD_ID)
        clear.assert_called_once_with(guild=None)
        self.assertEqual(sync.await_args_list[1].kwargs, {})
