"""Read-only character diagnostic: no Discord login, no mutation."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aion2_scraper as s

URL = sys.argv[1] if len(sys.argv) > 1 else 'https://aion2.plaync.com/ko-kr/characters/2008/CUO8quJPF5gPWedlhvPi7OVDLqjQY7q8Z2tvHOHY0IU%3D'
if not s.is_official_url(URL):
    raise SystemExit('Only official AION2 HTTPS profile URLs are accepted')
async def main():
    try:
        async with s.browser_page() as page:
            response = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
            try:
                await page.locator('.profile__info-desc').wait_for(timeout=20000)
            except Exception as error:
                print('wait_error:', type(error).__name__)
            print(json.dumps({'url': page.url, 'status': response.status if response else None, 'title': await page.title(), 'profile_count': await page.locator('.profile__info-desc').count(), 'profile_classes': await page.locator('[class*="profile"]').evaluate_all('els => [...new Set(els.map(e=>e.className))]')}, ensure_ascii=True), flush=True)
        async with asyncio.timeout(150):
            info = await s.get_character_info(URL)
        print(json.dumps({'character_info': info}, ensure_ascii=True))
    finally:
        await s.close_browser()
asyncio.run(main())
