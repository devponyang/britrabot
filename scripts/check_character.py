"""Read-only character diagnostic: no Discord login, no mutation."""
import asyncio
import argparse
import json
import platform
import sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aion2_scraper as s

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('url', nargs='?', default='https://aion2.plaync.com/ko-kr/characters/2008/CUO8quJPF5gPWedlhvPi7OVDLqjQY7q8Z2tvHOHY0IU%3D')
parser.add_argument('--compare-locales', action='store_true', help='Compare en-US and ko-KR API responses without running verification')
args = parser.parse_args()
URL = args.url
if not s.is_official_url(URL):
    raise SystemExit('Only official AION2 HTTPS profile URLs are accepted')
async def main():
    try:
        if args.compare_locales:
            await compare_locales()
            return
        async with s.browser_page() as page:
            failures = []
            http_errors = []
            page_errors = []
            def safe_address(url):
                parsed = urlsplit(url)
                return f'{parsed.hostname or ""}{parsed.path}'
            def failed_request(request):
                if len(failures) < 20:
                    failures.append({'address': safe_address(request.url), 'type': request.resource_type, 'error': request.failure})
            def failed_response(response):
                if response.status >= 400 and len(http_errors) < 20:
                    http_errors.append({'address': safe_address(response.url), 'status': response.status})
            page.on('requestfailed', failed_request)
            page.on('response', failed_response)
            # Error names identify JavaScript failures without printing page data.
            page.on('pageerror', lambda error: page_errors.append(error.name) if len(page_errors) < 20 else None)
            response = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
            try:
                await page.locator('.profile__info-desc').wait_for(timeout=20000)
            except Exception as error:
                print('wait_error:', type(error).__name__)
            print(json.dumps({'url': page.url, 'status': response.status if response else None, 'title': await page.title(), 'profile_count': await page.locator('.profile__info-desc').count(), 'profile_classes': await page.locator('[class*="profile"]').evaluate_all('els => [...new Set(els.map(e=>e.className))]')}, ensure_ascii=True), flush=True)
            print(json.dumps({'failed_requests': failures, 'http_errors': http_errors, 'javascript_error_names': page_errors}, ensure_ascii=True), flush=True)
        async with asyncio.timeout(150):
            info = await s.get_character_info(URL)
        print(json.dumps({'character_info': info}, ensure_ascii=True))
    finally:
        await s.close_browser()


async def compare_locales():
    """Record only public API status and selected headers; never cookies/tokens."""
    browser = await s._get_browser()
    print(json.dumps({'platform': platform.system(), 'browser': browser.version}), flush=True)
    for locale in ('en-US', 'ko-KR'):
        page = await browser.new_page(locale=locale)
        api_responses = []
        try:
            def record(response):
                parsed = urlsplit(response.url)
                if parsed.hostname == 'aion2.plaync.com' and parsed.path.startswith('/api/'):
                    headers = response.headers
                    api_responses.append({
                        'path': parsed.path,
                        'status': response.status,
                        'method': response.request.method,
                        'headers': {name: headers[name] for name in
                                    ('content-type', 'server', 'via', 'x-cache', 'x-amz-cf-pop') if name in headers},
                    })
            page.on('response', record)
            response = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
            loaded = False
            try:
                await page.locator('.profile__info-desc').wait_for(timeout=20000)
                loaded = True
            except s.PlaywrightTimeoutError:
                pass
            print(json.dumps({'locale': locale, 'document_status': response.status if response else None,
                              'profile_loaded': loaded, 'api_responses': api_responses}, ensure_ascii=True), flush=True)
        finally:
            await page.close()


asyncio.run(main())
