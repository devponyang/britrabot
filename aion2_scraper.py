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
import re
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright

# 실제 아이온2 게시판과 캐릭터 정보를 조회합니다.
DUMMY_MODE = False

BASE_URL = "https://aion2.plaync.com"
ARTIFACT_RESULT_URL = "https://aion2tool.com/server-comparison"

OFFICIAL_BOARD_URLS = {
    "이벤트": f"{BASE_URL}/ko-kr/eventon",
    "공지": f"{BASE_URL}/ko-kr/board/notice/list",
    "업데이트": f"{BASE_URL}/ko-kr/board/update/list",
    "CM 아지트": f"{BASE_URL}/ko-kr/board/cm_story/list",
}
EXCLUDED_OFFICIAL_TITLE_PARTS = (
    "운영정책 위반 및 임시보호 계정들에 대한 게임 이용제한 안내",
)


def is_excluded_official_article(category: str, title: str) -> bool:
    """공지 알림에서 제외할 게시글인지 확인합니다."""
    return category == "공지" and any(
        excluded_part in title for excluded_part in EXCLUDED_OFFICIAL_TITLE_PARTS
    )

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


async def get_latest_official_articles(limit: int = 10) -> list[dict]:
    """공식 사이트 네 게시판에서 최신 게시글 목록을 수집합니다."""
    if DUMMY_MODE:
        return []

    browser = await _get_browser()
    page = await browser.new_page()
    articles = []
    seen_urls = set()
    try:
        for category, list_url in OFFICIAL_BOARD_URLS.items():
            await page.goto(list_url, wait_until="networkidle", timeout=30000)
            links = await page.locator("a[href*='/view?articleId=']").evaluate_all(
                """els => els.map(a => ({href: a.href, text: (a.innerText || '').trim()}))"""
            )
            category_count = 0
            for link in links:
                url = link["href"]
                if url in seen_urls:
                    continue
                title = re.sub(r"\s+", " ", link["text"]).strip()
                if not title or is_excluded_official_article(category, title):
                    continue
                seen_urls.add(url)
                articles.append({"category": category, "title": title, "url": url})
                category_count += 1
                if category_count >= limit:
                    break
        return articles
    finally:
        await page.close()


async def get_latest_artifact_result(expected_date=None) -> dict | None:
    """아툴에서 최근 집계 완료된 아티팩트쟁 결과를 가져옵니다."""
    if DUMMY_MODE:
        return None

    browser = await _get_browser()
    page = await browser.new_page()
    try:
        await page.goto(ARTIFACT_RESULT_URL, wait_until="domcontentloaded", timeout=30000)
        body_text = ""
        for _ in range(15):
            body_text = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
            if "결과 집계완료" in body_text:
                break
            await page.wait_for_timeout(1000)
        completed = re.findall(
            r"✅\s*(\d+월\s*\d+일\s*\(\d+차전\))\s*결과 집계완료", body_text
        )
        if not completed:
            return None

        date_match = re.search(r"(\d+)월\s*(\d+)일", completed[0])
        if expected_date and date_match:
            result_month, result_day = map(int, date_match.groups())
            if (result_month, result_day) != (expected_date.month, expected_date.day):
                return None

        round_summary = None
        summary_match = re.search(
            r"(\d+차전 라운드 요약.*?)(?=📊 한눈에|🏆 서버 점령 순위)",
            body_text,
        )
        if summary_match:
            round_summary = summary_match.group(1).strip()

        return {
            "completion": completed[0],
            "summary": round_summary,
            "url": ARTIFACT_RESULT_URL,
        }
    finally:
        await page.close()


async def get_artifact_server_record(opponent_server: str) -> dict | None:
    """아툴에서 브리트라와 상대 서버의 아티팩트 전적을 가져옵니다."""
    if DUMMY_MODE:
        return None

    browser = await _get_browser()
    page = await browser.new_page()
    try:
        await page.goto(ARTIFACT_RESULT_URL, wait_until="domcontentloaded", timeout=30000)
        body_text = ""
        for _ in range(15):
            body_text = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
            if "브리트라" in body_text and "누적" in body_text:
                break
            await page.wait_for_timeout(1000)

        record = parse_artifact_server_record(body_text, opponent_server)
        if record is None:
            return None
        return record | {"url": ARTIFACT_RESULT_URL}
    finally:
        await page.close()


async def get_artifact_server_history(opponent_server: str) -> dict | None:
    """아툴의 해당 서버 매칭 기록보기를 열어 회차별 기록을 가져옵니다."""
    if DUMMY_MODE:
        return None

    browser = await _get_browser()
    page = await browser.new_page()
    try:
        await page.goto(ARTIFACT_RESULT_URL, wait_until="domcontentloaded", timeout=30000)
        body_text = ""
        for _ in range(15):
            body_text = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
            if opponent_server in body_text and "기록 보기" in body_text:
                break
            await page.wait_for_timeout(1000)

        if parse_artifact_server_record(body_text.replace("브리 트라", "브리트라"), opponent_server) is None:
            return None

        record_buttons = page.get_by_text("기록 보기", exact=True)
        target = None
        target_text_length = None
        for index in range(await record_buttons.count()):
            candidate = record_buttons.nth(index)
            ancestor = candidate
            for _ in range(8):
                text = (await ancestor.inner_text()).strip()
                if "브리트라" in text and opponent_server in text:
                    if target_text_length is None or len(text) < target_text_length:
                        target = candidate
                        target_text_length = len(text)
                ancestor = ancestor.locator("..")

        if target is None:
            return None
        await target.click()
        await page.wait_for_timeout(500)

        rows = await page.locator("table tr").evaluate_all(
            """rows => rows.map(row => ({
                cells: Array.from(row.cells).map(cell => ({
                    text: (cell.innerText || '').trim(),
                    images: Array.from(cell.querySelectorAll('img')).map(img => img.src)
                }))
            }))"""
        )
        return {
            "pair": f"브리트라 VS {opponent_server}",
            "records": parse_artifact_history_rows(rows),
            "source_url": page.url,
        }
    finally:
        await page.close()


def parse_artifact_history_rows(rows: list[dict]) -> list[dict]:
    """기록보기 표의 행을 JSON 저장용 회차 데이터로 정리합니다."""
    records = []
    for row in rows:
        cells = row.get("cells", [])
        cell_texts = [re.sub(r"\s+", " ", cell.get("text", "")).strip() for cell in cells]
        joined = " ".join(cell_texts)
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})\s*\((\d+차전)\)", joined)
        if not date_match:
            continue
        scores = [value for value in cell_texts if re.fullmatch(r"\d+:\d+", value)]
        images = [image for cell in cells for image in cell.get("images", [])]
        records.append(
            {
                "date": date_match.group(1),
                "round": date_match.group(2),
                "scores": scores,
                "cells": cell_texts,
                "images": images,
            }
        )
    return records


def parse_artifact_server_record(body_text: str, opponent_server: str) -> dict | None:
    """렌더링된 아툴 본문에서 브리트라와 상대 서버의 전적을 추출합니다."""
    server_pattern = r"([가-힣A-Za-z0-9]+)\s+(WIN|LOSE|DRAW)\s+(\d+)\s+(\d+차전)\s+⚔️\s+(\d+)\s+([가-힣A-Za-z0-9]+)\s+(WIN|LOSE|DRAW)\s+누적\s+(\d+):(\d+)"
    for match in re.finditer(server_pattern, body_text):
        left_server, left_result, left_round, round_name, right_round, right_server, right_result, left_total, right_total = match.groups()
        if {left_server, right_server} != {"브리트라", opponent_server}:
            continue
        if left_server == "브리트라":
            breitra_result, opponent_result = left_result, right_result
            breitra_round, opponent_round = int(left_round), int(right_round)
            breitra_total, opponent_total = int(left_total), int(right_total)
        else:
            breitra_result, opponent_result = right_result, left_result
            breitra_round, opponent_round = int(right_round), int(left_round)
            breitra_total, opponent_total = int(right_total), int(left_total)

        def capture_count(server_name: str) -> int | None:
            count_match = re.search(rf"{re.escape(server_name)}\s+(\d+)회", body_text)
            return int(count_match.group(1)) if count_match else None

        completion = re.search(
            r"✅\s*(\d+월\s*\d+일\s*\(\d+차전\))\s*결과 집계완료", body_text
        )
        return {
            "opponent_server": opponent_server,
            "opponent_capture_count": capture_count(opponent_server),
            "breitra_capture_count": capture_count("브리트라"),
            "completion": completion.group(1) if completion else None,
            "matchup": {
                "round": round_name,
                "breitra_result": breitra_result,
                "opponent_result": opponent_result,
                "breitra_round": breitra_round,
                "opponent_round": opponent_round,
                "breitra_total": breitra_total,
                "opponent_total": opponent_total,
            },
        }
    return None


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
