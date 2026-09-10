"""아이온2 공식 게시판/캐릭터 및 아툴 통계를 비동기로 조회합니다."""

import asyncio
import re
import logging
from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import urlsplit, urlunsplit, urljoin

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
_page_slots = asyncio.Semaphore(4)
_background_slots = asyncio.Semaphore(1)
logger = logging.getLogger(__name__)


class ScrapeUnavailable(RuntimeError):
    """The source could not be read reliably; do not penalize the user."""


def is_official_url(url):
    try:
        parsed = urlsplit(url.strip())
        return (parsed.scheme == "https" and parsed.hostname == "aion2.plaync.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (AttributeError, ValueError):
        return False


@asynccontextmanager
async def browser_page():
    async with _page_slots:
        browser = await _get_browser()
        page = await browser.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                logger.exception("브라우저 페이지 정리 실패")


async def _get_browser():
    """헤드리스 브라우저를 한 번만 켜두고 재사용합니다."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            if _playwright is not None:
                await _playwright.stop()
                _playwright = None
            _browser = None
            _playwright = await async_playwright().start()
            try:
                _browser = await _playwright.chromium.launch(headless=True)
            except BaseException:
                await _playwright.stop()
                _playwright = None
                raise
    return _browser


async def close_browser():
    """봇 종료 시 브라우저와 Playwright 런타임을 정리합니다."""
    global _playwright, _browser
    async with _browser_lock:
        try:
            if _browser:
                await _browser.close()
        finally:
            _browser = None
            if _playwright:
                await _playwright.stop()
                _playwright = None


def background_scrape(callback):
    @wraps(callback)
    async def run(*args, **kwargs):
        # Reserve capacity for verification while periodic collection is active.
        async with _background_slots:
            return await callback(*args, **kwargs)
    return run


@background_scrape
async def get_latest_official_articles(limit: int = 10) -> list[dict]:
    """공식 사이트 네 게시판에서 최신 게시글 목록을 수집합니다."""
    if DUMMY_MODE:
        return []

    articles = []
    seen_urls = set()
    async with browser_page() as page:
        for category, list_url in OFFICIAL_BOARD_URLS.items():
            try:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                await page.locator("a[href*='/view?articleId=']").first.wait_for(timeout=15000)
            except Exception:
                logger.exception("공식 게시판 수집 실패: %s", category)
                continue
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


@background_scrape
async def get_latest_artifact_result(expected_date=None) -> dict | None:
    """아툴에서 최근 집계 완료된 아티팩트쟁 결과를 가져옵니다."""
    if DUMMY_MODE:
        return None

    async with browser_page() as page:
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

        breitra_record = parse_breitra_artifact_result(body_text)
        if breitra_record is None:
            return None
        breitra_record["territories"] = await get_breitra_territory_results(
            page, breitra_record["left_server"], breitra_record["right_server"]
        )

        return {
            "completion": completed[0],
            "record": breitra_record,
            "url": ARTIFACT_RESULT_URL,
        }


@background_scrape
async def get_artifact_server_record(opponent_server: str) -> dict | None:
    """아툴에서 브리트라와 상대 서버의 아티팩트 전적을 가져옵니다."""
    if DUMMY_MODE:
        return None

    async with browser_page() as page:
        await page.goto(ARTIFACT_RESULT_URL, wait_until="domcontentloaded", timeout=30000)
        body_text = ""
        for _ in range(15):
            body_text = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
            if "브리트라" in body_text and "누적" in body_text:
                break
            await page.wait_for_timeout(1000)

        record = parse_artifact_server_record(body_text.replace("브리 트라", "브리트라"), opponent_server)
        if record is None:
            return None
        return record | {"url": ARTIFACT_RESULT_URL}


@background_scrape
async def get_artifact_server_history(opponent_server: str) -> dict | None:
    """아툴의 해당 서버 매칭 기록보기를 열어 회차별 기록을 가져옵니다."""
    if DUMMY_MODE:
        return None

    async with browser_page() as page:
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
                if "브리트라" in text.replace("브리 트라", "브리트라") and opponent_server in text:
                    if target_text_length is None or len(text) < target_text_length:
                        target = candidate
                        target_text_length = len(text)
                ancestor = ancestor.locator("..")

        if target is None:
            return None
        await target.click()
        await page.locator("table tr").filter(has_text=re.compile(r"\d{4}-\d{2}-\d{2}")).first.wait_for(timeout=10000)

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


def parse_breitra_artifact_result(body_text: str) -> dict | None:
    """렌더링된 아툴 결과에서 브리트라가 포함된 최신 전적을 추출합니다."""
    normalized_text = body_text.replace("브리 트라", "브리트라")
    server_pattern = r"([가-힣A-Za-z0-9]+)\s+(WIN|LOSE|DRAW)\s+(\d+)\s+(\d+차전)\s+⚔️\s+(\d+)\s+([가-힣A-Za-z0-9]+)\s+(WIN|LOSE|DRAW)\s+누적\s+(\d+):(\d+)"
    match = next(
        (
            match
            for match in re.finditer(server_pattern, normalized_text)
            if "브리트라" in {match.group(1), match.group(6)}
        ),
        None,
    )
    if match is None:
        return None

    (
        left_server,
        left_result,
        left_round,
        round_name,
        right_round,
        right_server,
        right_result,
        left_total,
        right_total,
    ) = match.groups()
    if left_server == "브리트라":
        breitra_result, opponent_result = left_result, right_result
        breitra_round, opponent_round = int(left_round), int(right_round)
        breitra_total, opponent_total = int(left_total), int(right_total)
        opponent_server = right_server
    else:
        breitra_result, opponent_result = right_result, left_result
        breitra_round, opponent_round = int(right_round), int(left_round)
        breitra_total, opponent_total = int(right_total), int(left_total)
        opponent_server = left_server

    return {
        "opponent_server": opponent_server,
        "left_server": left_server,
        "right_server": right_server,
        "breitra_result": breitra_result,
        "opponent_result": opponent_result,
        "breitra_round": breitra_round,
        "opponent_round": opponent_round,
        "round": round_name,
        "breitra_total": breitra_total,
        "opponent_total": opponent_total,
    }


async def get_breitra_territory_results(page, left_server: str, right_server: str) -> list[dict]:
    """브리트라 전적 카드에서 종족 아이콘으로 지역별 점령 서버를 추출합니다."""
    rows = await page.evaluate(
        """([leftServer, rightServer]) => {
            const normalize = value => (value || '').replace(/\\s+/g, '');
            return Array.from(document.querySelectorAll('.artifact-layers-row'))
                .flatMap(row => {
                    let card = row;
                    for (let index = 0; index < 10 && card; index += 1, card = card.parentElement) {
                        const text = normalize(card.innerText);
                        if (text.includes(normalize(leftServer)) &&
                            text.includes(normalize(rightServer)) &&
                            text.includes('결과집계완료')) {
                            const layer = row.querySelector('.artifact-layer-label');
                            return [{
                                layer: (layer?.innerText || '').trim(),
                                items: Array.from(row.querySelectorAll('.artifact-icon-item')).map(item => {
                                    const image = item.querySelector('img');
                                    const name = item.querySelector('.artifact-name');
                                    return {
                                        race: image?.alt || '',
                                        name: (name?.getAttribute('title') || name?.innerText || '').trim()
                                    };
                                })
                            }];
                        }
                    }
                    return [];
                });
        }""",
        [left_server, right_server],
    )
    race_to_server = {"천족": left_server, "마족": right_server}
    results = []
    for row in rows:
        for item in row.get("items", []):
            server = race_to_server.get(item.get("race"))
            if server and item.get("name"):
                results.append(
                    {"layer": row.get("layer", ""), "name": item["name"], "server": server}
                )
    return results


MAX_COMMENT_PAGES = 30


def contains_code(text: str, code: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", text))


async def goto_official(page, url):
    if not is_official_url(url):
        raise ScrapeUnavailable("Invalid official URL")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if (response and response.status >= 400) or not is_official_url(page.url):
        raise ScrapeUnavailable("Official page unavailable or redirected")


async def advance_comments(page, previous_text):
    # Only click comment controls, never unrelated board navigation or submit buttons.
    candidates = page.locator('[class*="comment"] button, [class*="comment"] a').filter(
        has_text=re.compile(r"^(?:(?:이전\s*)?댓글\s*보기|(?:댓글\s*)?더\s*보기|다음(?:\s*페이지)?)$")
    )
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if not await candidate.is_visible() or not await candidate.is_enabled():
            continue
        if await candidate.get_attribute("aria-disabled") == "true":
            continue
        await candidate.click()
        try:
            await page.wait_for_function(
                "previous => Array.from(document.querySelectorAll('div.comment-article'))"
                ".map(e => e.innerText).join('\\n') !== previous",
                arg=previous_text, timeout=10000,
            )
        except Exception as error:
            raise ScrapeUnavailable("Comment pagination did not load") from error
        await page.locator("div.comment-article").first.wait_for(timeout=10000)
        return True
    return False


async def find_comment_by_code(article_url: str, code: str):
    """Read bounded comment pages; source failures are distinct from a missing code."""
    if not code:
        return None
    if DUMMY_MODE:
        if code.startswith("TEST"):
            return {"nickname": "더미테스트유저", "profile_url": f"{BASE_URL}/ko-kr/profile/character/0000/dummy"}
        return None
    async with browser_page() as page:
        await goto_official(page, article_url)
        try:
            await page.locator("div.comment-article").first.wait_for(timeout=15000)
        except Exception as error:
            raise ScrapeUnavailable("Comments did not load") from error
        seen_pages = set()
        for _ in range(MAX_COMMENT_PAGES):
            rows = await page.locator("div.comment-article").evaluate_all(
                """els => els.map(e => ({
                    text: e.innerText,
                    content: e.querySelector('div.comment-contents')?.innerText || '',
                    nickname: e.querySelector('div.writer a.name')?.innerText || '',
                    href: e.querySelector('div.writer a.name')?.getAttribute('href')
                }))"""
            )
            signature = "\n".join(row["text"] for row in rows)
            if not rows or signature in seen_pages:
                raise ScrapeUnavailable("Comment pagination repeated or returned no content")
            seen_pages.add(signature)
            for row in rows:
                if not contains_code(row["content"], code):
                    continue
                profile_url = urljoin(BASE_URL, row["href"] or "")
                if not row["href"] or not row["nickname"] or not is_official_url(profile_url):
                    raise ScrapeUnavailable("Comment author profile unavailable")
                return {"nickname": row["nickname"].strip(), "profile_url": profile_url}
            if not await advance_comments(page, signature):
                return None
        raise ScrapeUnavailable("Comment scan limit reached; retry with a recent comment")


async def get_character_info(profile_url: str):
    """
    댓글 작성자의 프로필 페이지(profile_url)를 렌더링해서
    닉네임/서버/종족/레기온을 반환합니다.

    찾으면: {"nickname": "...", "server": "...", "race": "...", "legion": "...", "power_level": 450}
    못 찾으면: None
    """
    if DUMMY_MODE:
        return {
            "nickname": "더미테스트유저",
            "server": "브리트라",
            "race": "마족",
            "legion": "더미레기온",
            "power_level": 450,
        }

    if not is_official_url(profile_url):
        raise ScrapeUnavailable("Invalid official profile URL")
    async with browser_page() as page:
        parsed_url = urlsplit(profile_url)
        detail_path = parsed_url.path.replace("/profile/character/", "/characters/", 1)
        detail_path = detail_path.split("/board/", 1)[0]
        detail_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, detail_path, parsed_url.query, "")
        )

        await goto_official(page, profile_url)
        try:
            await page.locator(".classcard").wait_for(timeout=15000)
        except Exception:
            pass
        class_el = await page.query_selector(".classcard")
        class_name = (await class_el.inner_text()).strip() if class_el else None

        await goto_official(page, detail_url)

        desc = page.locator(".profile__info-desc")
        try:
            await desc.wait_for(timeout=10000)
        except Exception:
            raise ScrapeUnavailable("Character profile did not load")

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

        power_level_el = await page.query_selector(".profile__info-power-level")
        power_level = None
        if power_level_el:
            power_level_text = (await power_level_el.inner_text()).strip()
            power_level_match = re.search(r"\d[\d,]*", power_level_text)
            if power_level_match:
                power_level = int(power_level_match.group().replace(",", ""))

        if not server:
            return None

        return {
            "nickname": nickname,
            "class_name": class_name or "알 수 없음",
            "server": server,
            "race": race,
            "legion": legion or "없음",
            "power_level": power_level,
        }
