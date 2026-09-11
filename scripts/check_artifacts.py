"""Read-only snapshot of the public artifact source for parser diagnosis."""
import asyncio
import json
import re
from pathlib import Path
import sys

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto("https://aion2tool.com/server-comparison", wait_until="domcontentloaded", timeout=30000)
            try:
                await page.get_by_text(re.compile(r"^(?:과거\s*)?기록\s*보기$")).first.wait_for(timeout=15000)
            except Exception:
                pass
            data = {"url": page.url, "body": await page.locator("body").inner_text(), "controls": await page.locator("button,a").evaluate_all("els => els.map(e=>({tag:e.tagName,text:e.innerText,href:e.getAttribute('href')}))")}
            buttons = page.get_by_text(re.compile(r"^(?:과거\s*)?기록\s*보기$"))
            data["cards"] = []
            for index in range(await buttons.count()):
                ancestors = await buttons.nth(index).evaluate("e => {let r=[]; for(let i=0;e&&i<6;i++,e=e.parentElement) r.push({cls:e.className,text:e.innerText}); return r;}")
                data["cards"].append({"index": index, "ancestors": ancestors})
            if len(sys.argv) > 1:
                await buttons.nth(int(sys.argv[1])).click()
                await page.wait_for_timeout(1500)
                data["after_click"] = await page.locator("body").inner_text()
                data["tables"] = await page.locator("table tr").evaluate_all("rows => rows.map(row => ({cells:Array.from(row.cells).map(cell=>({text:cell.innerText, images:Array.from(cell.querySelectorAll('img')).map(i=>i.src)}))}))")
            destination = ROOT / ".artifact-source.json"
            destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"snapshot": str(destination), "buttons": await buttons.count(), "body_excerpt": data["body"][:100]}, ensure_ascii=True))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
