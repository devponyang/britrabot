import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import storage
import verification_state as state
import verify


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "codes.json"
        self.override = patch.object(state, "CODE_FILE", self.path)
        self.override.start()
        self.addCleanup(self.override.stop)

    def test_guilds_are_independent(self):
        state.save_pending_code(1, 10, "one")
        state.save_pending_code(1, 20, "two")
        state.mark_verified(1, 10, 99, "profile")
        self.assertTrue(state.get_pending_code(1, 10)["verified"])
        self.assertFalse(state.get_pending_code(1, 20)["verified"])
        self.assertEqual(state.get_pending_code(1, 20)["code"], "two")

    def test_migrate_only_matching_guild_and_recheck_old_success(self):
        storage.save_json(self.path, {"1": {"guild_id": 10, "code": "old", "verified": True}})
        self.assertIsNone(state.get_pending_code(1, 20))
        record = state.get_pending_code(1, 10)
        self.assertTrue(record["legacy_verified"])
        self.assertFalse(record["verified"])
        self.assertEqual(storage.load_json(self.path)["10:1"]["code"], "old")

    def test_cooldown_resets_after_ten_minutes(self):
        state.save_pending_code(1, 10, "code")
        with patch.object(state.time, "time", return_value=1000):
            for _ in range(3):
                state.record_verification_failure(1, 10)
            self.assertEqual(state.get_cooldown_remaining(1, 10), 600)
        with patch.object(state.time, "time", return_value=1601):
            self.assertEqual(state.record_verification_failure(1, 10), (1, 0))

    def test_code_expiration_and_invalid_dates(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertFalse(state.code_expired({"created_at": now.isoformat()}))
        self.assertTrue(state.code_expired({"created_at": (now-datetime.timedelta(minutes=31)).isoformat()}))
        self.assertTrue(state.code_expired({}))

    def test_code_reissue_does_not_bypass_failures(self):
        state.save_pending_code(1, 10, "one")
        state.record_verification_failure(1, 10)
        state.save_pending_code(1, 10, "two")
        self.assertEqual(state.get_pending_code(1, 10)["attempts"], 1)

    def test_atomic_replace_failure_keeps_old_file(self):
        storage.save_json(self.path, {"old": True})
        with patch.object(storage.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                storage.save_json(self.path, {"new": True})
        self.assertEqual(storage.load_json(self.path), {"old": True})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupted_file_fails_closed(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertLogs("storage", level="ERROR"), self.assertRaises(ValueError):
            state.save_pending_code(1, 10, "code")
        self.assertEqual(self.path.read_text(), "{broken")


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "codes.json"
        self.config = {"role_id": 99, "article_url": verify.DEFAULT_ARTICLE_URL, "target_server": "브리트라"}
        self.role = Mock(id=99, mention="role")
        self.role.is_assignable.return_value = True
        self.member = Mock(id=1, roles=[], add_roles=AsyncMock(), edit=AsyncMock())
        self.guild = Mock(id=10)
        self.guild.get_role.return_value = self.role
        self.guild.me.guild_permissions.manage_roles = True
        self.interaction = SimpleNamespace(guild=self.guild, user=self.member,
            edit_original_response=AsyncMock(),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        self.comment = AsyncMock(return_value={"nickname": "test", "profile_url": "https://aion2.plaync.com/profile"})
        self.character = AsyncMock(return_value={"nickname": "test", "server": "브리트라", "power_level": 450, "class_name": "검성"})
        for override in (patch.object(state, "CODE_FILE", self.path),
                         patch.object(verify, "get_guild_config", side_effect=lambda _: dict(self.config)),
                         patch.object(verify, "log_verification", new=AsyncMock()),
                         patch.object(verify.aion2_scraper, "find_comment_by_code", new=self.comment),
                         patch.object(verify.aion2_scraper, "get_character_info", new=self.character)):
            override.start()
            self.addCleanup(override.stop)
        verify.IN_FLIGHT.clear()
        state.save_pending_code(1, 10, "TestCode123")

    async def check(self):
        view = verify.VerifyCodeView()
        await view.check_button.callback(self.interaction)

    def record(self):
        return state.get_pending_code(1, 10)

    async def test_button_disabled_during_lookup_and_stays_disabled_on_success(self):
        view = verify.VerifyCodeView()
        async def lookup(*args):
            rendered = self.interaction.response.edit_message.await_args.kwargs["view"]
            self.assertTrue(rendered.check_button.disabled)
            self.assertFalse(view.check_button.disabled)
            return {"nickname": "test", "profile_url": "https://aion2.plaync.com/profile"}
        self.comment.side_effect = lookup
        await view.check_button.callback(self.interaction)
        final_view = self.interaction.edit_original_response.await_args.kwargs["view"]
        self.assertTrue(final_view.check_button.disabled)
        self.assertEqual(final_view.check_button.label, "인증 완료")

    async def test_failure_reenables_button(self):
        self.comment.return_value = None
        await self.check()
        self.assertFalse(self.interaction.edit_original_response.await_args.kwargs["view"].check_button.disabled)

    async def test_exception_reenables_button(self):
        with patch.object(verify, "get_pending_code", side_effect=ValueError("broken state")):
            with self.assertLogs("verify", level="ERROR"):
                await self.check()
        self.assertFalse(self.interaction.edit_original_response.await_args.kwargs["view"].check_button.disabled)

    async def test_timeout_reenables_button_and_releases_lock(self):
        async def lookup(*args):
            await asyncio.Future()
        self.comment.side_effect = lookup
        with patch.object(verify, "VERIFY_TIMEOUT_SECONDS", 0.02), self.assertLogs("verify", level="ERROR"):
            await self.check()
        self.assertFalse(self.interaction.edit_original_response.await_args.kwargs["view"].check_button.disabled)
        self.assertEqual(verify.IN_FLIGHT, set())
        self.assertFalse(self.record()["verified"])

    async def test_progress_is_visible_before_lookup_and_replaced_by_success(self):
        async def lookup(*args):
            calls = self.interaction.edit_original_response.await_args_list
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].kwargs["embed"].title, "⏳ 인증을 진행 중입니다")
            return {"nickname": "test", "profile_url": "https://aion2.plaync.com/profile"}
        self.comment.side_effect = lookup
        await self.check()
        edits = [call for call in self.interaction.edit_original_response.await_args_list if "embed" in call.kwargs]
        self.assertEqual(len(edits), 2)
        self.assertEqual(edits[-1].kwargs["embed"].title, "✅ 인증 완료")
        self.interaction.followup.send.assert_not_awaited()

    async def test_success_sends_private_alarm_panel_after_completion(self):
        async def send(**kwargs):
            self.assertTrue(self.record()["verified"])
            self.assertEqual([call for call in self.interaction.edit_original_response.await_args_list if "embed" in call.kwargs][-1].kwargs["embed"].title, "✅ 인증 완료")
            self.assertTrue(kwargs["ephemeral"])
            self.assertEqual(kwargs["embed"].title, "🔔 알람 설정")
            self.assertIn("아티쟁 전략 공유", kwargs["embed"].description)
            self.assertEqual([button.label for button in kwargs["view"].children], ["필드보스", "시공", "어비스"])
        self.interaction.followup.send.side_effect = send
        with patch.object(verify, "ALARM_ROLE_GUILD_ID", self.guild.id):
            await self.check()
        self.interaction.followup.send.assert_awaited_once()

    async def test_failed_verification_does_not_offer_alarm_roles(self):
        self.character.return_value["power_level"] = 449
        with patch.object(verify, "ALARM_ROLE_GUILD_ID", self.guild.id):
            await self.check()
        self.interaction.followup.send.assert_not_awaited()

    async def test_alarm_delivery_failure_keeps_success_embed(self):
        self.interaction.followup.send.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
        with patch.object(verify, "ALARM_ROLE_GUILD_ID", self.guild.id), self.assertLogs("verify", level="ERROR"):
            await self.check()
        self.assertEqual([call for call in self.interaction.edit_original_response.await_args_list if "embed" in call.kwargs][-1].kwargs["embed"].title, "✅ 인증 완료")
        self.assertTrue(self.record()["verified"])

    async def test_alarm_buttons_toggle_their_configured_roles_independently(self):
        roles = {role_id: Mock(id=role_id, mention=str(role_id)) for role_id in verify.ALARM_ROLE_IDS.values()}
        for role in roles.values():
            role.is_assignable.return_value = True
        self.guild.get_role.side_effect = roles.get
        self.member.add_roles = AsyncMock(side_effect=lambda role, **kw: self.member.roles.append(role))
        self.member.remove_roles = AsyncMock(side_effect=lambda role, **kw: self.member.roles.remove(role))
        view = verify.VerificationAlarmRoleView()
        with patch.object(verify, "ALARM_ROLE_GUILD_ID", self.guild.id):
            for button in view.children:
                await button.callback(self.interaction)
            self.assertEqual({role.id for role in self.member.roles}, set(roles))
            for button in view.children:
                await button.callback(self.interaction)
        self.assertEqual(self.member.roles, [])
        self.assertTrue(all(call.kwargs["ephemeral"] for call in self.interaction.followup.send.await_args_list))

    async def test_failed_lookup_replaces_progress_with_retry_guidance(self):
        self.comment.return_value = None
        await self.check()
        edits = [call for call in self.interaction.edit_original_response.await_args_list if "embed" in call.kwargs]
        self.assertEqual(len(edits), 2)
        self.assertIn("댓글을 찾지 못했어요", edits[-1].kwargs["embed"].description)
        self.assertNotEqual(edits[-1].kwargs["embed"].title, "⏳ 인증을 진행 중입니다")

    async def test_unexpected_error_replaces_progress(self):
        with patch.object(verify, "get_pending_code", side_effect=ValueError("broken state")):
            with self.assertLogs("verify", level="ERROR"):
                await self.check()
        self.assertIn("오류", [call for call in self.interaction.edit_original_response.await_args_list if "embed" in call.kwargs][-1].kwargs["embed"].description)

    async def test_success_commits_only_after_role_grant(self):
        async def grant(*args, **kwargs):
            self.assertFalse(self.record()["verified"])
        self.member.add_roles.side_effect = grant
        await self.check()
        self.assertTrue(self.record()["verified"])
        self.assertEqual(self.record()["role_id"], 99)

    async def test_role_failure_remains_retryable(self):
        self.member.add_roles.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
        with self.assertLogs("verify", level="ERROR"):
            await self.check()
        self.assertFalse(self.record()["verified"])
        self.assertEqual(self.record()["attempts"], 0)
        self.member.add_roles.side_effect = None
        await self.check()
        self.assertTrue(self.record()["verified"])

    async def test_missing_role_never_queries_or_verifies(self):
        self.guild.get_role.return_value = None
        await self.check()
        self.comment.assert_not_awaited()
        self.assertFalse(self.record()["verified"])

    async def test_unassignable_role_never_queries(self):
        self.role.is_assignable.return_value = False
        await self.check()
        self.comment.assert_not_awaited()

    async def test_scraper_outage_does_not_count_failure(self):
        self.comment.side_effect = TimeoutError("source unavailable")
        with self.assertLogs("verify", level="ERROR"):
            await self.check()
        self.assertEqual(self.record()["attempts"], 0)
        self.assertFalse(self.record()["verified"])

    async def test_unknown_power_is_not_a_failed_verification(self):
        self.character.return_value["power_level"] = None
        await self.check()
        self.assertEqual(self.record()["attempts"], 0)
        self.member.add_roles.assert_not_awaited()

    async def test_low_power_counts_failure(self):
        self.character.return_value["power_level"] = 449
        await self.check()
        self.assertEqual(self.record()["attempts"], 1)
        self.member.add_roles.assert_not_awaited()

    async def test_wrong_server_counts_failure(self):
        self.character.return_value["server"] = "다른서버"
        await self.check()
        self.assertEqual(self.record()["attempts"], 1)
        self.member.add_roles.assert_not_awaited()

    async def test_expired_code_never_queries(self):
        data = storage.load_json(self.path)
        data["10:1"]["created_at"] = "2000-01-01T00:00:00+00:00"
        storage.save_json(self.path, data)
        await self.check()
        self.comment.assert_not_awaited()

    async def test_nickname_failure_does_not_undo_role_success(self):
        self.member.edit.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
        await self.check()
        self.assertTrue(self.record()["verified"])

    async def test_settings_change_during_lookup_prevents_grant(self):
        async def lookup(*args):
            self.config["target_server"] = "changed"
            return {"nickname": "test", "server": "브리트라", "power_level": 450}
        self.character.side_effect = lookup
        await self.check()
        self.member.add_roles.assert_not_awaited()

    async def test_duplicate_clicks_launch_only_one_lookup(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def lookup(*args):
            started.set()
            await release.wait()
            return {"nickname": "test", "profile_url": "https://aion2.plaync.com/profile"}
        self.comment.side_effect = lookup
        first = asyncio.create_task(self.check())
        await started.wait()
        await self.check()
        self.assertEqual(self.comment.await_count, 1)
        self.interaction.response.edit_message.assert_awaited_once()
        release.set()
        await first
        self.assertEqual(verify.IN_FLIGHT, set())

    async def test_already_verified_with_role_does_not_query(self):
        state.mark_verified(1, 10, 99, "profile")
        self.member.roles = [self.role]
        await self.check()
        self.comment.assert_not_awaited()

    async def test_verified_but_missing_role_can_recover(self):
        state.mark_verified(1, 10, 99, "profile")
        await self.check()
        self.member.add_roles.assert_awaited_once()

    async def test_cancelled_request_releases_user_lock(self):
        started = asyncio.Event()
        async def lookup(*args):
            started.set()
            await asyncio.Future()
        self.comment.side_effect = lookup
        task = asyncio.create_task(self.check())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(verify.IN_FLIGHT, set())

    async def test_queued_code_expiring_during_lookup_cannot_grant_role(self):
        with patch.object(verify, "code_expired", side_effect=[False, True]):
            await self.check()
        self.member.add_roles.assert_not_awaited()


class ConfigurationTests(unittest.TestCase):
    def test_only_obsolete_default_is_redirected_to_current_default(self):
        with patch.object(verify, "_load", return_value={"1": {"article_url": verify.LEGACY_ARTICLE_URL},
                                                        "2": {"article_url": "https://aion2.plaync.com/custom"}}):
            self.assertEqual(verify.get_guild_config(1)["article_url"], verify.DEFAULT_ARTICLE_URL)
            self.assertEqual(verify.get_guild_config(2)["article_url"], "https://aion2.plaync.com/custom")


if __name__ == "__main__":
    unittest.main()
