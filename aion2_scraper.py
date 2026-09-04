"""
아이온2 홈페이지(aion2.plaync.com)에서 정보를 가져오는 함수 모음.

이 사이트는 자바스크립트로 그려지는 SPA라서 requests+BeautifulSoup로는
내용을 볼 수 없어 Playwright(헤드리스 브라우저)로 실제 렌더링 후 읽어옵니다.
특히 '종족'과 '레기온' 이름은 CSS ::before content로 그려지는 텍스트라
일반 텍스트 추출(textContent)로는 안 잡히고, getComputedStyle로 직접
계산된 스타일 값을 읽어와야 합니다.

⚠️ 현재 상태:
  - get_character_info(profile_url): 완성됨 (개발자도구로 확인한 실제 구조 기반)
    - find_comment_by_code(article_url, code): 댓글 본문과 작성자 정보를 조회합니다.
    확인되는 대로 이 함수만 채우면 전체 기능이 완성됩니다.

  DUMMY_MODE가 True인 동안에는 실제 조회 없이 정해진 더미 데이터를 돌려줘서
  디스코드 쪽 흐름만 먼저 테스트할 수 있습니다.
"""

import asyncio
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright

# 실제 아이온2 게시판과 캐릭터 정보를 조회합니다.
DUMMY_MODE = False

BASE_URL = "https://aion2.plaync.com"

_playwright = None
_browser = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    """헤드리스 브라우저를 한 번만 켜두고 재사용합니다."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is None:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def close_browser():
    """봇 종료 시 호출하면 좋습니다 (선택 사항)."""
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


async def find_comment_by_code(article_url: str, code: str):
    """
    인증 게시글의 댓글 목록에서 `code`가 포함된 댓글을 찾아
    작성자 닉네임과 프로필 URL을 반환합니다.

    찾으면: {"nickname": "작성자닉네임", "profile_url": "https://aion2.plaync.com/..."}
    못 찾으면: None
    """
    if DUMMY_MODE:
        if code.startswith("TEST"):
            return {
                "nickname": "더미테스트유저",
                "profile_url": f"{BASE_URL}/ko-kr/profile/character/0000/dummy",
            }
        return None

    browser = await _get_browser()
    page = await browser.new_page()
    try:
        await page.goto(article_url, wait_until="networkidle", timeout=15000)

        comment_articles = await page.query_selector_all("div.comment-article")
        for comment in comment_articles:
            content_el = await comment.query_selector("div.comment-contents")
            if not content_el:
                continue
            content_text = await content_el.inner_text()
            if code not in content_text:
                continue

            writer_el = await comment.query_selector("div.writer a.name")
            if not writer_el:
                continue
            nickname = (await writer_el.inner_text()).strip()
            href = await writer_el.get_attribute("href")
            profile_url = BASE_URL + href if href and href.startswith("/") else href
            return {"nickname": nickname, "profile_url": profile_url}

        return None
    finally:
        await page.close()


async def get_character_info(profile_url: str):
    """
    댓글 작성자의 프로필 페이지(profile_url)를 렌더링해서
    닉네임/서버/종족/레기온을 반환합니다.

    찾으면: {"nickname": "...", "server": "...", "race": "...", "legion": "..."}
    못 찾으면: None
    """
    if DUMMY_MODE:
        return {
            "nickname": "더미테스트유저",
            "server": "브리트라",
            "race": "마족",
            "legion": "더미레기온",
        }

    browser = await _get_browser()
    page = await browser.new_page()
    try:
        parsed_url = urlsplit(profile_url)
        detail_path = parsed_url.path.replace("/profile/character/", "/characters/", 1)
        detail_path = detail_path.split("/board/", 1)[0]
        detail_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, detail_path, parsed_url.query, "")
        )

        await page.goto(profile_url, wait_until="networkidle", timeout=15000)
        class_el = await page.query_selector(".classcard")
        class_name = (await class_el.inner_text()).strip() if class_el else None

        await page.goto(detail_url, wait_until="networkidle", timeout=15000)

        desc = page.locator(".profile__info-desc")
        try:
            await desc.wait_for(timeout=10000)
        except Exception:
            return None  # 캐릭터 정보가 없는 프로필이거나 페이지 구조가 다름

        name_el = await page.query_selector(".profile__info-name")
        if not name_el:
            return None
        nickname = (await name_el.inner_text()).strip()

        desc_handle = await desc.element_handle()

        # 서버명: profile__info-desc의 첫 번째 자식 div (순수 텍스트, ::before 아님)
        server = await page.evaluate(
            "(el) => el.children[0] ? el.children[0].textContent.trim() : null",
            desc_handle,
        )

        # 종족: 현재 페이지에서는 요소의 일반 텍스트로 제공됩니다.
        race_el = await page.query_selector(".profile__info-race")
        race = None
        if race_el:
            race = (await race_el.inner_text()).strip() or None

        # 레기온: profile__info-desc의 3번째 자식 div
        legion = await page.evaluate(
            """(el) => {
                const child = el.children[2];
                if (!child) return null;
                const text = child.textContent.trim();
                if (text) return text;
                const v = window.getComputedStyle(child, '::before').content;
                return v ? v.replace(/^["']|["']$/g, '') : null;
            }""",
            desc_handle,
        )

        if not server:
            return None

        return {
            "nickname": nickname,
            "class_name": class_name or "알 수 없음",
            "server": server,
            "race": race,
            "legion": legion or "없음",
        }
    finally:
        await page.close()
