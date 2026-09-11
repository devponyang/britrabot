"""Read-only validation of the production artifact scraper; never connects to Discord."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aion2_scraper as scraper


async def main():
    try:
        result = await scraper.get_latest_artifact_result()
        if not result:
            raise RuntimeError("No completed current matchup found")
        history = await scraper.get_artifact_server_history(result["record"]["opponent_server"])
        print(json.dumps({"latest": result, "history": history}, ensure_ascii=True))
        if not history or not history["records"]:
            raise RuntimeError("History is empty")
    finally:
        await scraper.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
