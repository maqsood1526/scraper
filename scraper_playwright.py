import asyncio
import csv
import random
import glob
import os
import re
import pandas as pd
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

# ═══════════ CONFIG ═══════════
STATE_ABBR    = "FL"
INDUSTRY      = "Plumbing"
OUTPUT_FOLDER = "FL plumbing 1st"

CITIES = [
    "Miami"
]
# ══════════════════════════════

# ═══════════════════════════════════════════════
#  PERFORMANCE CONFIG
# ═══════════════════════════════════════════════
MAX_CONCURRENT_SITES = 20   # 16GB RAM safe
PAGE_TIMEOUT_MS       = 12000
FOOTER_WAIT_MS        = 3000  # wait for footer JS to render

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"

# ═══════════════════════════════════════════════
#  FIXED PAGES TO CHECK — in priority order
#  We ONLY visit these pages. Nothing else.
#  Homepage first (footer/header), then contact
#  pages, then privacy/terms (footer emails).
# ═══════════════════════════════════════════════
PRIORITY_PATHS = [
    "",                    # Homepage — footer + header
    "/contact-us",
    "/contact",
    "/about-us",
    "/about",
    "/privacy-policy",
    "/terms-and-conditions",
    "/terms",
    "/contact.html",
    "/about.html",
    "/privacy.html",
]

# ═══════════════════════════════════════════════
#  JUNK FILTERS
# ═══════════════════════════════════════════════
ALWAYS_JUNK_DOMAINS = {
    "google.com", "example.com", "test.com",
    "mailservice.com", "sentry.io", "wixpress.com",
    "squarespace.com", "wpengine.com",
}
LEGIT_TLDS = {
    "com","org","net","edu","gov","biz","info","co","us","ca",
    "uk","de","io","ai","app","dev","tech","store","online","site",
    "blog","me","tv","cc","ch","se","no","au","nz"
}

# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════
def sanitize(name):
    return name.replace(" ", "_").replace("/", "-")

def format_phone(raw):
    if not raw: return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1 {digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return raw.strip()

def clean_address(raw):
    if not raw: return ""
    text = raw.replace("Â·", "·").replace("Â", "").replace("\u00b7", "|")
    parts = re.split(r"[·|\n]", text)
    address_keywords = [
        r"\d+\s+\w", "St", "Ave", "Rd", "Blvd", "Dr", "Ln",
        "Way", "Ct", "Pl", "Hwy", "Pkwy", "Suite", "#", "Ste"
    ]
    junk_patterns = [
        r"open\s*\d", r"closed", r"24\s*hours",
        r"^\+?1?\s*[\(\d][\d\s\-\(\)]{8,}$",
        r"^(hvac|plumb|electr|roof|landscap|contractor|service)"
    ]
    best = ""
    for part in parts:
        part = part.strip()
        if not part or len(part) < 5: continue
        if any(re.search(p, part, re.IGNORECASE) for p in junk_patterns):
            continue
        if bool(re.search(r"\d", part)) and any(kw.lower() in part.lower() for kw in address_keywords):
            best = part
            break
    return best.strip()

# ═══════════════════════════════════════════════
#  EMAIL HELPERS
# ═══════════════════════════════════════════════
EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"
)

def is_real_tld(domain: str) -> bool:
    parts = domain.rsplit(".", 1)
    return len(parts) >= 2 and parts[1].lower() in LEGIT_TLDS

def clean_email(email: str) -> str:
    email = email.strip().rstrip(".,;:'\"")
    return email if "@" in email and "." in email else ""

def is_valid_email(email: str) -> bool:
    if re.search(r"\.(png|jpg|jpeg|gif|css|js|woff|svg|ico|webp|pdf|xml)$", email, re.IGNORECASE):
        return False
    if re.search(r"@\d+x", email): return False
    if re.search(r"(logo|img|banner|icon|avatar)", email, re.IGNORECASE): return False
    parts = email.split("@")
    if len(parts) != 2: return False
    local, domain = parts
    if domain.lower() in ALWAYS_JUNK_DOMAINS: return False
    if any(w in domain.lower() for w in ["thenumberprovided","example","test","fake","noreply"]):
        return False
    if not is_real_tld(domain): return False
    if " " in email: return False
    junk_locals = ["noreply","no-reply","donotreply","do-not-reply",
                   "webmaster","postmaster","mailer-daemon","press"]
    if local.lower() in junk_locals: return False
    return True

def score_email(email: str, website_domain: str) -> int:
    score = 0
    try: local, domain = email.split("@")
    except: return 0
    if domain.lower() == website_domain.lower(): score += 20
    elif website_domain.endswith("." + domain) or domain.endswith("." + website_domain): score += 10
    for pref in ["info","sales","contact","service","support","admin","hello","office","mail","accounts","help"]:
        if local.lower().startswith(pref): score += 8; break
    if any(w in local.lower() for w in ["dev","test","noreply","no-reply","spam","fake","example","press"]):
        score -= 15
    if len(local) > 30: score -= 5
    if domain.lower() in {"gmail.com","yahoo.com","hotmail.com","outlook.com"}: score -= 3
    return score

def select_best_email(emails: set, website_url: str) -> str:
    if not emails: return ""
    domain = urlparse(website_url).netloc.replace("www.", "")
    scored = [(score_email(e, domain), e) for e in emails]
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1]

# ═══════════════════════════════════════════════
#  GOOGLE MAPS SCROLL
# ═══════════════════════════════════════════════
async def scroll_to_end(page):
    print("   Scrolling", end="", flush=True)
    prev = 0; stable_count = 0; max_stable = 6; extra_scrolls = 3
    while True:
        await page.evaluate("""
            () => {
                const f = document.querySelector('div[role="feed"]');
                if (f) { f.scrollTop = f.scrollHeight; f.scrollBy(0, 3000); }
            }
        """)
        await asyncio.sleep(1.2)
        cur = await page.locator('div[role="feed"] > div > div[jsaction]').count()
        print(f".{cur}", end="", flush=True)
        html = await page.content()
        if "You've reached the end" in html or "end of the list" in html:
            for _ in range(2):
                await page.evaluate("() => { const f = document.querySelector('div[role=\"feed\"]'); if (f) f.scrollTop = f.scrollHeight; }")
                await asyncio.sleep(1.0)
            cur = await page.locator('div[role="feed"] > div > div[jsaction]').count()
            print(f" ✅ End reached! ({cur} results)"); return cur
        if cur == prev:
            stable_count += 1
            if stable_count >= max_stable:
                print("~", end="", flush=True)
                for _ in range(extra_scrolls):
                    await page.evaluate("() => { const f = document.querySelector('div[role=\"feed\"]'); if (f) f.scrollTop = f.scrollHeight; }")
                    await asyncio.sleep(1.5)
                final = await page.locator('div[role="feed"] > div > div[jsaction]').count()
                if final > cur:
                    prev = final; stable_count = 0
                    print(f"+{final}", end="", flush=True); continue
                print(f" ✅ Stable! ({final} results)"); return final
        else:
            stable_count = 0; prev = cur

# ═══════════════════════════════════════════════
#  GOOGLE MAPS DATA EXTRACT
# ═══════════════════════════════════════════════
async def extract_all_at_once(page, city):
    data = await page.evaluate("""
        () => {
            const phoneRegex = /(\\(?\\d{3}\\)?[\\s\\-\\.]?\\d{3}[\\s\\-\\.]?\\d{4})/;
            const addressKeywords = [' St',' St,',' Ave',' Ave,',' Rd',' Rd,',' Blvd',' Dr',' Dr,',' Ln',' Ln,',' Way',' Ct',' Ct,',' Pl',' Pl,',' Hwy',' Pkwy',' Suite',' #',' Ste'];
            function looksLikeAddress(txt) { if (!txt || txt.length < 5) return false; return /\\d/.test(txt) && addressKeywords.some(kw => txt.includes(kw)); }
            function looksLikePhone(txt) { if (!txt) return false; const m = txt.match(phoneRegex); return m ? m[0] : null; }
            function isJunk(txt) { if (!txt) return true; const lower = txt.toLowerCase(); return /open|closed|hours|^\\d+(\\.\\d)?\\s*\\(/.test(lower) || txt.length < 5; }
            const results = [];
            document.querySelectorAll('div[role="feed"] > div > div[jsaction]').forEach(card => {
                const row = { name:'', category:'', rating:'', reviews:'', address:'', phone:'', website:'', maps_url:'' };
                const nameEl = card.querySelector('.qBF1Pd'); if (nameEl) row.name = nameEl.innerText.trim(); if (!row.name) return;
                const catEl = card.querySelector('.W4Efsd .W4Efsd span'); if (catEl) row.category = catEl.innerText.trim();
                const ratingEl = card.querySelector('.MW4etd'); if (ratingEl) row.rating = ratingEl.innerText.trim();
                const revEl = card.querySelector('.UY7F9'); if (revEl) row.reviews = revEl.innerText.replace(/[()]/g,'').trim();
                const webEl = card.querySelector('a[data-value="Website"]'); if (webEl) row.website = webEl.href || '';
                const mapsEl = card.querySelector('a.hfpxzc'); if (mapsEl) row.maps_url = mapsEl.href || '';
                card.querySelectorAll('[aria-label]').forEach(el => {
                    const label = (el.getAttribute('aria-label') || '').trim();
                    if (/^Address[:\\s]/i.test(label) && !row.address) { const addr = label.replace(/^Address[:\\s]*/i,'').trim(); if (!isJunk(addr)) row.address = addr; }
                    if (/^Phone[:\\s]/i.test(label) && !row.phone) row.phone = label.replace(/^Phone[:\\s]*/i,'').trim();
                });
                if (!row.address) { const addrEl = card.querySelector('[data-item-id="address"]'); if (addrEl) { const txt = addrEl.getAttribute('aria-label') || addrEl.innerText || ''; const clean = txt.replace(/^Address[:\\s]*/i,'').trim(); if (looksLikeAddress(clean)) row.address = clean; } }
                if (!row.phone) { const telEl = card.querySelector('a[href^="tel:"]'); if (telEl) row.phone = telEl.href.replace('tel:','').trim(); }
                if (!row.address) { const spans = card.querySelectorAll('.W4Efsd > span, .W4Efsd > div'); for (const span of spans) { const txt = span.innerText.split('\\n')[0].trim(); if (looksLikeAddress(txt) && !isJunk(txt)) { row.address = txt; break; } } }
                if (!row.phone) { const spans = card.querySelectorAll('.W4Efsd span, .W4Efsd div'); for (const span of spans) { const txt = span.innerText.split('\\n')[0].trim(); const ph = looksLikePhone(txt); if (ph && txt.replace(/[\\d\\s\\-\\(\\)\\+]/g,'').length < 3) { row.phone = ph; break; } } }
                results.push(row);
            });
            return results;
        }
    """)
    print(f"   ✅ {len(data)} records extracted!")
    rows = []
    for r in data:
        if not r.get("name"): continue
        rows.append({
            "Name": r["name"], "Category": r["category"],
            "Rating": r["rating"], "Reviews": r["reviews"],
            "Address": clean_address(r.get("address", "")),
            "Phone": format_phone(r.get("phone", "")),
            "Email": "", "Website": r["website"], "Maps_URL": r["maps_url"],
        })
    return rows

# ═══════════════════════════════════════════════
#  EXTRACT EMAILS FROM ONE PAGE
# ═══════════════════════════════════════════════
async def extract_emails_from_page(page, is_homepage: bool = False) -> set:
    found = set()
    try:
        if is_homepage:
            try:
                await page.wait_for_selector(
                    "footer, #footer, .footer, [class*='footer'], [id*='footer']",
                    timeout=FOOTER_WAIT_MS,
                    state="attached"
                )
                await asyncio.sleep(1.2)
            except:
                await asyncio.sleep(0.8)
        else:
            await asyncio.sleep(0.8)

        # ── 1. mailto: links ──
        mailtos = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href^=\"mailto:\"]'))"
            ".map(a => a.getAttribute('href'))"
        )
        for m in mailtos:
            email = m.replace("mailto:", "").split("?")[0].strip()
            if is_valid_email(email):
                found.add(clean_email(email))

        # ── 2. header section ──
        try:
            header_text = await page.evaluate("""
                () => {
                    const h = document.querySelector(
                        'header, #header, .header, [class*="header"], [id*="header"], nav, .navbar'
                    );
                    return h ? h.innerText : '';
                }
            """)
            for e in EMAIL_REGEX.findall(header_text):
                if is_valid_email(e): found.add(clean_email(e))
        except: pass

        # ── 3. footer section ──
        try:
            footer_text = await page.evaluate("""
                () => {
                    const f = document.querySelector(
                        'footer, #footer, .footer, [class*="footer"], [id*="footer"]'
                    );
                    return f ? f.innerText : '';
                }
            """)
            for e in EMAIL_REGEX.findall(footer_text):
                if is_valid_email(e): found.add(clean_email(e))
        except: pass

        # ── 4. full page body scan ──
        body_text = await page.inner_text("body")
        html = await page.content()
        clean_html = re.sub(
            r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>",
            "", html, flags=re.IGNORECASE
        )
        for e in EMAIL_REGEX.findall(body_text + " " + clean_html):
            if is_valid_email(e): found.add(clean_email(e))

        # ── 5. obfuscated patterns ──
        decoded = (body_text
                   .replace("[at]", "@").replace("[dot]", ".")
                   .replace(" at ", "@").replace(" dot ", "."))
        for e in EMAIL_REGEX.findall(decoded):
            if is_valid_email(e): found.add(clean_email(e))

    except: pass
    return found

# ═══════════════════════════════════════════════
#  FACEBOOK HANDLER
# ═══════════════════════════════════════════════
async def extract_email_facebook(context, url: str) -> str:
    page = await context.new_page()
    try:
        await page.set_extra_http_headers({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        about_url = url.rstrip("/") + "/about"
        await page.goto(about_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        body = await page.inner_text("body")
        emails = {clean_email(e) for e in EMAIL_REGEX.findall(body) if is_valid_email(e)}
        if not emails:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)
            body = await page.inner_text("body")
            emails = {clean_email(e) for e in EMAIL_REGEX.findall(body) if is_valid_email(e)}
        return select_best_email(emails, url) if emails else ""
    except: return ""
    finally:
        try: await page.close()
        except: pass

# ═══════════════════════════════════════════════
#  CORE CRAWLER
# ═══════════════════════════════════════════════
async def crawl_site_for_email(context, start_url: str) -> str:
    if not start_url or not start_url.startswith(("http://", "https://")):
        return ""

    if "facebook.com" in urlparse(start_url).netloc:
        return await extract_email_facebook(context, start_url)

    base_url     = start_url.rstrip("/")
    visited: set = set()
    all_emails: set = set()

    FREE_PROVIDERS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "mail.com"}

    def has_business_email(emails: set) -> bool:
        site_domain = urlparse(start_url).netloc.replace("www.", "")
        for e in emails:
            domain = e.split("@")[-1].lower()
            if domain not in FREE_PROVIDERS and domain == site_domain:
                return True
        return False

    async def visit_page(url: str, is_homepage: bool = False) -> set:
        if url in visited: return set()
        visited.add(url)
        page = await context.new_page()
        try:
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,ico,mp4,mp3}",
                lambda r: r.abort()
            )
            await page.set_extra_http_headers({
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            return await extract_emails_from_page(page, is_homepage=is_homepage)
        except:
            return set()
        finally:
            try: await page.close()
            except: pass

    for i, path in enumerate(PRIORITY_PATHS):
        url = urljoin(base_url + "/", path.lstrip("/")) if path else start_url
        is_hp = (i == 0)

        emails = await visit_page(url, is_homepage=is_hp)
        all_emails.update(emails)

        if has_business_email(all_emails):
            break

    return select_best_email(all_emails, start_url) if all_emails else ""

# ═══════════════════════════════════════════════
#  EMAIL PIPELINE — 20 concurrent sites
# ═══════════════════════════════════════════════
async def add_emails_to_results(results, context):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SITES)
    total = sum(1 for r in results if r.get("Website"))
    done = 0

    async def process_one(row):
        nonlocal done
        if not row.get("Website"): return
        async with semaphore:
            row["Email"] = await crawl_site_for_email(context, row["Website"])
        done += 1
        if done % 10 == 0 or done == total:
            found = sum(1 for r in results if r.get("Email"))
            print(f"   📧 Progress: {done}/{total} | Emails: {found}")

    await asyncio.gather(*[process_one(r) for r in results if r.get("Website")])

# ═══════════════════════════════════════════════
#  STEP 1 — PER-CITY SCRAPER (no email hunting)
# ═══════════════════════════════════════════════
async def scrape_city(city, browser, out_dir):
    query    = f"{INDUSTRY} in {city}, {STATE_ABBR}"
    out_file = out_dir / f"{sanitize(city)}_{STATE_ABBR}.csv"
    print(f"\n🔍 Searching: {query}")

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    page = await context.new_page()
    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,ico,mp4,mp3}",
        lambda r: r.abort()
    )

    results = []
    try:
        url = "https://www.google.com/maps/search/" + query.replace(" ", "+")
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        try:
            btn = page.locator('button:has-text("Accept all")').first
            if await btn.is_visible(timeout=2000): await btn.click(); await asyncio.sleep(0.5)
        except: pass
        try:
            await page.wait_for_selector('div[role="feed"]', timeout=10000)
        except:
            print("   ⚠️ No results — skipping"); await context.close(); return 0

        await scroll_to_end(page)
        results = await extract_all_at_once(page, city)

        # ✅ No email scraping here — handled separately after preprocessing

    except Exception as e:
        print(f"   ❌ Error: {e}")
    finally:
        await context.close()

    if results:
        fields = ["Name","Category","Rating","Reviews","Address","Phone","Email","Website","Maps_URL"]
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(results)
        print(f"   💾 Saved: {len(results)} records → {out_file.name}")
    else:
        print("   ⚠️ No data.")
    return len(results)

# ═══════════════════════════════════════════════
#  STEP 2 — PREPROCESSING & CLEANUP
# ═══════════════════════════════════════════════
def preprocess(folder_path, state, industry):
    print("\n" + "="*55)
    print("  🔄 PREPROCESSING...")
    print("="*55)
    all_files = glob.glob(os.path.join(folder_path, "*.csv"))
    if not all_files: print("  ⚠️ No CSV files found."); return None
    df_list = []
    for f in all_files:
        try:
            df = pd.read_csv(f, encoding="latin1")
            if "City" not in df.columns:
                df["City"] = os.path.basename(f).replace(f"_{state}.csv","").replace("_"," ")
            df_list.append(df)
        except Exception as e: print(f"  ⚠️ Skipped {f}: {e}")
    merged = pd.concat(df_list, ignore_index=True)
    print(f"  📊 Total rows (raw): {len(merged)}")
    cols = ["Name","Category","City","Rating","Reviews","Address","Phone","Email","Website","Maps_URL"]
    merged = merged[[c for c in cols if c in merged.columns]]
    merged = merged.loc[:, ~merged.columns.str.contains("^Unnamed")].dropna(how="all")
    for col in merged.columns:
        merged[col] = merged[col].fillna("").astype(str).str.strip()
    if "Phone"   in merged.columns: merged["Phone"]   = merged["Phone"].apply(format_phone)
    if "Address" in merged.columns: merged["Address"] = merged["Address"].apply(clean_address)
    name_cities = merged.groupby("Name")["City"].apply(
        lambda x: ", ".join(sorted(set(c.strip() for c in x if c.strip())))
    ).to_dict()
    name_count = merged.groupby("Name")["Name"].count().to_dict()
    merged["Times_Found"]     = merged["Name"].map(lambda n: name_count.get(n, 1))
    merged["Found_In_Cities"] = merged["Name"].map(lambda n: name_cities.get(n, ""))
    total_before = len(merged)
    has_phone = merged[merged["Phone"] != ""].copy()
    no_phone  = merged[merged["Phone"] == ""].copy()
    has_phone = has_phone.drop_duplicates(subset=["Name","Phone"], keep="first")
    merged = pd.concat([has_phone, no_phone], ignore_index=True).sort_values(["City","Name"]).reset_index(drop=True)
    print(f"  🔁 Duplicates removed : {total_before - len(merged)}")
    print(f"  ✅ Clean rows         : {len(merged)}")
    no_web   = merged[merged["Website"] == ""].copy()
    with_web = merged[merged["Website"] != ""].copy()
    print(f"  🌐 With Website : {len(with_web)}")
    print(f"  📵 No Website   : {len(no_web)}")
    clean_dir = os.path.join(folder_path, "clean_data")
    os.makedirs(clean_dir, exist_ok=True)
    with_web_file = os.path.join(clean_dir, f"{state}_{industry}_With_Website.csv")
    no_web_file   = os.path.join(clean_dir, f"{state}_{industry}_No_Website.csv")
    with_web.to_csv(with_web_file, index=False, encoding="utf-8-sig")
    no_web.to_csv(no_web_file,     index=False, encoding="utf-8-sig")
    print(f"\n  ✅ Saved to  : {clean_dir}")
    print(f"  📄 {os.path.basename(with_web_file)} ({len(with_web)} rows)")
    print(f"  📄 {os.path.basename(no_web_file)} ({len(no_web)} rows)")
    return clean_dir

# ═══════════════════════════════════════════════
#  STEP 3 — EMAIL SCRAPING FROM CLEANED CSV
# ═══════════════════════════════════════════════
async def scrape_emails_from_csv(with_web_file: str):
    print("\n" + "="*55)
    print("  📧 EMAIL SCRAPING FROM CLEANED CSV")
    print("="*55)

    df = pd.read_csv(with_web_file, encoding="utf-8-sig")
    if "Email" not in df.columns:
        df["Email"] = ""
    df["Email"] = df["Email"].fillna("").astype(str)

    results = df.to_dict("records")
    total_with_web = sum(1 for r in results if str(r.get("Website", "")).strip())
    print(f"  🌐 Websites to crawl : {total_with_web}")
    print(f"  ⚙️  {MAX_CONCURRENT_SITES} concurrent | {len(PRIORITY_PATHS)} fixed pages per site")
    print(f"  🔎 Strategy: homepage(header+footer) → contact → about → privacy")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=0)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        await add_emails_to_results(results, context)
        await context.close()
        await browser.close()

    # Save results back to the same With_Website.csv
    df_out = pd.DataFrame(results)
    df_out.to_csv(with_web_file, index=False, encoding="utf-8-sig")

    found = sum(1 for r in results if str(r.get("Email", "")).strip())
    print(f"\n  ✅ Emails found : {found}/{total_with_web}")
    print(f"  💾 Updated file : {with_web_file}")

# ═══════════════════════════════════════════════
#  MAIN — Order: Maps → Preprocess → Emails
# ═══════════════════════════════════════════════
async def main():
    """Runs the full Maps -> Preprocess -> Emails pipeline.
    Returns the path to the final 'With_Website.csv' file, or None on failure.
    """
    out_dir = Path(OUTPUT_FOLDER)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("="*55)
    print(f"  Industry         : {INDUSTRY}")
    print(f"  State            : {STATE_ABBR}")
    print(f"  Cities           : {len(CITIES)}")
    print(f"  Concurrent sites : {MAX_CONCURRENT_SITES}")
    print(f"  Pages per site   : {len(PRIORITY_PATHS)} fixed pages only")
    print(f"  Strategy         : Maps → Preprocess → Emails")
    print(f"  Output           : {out_dir.resolve()}")
    print("="*55)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  STEP 1 — Google Maps scraping
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "="*55)
    print("  🗺️  STEP 1: GOOGLE MAPS SCRAPING")
    print("="*55)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=0)
        total = 0
        for i, city in enumerate(CITIES):
            count = await scrape_city(city, browser, out_dir)
            total += count
            if i < len(CITIES) - 1:
                wait = random.uniform(1.5, 3)
                print(f"  ⏳ Waiting {wait:.1f}s..."); await asyncio.sleep(wait)
        await browser.close()

    print(f"\n  ✅ Maps done! Total records: {total}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  STEP 2 — Preprocess & clean
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    clean_dir = preprocess(OUTPUT_FOLDER, STATE_ABBR, INDUSTRY)
    if not clean_dir:
        print("  ❌ Preprocessing failed — aborting."); return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  STEP 3 — Email scraping on cleaned file
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with_web_file = os.path.join(clean_dir, f"{STATE_ABBR}_{INDUSTRY}_With_Website.csv")
    if os.path.exists(with_web_file):
        await scrape_emails_from_csv(with_web_file)
    else:
        print(f"  ❌ File not found: {with_web_file}")
        return None

    print("\n" + "="*55)
    print("  🏁 ALL DONE!")
    print(f"  📁 Output folder: {Path(OUTPUT_FOLDER).resolve()}")
    print(f"  📄 Final file   : {with_web_file}")
    print("="*55)
    return with_web_file

if __name__ == "__main__":
    asyncio.run(main())
