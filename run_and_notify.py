"""
Runs inside GitHub Actions. Reads config from environment variables
(set by the workflow_dispatch inputs), runs the Playwright scraper,
then POSTs the resulting CSV to the n8n webhook URL.
"""
import os
import asyncio

import requests
import scraper_playwright as scraper


def main():
    scraper.STATE_ABBR = os.environ.get("STATE_ABBR", scraper.STATE_ABBR)
    scraper.INDUSTRY = os.environ.get("INDUSTRY", scraper.INDUSTRY)

    cities_raw = os.environ.get("CITIES", "")
    if cities_raw:
        scraper.CITIES = [c.strip() for c in cities_raw.split(",") if c.strip()]

    run_tag = f"{scraper.STATE_ABBR}_{scraper.INDUSTRY}_{'-'.join(scraper.CITIES)}".replace(" ", "_")
    scraper.OUTPUT_FOLDER = run_tag

    callback_url = os.environ.get("CALLBACK_URL", "")

    try:
        result_path = asyncio.run(scraper.main())
    except Exception as e:
        if callback_url:
            requests.post(callback_url, data={"status": "error", "reason": str(e)}, timeout=30)
        raise

    if not callback_url:
        return

    if result_path and os.path.exists(result_path):
        with open(result_path, "rb") as f:
            requests.post(
                callback_url,
                files={"file": (os.path.basename(result_path), f, "text/csv")},
                data={"status": "success"},
                timeout=120,
            )
    else:
        requests.post(
            callback_url,
            data={"status": "failed", "reason": "no output file produced"},
            timeout=30,
        )


if __name__ == "__main__":
    main()
