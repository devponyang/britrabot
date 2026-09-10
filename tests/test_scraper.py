from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aion2_scraper as s


class ScraperTests(unittest.IsolatedAsyncioTestCase):
    def test_code_is_exact_token(self):
        self.assertTrue(s.contains_code("인증: `Code123`", "Code123"))
        self.assertFalse(s.contains_code("OtherCode123", "Code123"))
        self.assertFalse(s.contains_code("Code1234", "Code123"))

    def test_only_official_https_urls_are_accepted(self):
        self.assertTrue(s.is_official_url("https://aion2.plaync.com/ko-kr/board/server/view?a=b"))
        for url in ("http://aion2.plaync.com/", "https://aion2.plaync.com.evil.test/",
                    "https://aion2.plaync.com@evil.test/", "file:///etc/passwd", None):
            self.assertFalse(s.is_official_url(url))

    async def test_redirect_to_error_page_is_source_error(self):
        page = Mock(url="https://www.plaync.com/error/404")
        page.goto = AsyncMock(return_value=Mock(status=200))
        with self.assertRaises(s.ScrapeUnavailable):
            await s.goto_official(page, "https://aion2.plaync.com/ko-kr/board/server/view")

    async def test_code_on_second_comment_page(self):
        locator = Mock()
        locator.first.wait_for = AsyncMock()
        locator.evaluate_all = AsyncMock(side_effect=[
            [{"text": "other", "content": "not this", "nickname": "one", "href": "/profile/1"}],
            [{"text": "target", "content": "Code123", "nickname": "two", "href": "/profile/2"}],
        ])
        page = Mock()
        page.locator.return_value = locator
        @asynccontextmanager
        async def context():
            yield page
        with patch.object(s, "browser_page", context), patch.object(s, "goto_official", AsyncMock()), \
             patch.object(s, "advance_comments", AsyncMock(return_value=True)):
            result = await s.find_comment_by_code("https://aion2.plaync.com/", "Code123")
        self.assertEqual(result["profile_url"], "https://aion2.plaync.com/profile/2")

    async def test_missing_comment_container_is_not_missing_code(self):
        page = Mock()
        page.locator.return_value.first.wait_for = AsyncMock(side_effect=TimeoutError())
        @asynccontextmanager
        async def context():
            yield page
        with patch.object(s, "browser_page", context), patch.object(s, "goto_official", AsyncMock()):
            with self.assertRaises(s.ScrapeUnavailable):
                await s.find_comment_by_code("https://aion2.plaync.com/", "Code123")

    async def test_page_is_closed_on_failure(self):
        page = Mock(close=AsyncMock())
        browser = Mock(new_page=AsyncMock(return_value=page))
        with patch.object(s, "_get_browser", AsyncMock(return_value=browser)):
            with self.assertRaises(ValueError):
                async with s.browser_page():
                    raise ValueError("failed")
        page.close.assert_awaited_once()

    async def test_disconnected_browser_is_recreated(self):
        old = Mock(is_connected=Mock(return_value=False))
        driver = Mock(stop=AsyncMock())
        new_browser = Mock()
        new_driver = Mock(stop=AsyncMock())
        new_driver.chromium.launch = AsyncMock(return_value=new_browser)
        factory = Mock(start=AsyncMock(return_value=new_driver))
        with patch.object(s, "_browser", old), patch.object(s, "_playwright", driver), \
             patch.object(s, "async_playwright", Mock(return_value=factory)):
            self.assertIs(await s._get_browser(), new_browser)
            driver.stop.assert_awaited_once()
