"""
Runs inside GitHub Actions. Reads config from environment variables
(set by the workflow_dispatch inputs), runs the Playwright scraper,
then POSTs both resulting CSVs (with-website + no-website) to the
n8n webhook URL.
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
        with_web_file = asyncio.run(scraper.main())
    except Exception as e:
        if callback_url:
            requests.post(callback_url, data={"status": "error", "reason": str(e)}, timeout=30)
        raise

    if not callback_url:
        return

    # main() returns the With_Website path (or None on failure). The
    # No_Website file sits alongside it in the same clean_data folder —
    # derive its path the same way preprocess() names it.
    no_web_file = None
    if with_web_file:
        clean_dir = os.path.dirname(with_web_file)
        no_web_file = os.path.join(clean_dir, f"{scraper.STATE_ABBR}_{scraper.INDUSTRY}_No_Website.csv")

    if with_web_file and os.path.exists(with_web_file):
        files = {"file": (os.path.basename(with_web_file), open(with_web_file, "rb"), "text/csv")}
        if no_web_file and os.path.exists(no_web_file):
            files["file_no_website"] = (os.path.basename(no_web_file), open(no_web_file, "rb"), "text/csv")

        try:
            requests.post(
                callback_url,
                files=files,
                data={"status": "success"},
                timeout=120,
            )
        finally:
            for f in files.values():
                try: f[1].close()
                except: pass
    else:
        requests.post(
            callback_url,
            data={"status": "failed", "reason": "no output file produced"},
            timeout=30,
        )


if __name__ == "__main__":
    main()
