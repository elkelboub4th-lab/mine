import os
import time
import hashlib
import json
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
from groq import Groq

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")
NTFY_URL     = os.getenv("NTFY_URL", "https://ntfy.sh")
FB_COOKIES   = os.getenv("FB_COOKIES", "")

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("Missing environment variables. Please check your Render Environment tab.")
    exit(1)

supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3] });
Object.defineProperty(navigator, 'languages',  { get: () => ['fr-DZ', 'fr', 'en-US'] });
window.chrome = { runtime: {} };
"""

# ── Cookie format converter ───────────────────────────────────────────────────
# Cookie-Editor exports a slightly different format than Playwright expects.
# This converts it automatically so you can paste the raw export into FB_COOKIES.

SAMESITE_MAP = {
    "no_restriction": "None",
    "lax":            "Lax",
    "strict":         "Strict",
    "unspecified":    "None",
    None:             "None",
}

def convert_cookies(raw_cookies: list) -> list:
    converted = []
    for c in raw_cookies:
        cookie = {
            "name":     c["name"],
            "value":    c["value"],
            "domain":   c["domain"],
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": SAMESITE_MAP.get(c.get("sameSite"), "None"),
        }
        # expirationDate (float) → expires (int); skip if session cookie
        if c.get("expirationDate"):
            cookie["expires"] = int(c["expirationDate"])
        converted.append(cookie)
    return converted

# ── Notifications ─────────────────────────────────────────────────────────────

def send_ntfy_notification(title, body, priority="high", tags="iphone,money_bag"):
    try:
        requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
        ).raise_for_status()
        print(f"  ntfy sent: {title}")
    except Exception as e:
        print(f"  ntfy error: {e}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_id(url, title):
    return hashlib.md5(f"{title}_{url.split('?')[0]}".encode()).hexdigest()

def is_seen(external_id):
    try:
        return len(supabase.table("seen_listings").select("external_id").eq("external_id", external_id).execute().data) > 0
    except Exception as e:
        print(f"  is_seen error: {e}")
        return False

def mark_as_seen(external_id):
    try:
        supabase.table("seen_listings").insert({"external_id": external_id}).execute()
    except Exception as e:
        print(f"  mark_as_seen error: {e}")

# ── AI Classification ─────────────────────────────────────────────────────────

def classify_listing_with_ai(title, raw_price):
    prompt = f"""
You are an expert in the Algerian iPhone market.
Analyze this Facebook Marketplace listing:
Title: "{title}"
Price string: "{raw_price}"

1. Detect iPhone model (e.g. "iPhone 13 Pro Max"). Unknown → "Unknown".
2. Extract real price in DZD. "15 million" = 150000. Handle Darja-style shorthand.
3. Is the price fake? (e.g. 1111, 1234, 1 DZD) → is_fake_price: true.
4. Estimate current market price in DZD.
5. is_steal = true if (market_price - listing_price) > 20000 DZD.

Respond with JSON ONLY:
{{
    "model": "iPhone ...",
    "price_dzd": 150000,
    "is_fake_price": false,
    "estimated_market_price_dzd": 180000,
    "is_steal": true
}}
"""
    for model in GROQ_MODELS:
        try:
            result = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            return json.loads(result.choices[0].message.content)
        except Exception as e:
            print(f"  Groq [{model}] failed: {e} — trying next...")
    return None

# ── Listing Processing ────────────────────────────────────────────────────────

def process_listings(listings):
    print(f"Processing {len(listings)} listings...")
    for item in listings:
        ext_id = hash_id(item["url"], item["title"])
        if is_seen(ext_id):
            continue

        print(f"  Analyzing: {item['title']}")
        ai = classify_listing_with_ai(item["title"], item["price"])

        if not ai or ai.get("is_fake_price"):
            mark_as_seen(ext_id)
            continue

        is_steal     = ai.get("is_steal", False)
        parsed_price = ai.get("price_dzd", 0)

        if is_steal:
            print("  🔥 STEAL DETECTED")
            send_ntfy_notification(
                title=f"🚨 STEAL: {ai.get('model')}",
                body=(
                    f"Price: {parsed_price:,} DZD\n"
                    f"Market: {ai.get('estimated_market_price_dzd'):,} DZD\n"
                    f"Link: {item['url']}"
                ),
            )

        try:
            supabase.table("listings").insert({
                "external_id": ext_id,
                "title":       item["title"],
                "price":       parsed_price,
                "url":         item["url"],
                "category":    ai.get("model", "Unknown"),
                "is_steal":    is_steal,
                "metadata":    ai,
            }).execute()
            mark_as_seen(ext_id)
        except Exception as e:
            print(f"  DB error: {e}")

# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_facebook():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="fr-DZ",
        )
        context.add_init_script(STEALTH_JS)

        # Load and convert FB cookies
        if FB_COOKIES:
            try:
                raw  = json.loads(FB_COOKIES)
                converted = convert_cookies(raw)
                context.add_cookies(converted)
                print(f"FB cookies loaded ✓ ({len(converted)} cookies)")
            except Exception as e:
                print(f"Cookie error: {e}")
        else:
            print("⚠️  No FB_COOKIES set — will likely hit login wall.")

        page = context.new_page()
        url  = "https://www.facebook.com/marketplace/algiers/search/?query=iphone"
        print(f"Scraping {url} ...")

        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("domcontentloaded")

            print(f"Page title: {page.title()}")
            print(f"Page URL:   {page.url}")

            # Bail early if redirected to login
            if "/login" in page.url:
                print("❌ Redirected to login — cookies may be expired. Re-export and update FB_COOKIES.")
                return

            # Try to dismiss cookie consent banner
            for selector in [
                'button[data-cookiebanner="accept_button"]',
                'button[title="Allow all cookies"]',
                'button:has-text("Accept All")',
                'button:has-text("Allow")',
                'button:has-text("Tout accepter")',
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        print(f"Dismissed cookie banner ({selector})")
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # Wait for listings
            try:
                page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=30000)
            except Exception:
                print("❌ No listings found. Page snippet:")
                print(page.content()[:500])
                return

            listings = []
            for link in page.query_selector_all('a[href*="/marketplace/item/"]'):
                href = link.get_attribute("href")
                if not href:
                    continue
                full_url = f"https://www.facebook.com{href}" if href.startswith("/") else href
                lines = [l.strip() for l in link.inner_text().split("\n") if l.strip()]
                if len(lines) >= 2:
                    listings.append({"price": lines[0], "title": lines[1], "url": full_url})

            print(f"Found {len(listings)} raw listings.")
            process_listings(listings)

        except Exception as e:
            print(f"Scrape error: {e}")
        finally:
            browser.close()

# ── Health-check server ───────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SwoopDZ Active")
    def log_message(self, *args):
        pass

def run_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), HealthHandler).serve_forever()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    print("SwoopDZ Scraper v2.6 Starting...")
    while True:
        scrape_facebook()
        print("Sleeping 60s...")
        time.sleep(60)
