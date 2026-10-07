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
parser.add_argument('--locale', choices=('en-US', 'ko-KR'), help='Probe one locale and include API error details')
parser.add_argument('--compare-platforms', action='store_true', help='Compare Windows/Linux User-Agent strings (does not emulate an OS)')
args = parser.parse_args()
URL = args.url
if not s.is_official_url(URL):
    raise SystemExit('Only official AION2 HTTPS profile URLs are accepted')
async def main():
    try:
        if args.compare_locales or args.locale or args.compare_platforms:
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
    cases = [(locale, None) for locale in ((args.locale,) if args.locale else ('en-US', 'ko-KR'))]
    if args.compare_platforms:
        cases = [(args.locale or 'ko-KR', os_name) for os_name in ('Linux', 'Windows')]
    for locale, os_name in cases:
        label = {'locale': locale, 'user_agent_platform': os_name or 'native'}
        print(json.dumps({**label, 'stage': 'opening_page'}), flush=True)
        options = {'locale': locale}
        if os_name:
            os_token = 'X11; Linux x86_64' if os_name == 'Linux' else 'Windows NT 10.0; Win64; x64'
            options['user_agent'] = (f'Mozilla/5.0 ({os_token}) AppleWebKit/537.36 '
                                     f'(KHTML, like Gecko) HeadlessChrome/{browser.version} Safari/537.36')
        page = await browser.new_page(**options)
        api_responses = []
        error_responses = []
        try:
            def record(response):
                parsed = urlsplit(response.url)
                if (parsed.hostname == 'aion2.plaync.com' and parsed.path.startswith('/api/')
                        and len(api_responses) < 30):
                    headers = response.headers
                    api_responses.append({
                        'path': parsed.path,
                        'status': response.status,
                        'method': response.request.method,
                        'headers': {name: headers[name] for name in
                                    ('content-type', 'server', 'via', 'x-cache', 'x-amz-cf-pop') if name in headers},
                    })
                    if response.status >= 400:
                        error_responses.append((response, api_responses[-1]))
            page.on('response', record)
            response = None
            try:
                response = await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
            except s.PlaywrightTimeoutError:
                print(json.dumps({**label, 'stage': 'navigation_timeout',
                                  'message': 'Collecting any API responses already received'}), flush=True)
            print(json.dumps({'locale': locale, 'stage': 'waiting_for_profile', 'timeout_seconds': 20}), flush=True)
            loaded = False
            try:
                await page.locator('.profile__info-desc').wait_for(timeout=20000)
                loaded = True
            except s.PlaywrightTimeoutError:
                pass
            # Snapshot before awaiting bodies; callbacks may receive more responses.
            await asyncio.gather(*(read_api_problem(reply, entry)
                                   for reply, entry in list(error_responses)))
            print(json.dumps({**label, 'document_status': response.status if response else None,
                              'profile_loaded': loaded, 'api_responses': api_responses}, ensure_ascii=True), flush=True)
        finally:
            await page.close()


async def read_api_problem(response, entry):
    """Only selected error fields, no successful character data or auth headers."""
    try:
        async with asyncio.timeout(5):
            payload = await response.json()
        if isinstance(payload, dict):
            entry['problem'] = {
                key: value[:500] if isinstance(value, str) else value
                for key in ('type', 'title', 'status', 'detail', 'code', 'message')
                if isinstance(value := payload.get(key), (str, int, float))
            }
    except Exception as error:
        entry['problem_read_error'] = type(error).__name__


asyncio.run(main())
