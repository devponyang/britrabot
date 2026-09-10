"""Read-only check of configured public verification pages; never logs codes."""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aion2_scraper


async def main():
    root = Path(__file__).resolve().parents[1]
    configs = json.loads((root / "verify_config.json").read_text(encoding="utf-8"))
    urls = list(dict.fromkeys(c.get("article_url") for c in configs.values() if c.get("article_url")))
    async with async_playwright() as playwright:
        options = {"headless": True}
        if len(sys.argv) > 1:
            options["channel"] = sys.argv[1]
        browser = await playwright.chromium.launch(**options)
        try:
            for url in urls:
                page = await browser.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.locator("div.comment-article").first.wait_for(timeout=10000)
                    except Exception:
                        pass
                    controls = await page.locator('button, [class*="paging"] a, [class*="pagination"] a').evaluate_all(
                        "els => els.map(e => ({tag:e.tagName, cls:e.className, text:e.innerText.trim()}))"
                        ".filter(e => /더|다음|이전|more|paging|pagination/.test(e.text + e.cls))"
                    )
                    print(json.dumps({"requested_url": url, "final_url": page.url,
                                      "status": response.status if response else None,
                                      "comments": await page.locator('div.comment-article').count(),
                                      "controls": controls}, ensure_ascii=True), flush=True)
                    if await page.locator('div.comment-article').count():
                        print(json.dumps(await page.locator('div.comment-article').first.evaluate(
                            "e => {let a=[]; for(let i=0;e && i<7;i++,e=e.parentElement) a.push({tag:e.tagName,cls:e.className}); return a;}"), ensure_ascii=True))
                        previous = "\n".join(await page.locator('div.comment-article').all_inner_texts())
                        advanced = await aion2_scraper.advance_comments(page, previous)
                        print(json.dumps({"pagination_advanced": advanced, "comments_after": await page.locator('div.comment-article').count()}), flush=True)
                        href = await page.locator('div.comment-article div.writer a.name').first.get_attribute('href')
                        from urllib.parse import urljoin
                        aion2_scraper._browser = browser
                        info = await aion2_scraper.get_character_info(urljoin(aion2_scraper.BASE_URL, href))
                        print(json.dumps({"character_fields_present": {k: v is not None for k,v in (info or {}).items()}}, ensure_ascii=True))
                finally:
                    await page.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
