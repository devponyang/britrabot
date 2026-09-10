import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from cogs import automation as a
import storage
from discord_helpers import send_embed_pages, open_ticket


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "guild.json"
        self.records = Path(directory.name) / "records.json"
        for override in (patch.object(a, "CONFIG_FILE", self.config), patch.object(a, "ARTIFACT_RECORDS_FILE", self.records)):
            override.start()
            self.addCleanup(override.stop)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 50
        self.channel.send = AsyncMock()
        self.guild = Mock(id=a.OFFICIAL_NOTICE_GUILD_ID)
        self.guild.get_channel.return_value = self.channel
        self.bot = Mock(guilds=[self.guild])
        self.cog = a.Automation(self.bot)

    def test_history_merge_preserves_old_round_and_summary(self):
        history = {"pair": "브리트라 VS 상대", "source_url": "https://example.org",
                   "records": [{"date": "2026-09-01", "round": "1차전", "scores": ["1:0"]}],
                   "record": {"matchup": {"round": "1차전"}}}
        a.save_artifact_history(history)
        a.save_artifact_history({**history, "records": [{"date": "2026-09-02", "round": "2차전", "scores": ["2:0"]}]})
        history.pop("record")
        a.save_artifact_history(history)
        saved = a.load_artifact_history(history["pair"])
        self.assertEqual(len(saved["records"]), 2)
        self.assertIn("record", saved)

    async def test_notice_initialization_survives_missing_config(self):
        articles = [{"url": "https://example.org/one", "category": "공지", "title": "one"}]
        with patch.object(a.aion2_scraper, "get_latest_official_articles", new=AsyncMock(return_value=articles)):
            await self.cog.official_notice_loop()
            await a.Automation(self.bot).official_notice_loop()
        self.channel.send.assert_not_awaited()
        self.assertEqual(a.get_guild_config(self.guild.id)["official_seen_articles"], [articles[0]["url"]])

    async def test_notice_order_and_restart_deduplication(self):
        a.set_guild_config(self.guild.id, "official_initialized_categories", ["공지"])
        old = [f"https://example.org/{i}" for i in range(105)]
        a.set_guild_config(self.guild.id, "official_seen_articles", old)
        articles = [{"url": "https://example.org/new", "category": "공지", "title": "new"},
                    {"url": old[-1], "category": "공지", "title": "old"}]
        with patch.object(a.aion2_scraper, "get_latest_official_articles", new=AsyncMock(return_value=articles)):
            await self.cog.official_notice_loop()
            await a.Automation(self.bot).official_notice_loop()
        self.channel.send.assert_awaited_once()
        self.assertEqual(a.get_guild_config(self.guild.id)["official_seen_articles"], old + [articles[0]["url"]])

    async def test_alarm_has_no_scraper_dependency_and_survives_restart(self):
        a.set_guild_config(self.guild.id, "alarm_channel", 50)
        fixed = datetime.datetime(2026, 9, 10, 9, 0, tzinfo=a.KST)
        clock = Mock(wraps=datetime.datetime)
        clock.now.return_value = fixed
        with patch.object(a, "datetime", Mock(wraps=datetime, datetime=clock)), patch.object(self.cog, "_check_artifact_result", new=AsyncMock()) as scrape:
            await self.cog.alarm_loop()
            await a.Automation(self.bot).alarm_loop()
            scrape.assert_not_awaited()
        self.channel.send.assert_awaited_once()

    async def test_failed_alarm_retries_in_catchup_window(self):
        a.set_guild_config(self.guild.id, "alarm_channel", 50)
        clock = Mock(wraps=datetime.datetime)
        clock.now.return_value = datetime.datetime(2026, 9, 10, 9, 0, tzinfo=a.KST)
        self.channel.send.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "denied")
        with patch.object(a, "datetime", Mock(wraps=datetime, datetime=clock)):
            with self.assertLogs("cogs.automation", level="ERROR"):
                await self.cog.alarm_loop()
            self.assertFalse(a.get_guild_config(self.guild.id).get("sent_alarm_keys"))
            self.channel.send.side_effect = None
            clock.now.return_value = datetime.datetime(2026, 9, 10, 9, 2, tzinfo=a.KST)
            await self.cog.alarm_loop()
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_background_error_does_not_escape_loop(self):
        self.config.write_text("invalid", encoding="utf-8")
        with self.assertLogs(level="ERROR"):
            await self.cog.alarm_loop()

    async def test_long_history_embeds_are_split_without_losing_text(self):
        embed = discord.Embed(title="History")
        for i in range(30):
            embed.add_field(name=str(i), value="x" * 1500)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        await send_embed_pages(interaction, embed)
        pages = [call.kwargs["embed"] for call in interaction.followup.send.await_args_list]
        self.assertTrue(all(len(page) <= 6000 and len(page.fields) <= 25 for page in pages))
        self.assertEqual(sum(len(field.value) for page in pages for field in page.fields), 45000)

    async def test_duplicate_ticket_creation_is_serialized(self):
        started, release = asyncio.Event(), asyncio.Event()
        guild = Mock(id=10, text_channels=[])
        channel = Mock(mention="#ticket", send=AsyncMock())
        async def create(*args, **kwargs):
            started.set()
            await release.wait()
            return channel
        guild.create_text_channel = AsyncMock(side_effect=create)
        interaction = SimpleNamespace(guild=guild, user=Mock(id=1, display_name="user"),
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True), followup=SimpleNamespace(send=AsyncMock()))
        task = asyncio.create_task(open_ticket(interaction))
        await started.wait()
        await open_ticket(interaction)
        release.set()
        await task
        guild.create_text_channel.assert_awaited_once()
