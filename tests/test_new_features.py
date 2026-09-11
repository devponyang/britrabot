import asyncio
import datetime
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import aion2_scraper as scraper
from cogs import automation as a
from cogs import voice_rooms as v
from discord_helpers import embed_text

# Public matchup card observed on 2026-09-10; opponent is displayed first.
CARD = "네자칸 WIN 4 1차전 ⚔️ 2 브리트라 LOSE 누적 4:2 ✅ 9월 9일 (1차전) 결과 집계완료"


class ArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = patch.object(a, "ARTIFACT_RECORDS_FILE", Path(directory.name) / "records.json")
        override.start()
        self.addCleanup(override.stop)
        self.result = {"record": scraper.parse_breitra_artifact_result(CARD),
                       "completion": "9월 9일 (1차전)", "url": scraper.ARTIFACT_RESULT_URL}

    def test_current_round_is_saved_and_selected_over_old_opponent(self):
        a.save_artifact_history({"pair": "브리트라 VS 루드라", "source_url": "https://example.org",
                                 "records": [{"date": "2026-09-05", "round": "4차전", "scores": ["3:3"]}]})
        history = scraper.artifact_history_from_result(self.result, datetime.date(2026, 9, 10))
        a.save_artifact_history(history)
        self.assertEqual(a.latest_saved_artifact_opponent(), "네자칸")
        saved = a.load_artifact_history(history["pair"])
        self.assertEqual(saved["records"][0]["scores"], ["2:4"])
        text = embed_text(a.build_artifact_history_embed("네자칸", saved))
        for expected in ("2026-09-09 22:00", "1차전", "2:4"):
            self.assertIn(expected, text)
        self.assertIsNotNone(a.load_artifact_history("브리트라 VS 루드라"))

    def test_summary_missing_still_shows_recent_match(self):
        history = scraper.artifact_history_from_result(self.result, datetime.date(2026, 9, 10))
        history.pop("record")
        self.assertIn("2:4", embed_text(a.build_artifact_history_embed("네자칸", history)))

    def test_year_rollover(self):
        self.result["completion"] = "12월 31일 (4차전)"
        self.assertEqual(scraper.artifact_result_date(self.result, datetime.date(2027, 1, 1)), datetime.date(2026, 12, 31))

    async def test_other_card_completion_cannot_complete_breitra_card(self):
        locator = Mock()
        locator.first.wait_for = AsyncMock()
        locator.all_inner_texts = AsyncMock(return_value=[
            "다른서버 WIN 5 1차전 ⚔️ 1 다른상대 LOSE 누적 5:1 ✅ 9월 9일 (1차전) 결과 집계완료",
            CARD.split(" ✅")[0],
        ])
        page = Mock(goto=AsyncMock())
        page.locator.return_value = locator
        @asynccontextmanager
        async def context():
            yield page
        with patch.object(scraper, "browser_page", context):
            self.assertIsNone(await scraper.get_latest_artifact_result())

    def test_alarm_preparation_and_delayed_wording(self):
        when = datetime.datetime(2026, 9, 9, 21, 30, tzinfo=a.KST)
        embed = a.build_alarm_embed("", "아티팩트쟁:22:00:30", alarm_time=when, now=when)
        self.assertIn("30분 전", embed.title)
        self.assertIn("10분 전에", embed.description)
        delayed = a.build_alarm_embed("", "아티팩트쟁:22:00:0", alarm_time=when.replace(hour=22, minute=0), now=when.replace(hour=22, minute=2))
        self.assertIn("지연 안내", delayed.title)
        self.assertFalse(a.should_ping_alarm("important", "아티팩트쟁:22:00:30"))
        self.assertTrue(a.should_ping_alarm("important", "아티팩트쟁:22:00:10"))
        self.assertFalse(a.should_ping_alarm("none", "아티팩트쟁:22:00:0"))

    async def test_result_routes_to_new_channel(self):
        cfg = patch.object(a, "CONFIG_FILE", Path(a.ARTIFACT_RECORDS_FILE).with_name("config.json"))
        cfg.start()
        self.addCleanup(cfg.stop)
        channel = Mock(spec=discord.TextChannel, send=AsyncMock())
        guild = Mock(id=a.OFFICIAL_NOTICE_GUILD_ID)
        guild.get_channel.return_value = channel
        cog = a.Automation(Mock(guilds=[guild]))
        with patch.object(scraper, "get_latest_artifact_result", AsyncMock(return_value=self.result)), patch.object(cog, "_sync_artifact_records", AsyncMock()):
            await cog._check_artifact_result(datetime.datetime(2026, 9, 9, 22, 30, tzinfo=a.KST))
        guild.get_channel.assert_called_with(1547489396301762640)
        channel.send.assert_awaited_once()
        self.assertIn("이번 회차", channel.send.await_args.kwargs["content"])


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = patch.object(v, "VOICE_CONFIG_FILE", Path(directory.name) / "voice.json")
        override.start()
        self.addCleanup(override.stop)
        self.cog = v.VoiceRooms(Mock())
        self.trigger = Mock(id=10, category=None, overwrites={}, user_limit=0, bitrate=64000)
        self.room = Mock(id=20, members=[], delete=AsyncMock(), send=AsyncMock(), edit=AsyncMock())
        self.guild = Mock(id=1, bitrate_limit=96000, create_voice_channel=AsyncMock(return_value=self.room))
        self.guild.get_channel.side_effect = lambda cid: self.room if cid == 20 else self.trigger if cid == 10 else None
        self.member = Mock(id=2, bot=False, guild=self.guild, display_name="테스트", move_to=AsyncMock())
        self.member.voice = SimpleNamespace(channel=self.trigger)
        self.cog.save(1, {"enabled": True, "trigger_channel_id": 10})

    async def test_creation_and_reentry_reuses_room(self):
        await self.cog.create_room(self.member, self.trigger)
        await self.cog.create_room(self.member, self.trigger)
        self.guild.create_voice_channel.assert_awaited_once()
        self.assertEqual(self.member.move_to.await_count, 2)
        self.assertEqual(self.cog.config(1)["rooms"], {"20": 2})
        self.assertIsNone(self.guild.create_voice_channel.await_args.kwargs["overwrites"][self.member].manage_channels)

    async def test_only_tracked_empty_rooms_are_deleted(self):
        await self.cog.remove_empty(self.guild, 10)
        self.trigger.delete.assert_not_called()
        self.cog.save(1, {"trigger_channel_id": 10, "rooms": {"20": 2}})
        self.room.members = [self.member]
        await self.cog.remove_empty(self.guild, 20)
        self.room.delete.assert_not_awaited()
        self.room.members = []
        await self.cog.remove_empty(self.guild, 20)
        self.room.delete.assert_awaited_once()
        self.assertEqual(self.cog.config(1)["rooms"], {})

    async def test_move_failure_cleans_new_room(self):
        self.member.move_to.side_effect = RuntimeError("left server")
        with self.assertRaises(RuntimeError):
            await self.cog.create_room(self.member, self.trigger)
        self.room.delete.assert_awaited_once()
        self.assertEqual(self.cog.config(1)["rooms"], {})

    async def test_departure_during_creation_cleans_room(self):
        async def create(**kwargs):
            self.member.voice = None
            return self.room
        self.guild.create_voice_channel.side_effect = create
        await self.cog.create_room(self.member, self.trigger)
        self.member.move_to.assert_not_awaited()
        self.room.delete.assert_awaited_once()

    async def test_only_owner_can_rename_and_cooldown_applies(self):
        self.member.voice.channel = self.room
        self.cog.save(1, {"rooms": {"20": 3}})
        interaction = SimpleNamespace(guild=self.guild, user=self.member,
                                      response=Mock(defer=AsyncMock()), followup=Mock(send=AsyncMock()))
        await v.VoiceRooms.rename.callback(self.cog, interaction, "새 방")
        self.room.edit.assert_not_awaited()
        self.cog.save(1, {"rooms": {"20": 2}})
        await v.VoiceRooms.rename.callback(self.cog, interaction, "새 방")
        await v.VoiceRooms.rename.callback(self.cog, interaction, "또 다른 방")
        self.room.edit.assert_awaited_once()

