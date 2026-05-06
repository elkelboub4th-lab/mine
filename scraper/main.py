import os
import sys
import time
import re
import hashlib
import json
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# ── Fix emoji output on Windows (cp1252 → utf-8) ─────────────────────────────
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Load .env BEFORE importing clients that need env vars
load_dotenv()

from supabase import create_client, Client
from playwright.sync_api import sync_playwright
from groq import Groq

# ── Configuration ─────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("❌ Missing env vars! Check your .env file.", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# ── Notifications & DB ────────────────────────────────────────────────────────

def send_ntfy(title, body):
    try:
        # HTTP headers are latin-1, so strip emoji from title; put emoji in body
        safe_title = title.encode("ascii", errors="ignore").decode("ascii").strip()
        if not safe_title:
            safe_title = "SwoopDZ Deal Alert"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"DEAL ALERT\n{body}".encode("utf-8"),
            headers={
                "Title": safe_title,
                "Tags": "rotating_light,iphone",
                "Priority": "high",
            },
            timeout=10
        )
        print(f"📲 Notification sent: {safe_title}", flush=True)
    except Exception as e:
        print(f"⚠️  ntfy error: {e}", flush=True)

def is_seen(ext_id: str) -> bool:
    try:
        res = supabase.table("seen_listings").select("external_id").eq("external_id", ext_id).execute()
        return len(res.data) > 0
    except Exception as e:
        print(f"⚠️  Supabase seen check error: {e}", flush=True)
        return False

# ── Native Stealth Scraper ────────────────────────────────────────────────────

def get_listings_stealth(page_num: int = 1):
    url = f"https://www.ouedkniss.com/s/{page_num}?keywords=iphone"
    print(f"🌐 Launching browser for page {page_num}...", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        import random
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        context = browser.new_context(
            user_agent=random.choice(user_agents),
            locale="fr-DZ",
            timezone_id="Africa/Algiers",
            viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()

        # Log all failing requests or GraphQL requests to debug Render API blocks
        page.on("response", lambda r: print(f"🔍 Network: {r.status} {r.url}", flush=True) if "graphql" in r.url or r.status >= 400 else None)

        try:
            # Inject opts object expected by stealth.js to avoid ReferenceError
            opts_script = """
            const opts = {
                script_logging: false,
                navigator_languages_override: ["fr-DZ", "fr"],
                navigator_platform: "Win32",
                navigator_user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                navigator_vendor: "Google Inc.",
                webgl_vendor: "Intel Inc.",
                webgl_renderer: "Intel Iris OpenGL Engine"
            };
            """
            stealth_path = os.path.join(os.path.dirname(__file__), "stealth.js")
            if os.path.exists(stealth_path):
                with open(stealth_path, "r", encoding="utf-8") as f:
                    page.add_init_script(opts_script + f.read())
            else:
                print(f"⚠️ stealth.js not found at {stealth_path}", flush=True)
        except Exception as e:
            print(f"⚠️ Could not load stealth.js: {e}", flush=True)

        try:
            print(f"📡 Navigating to: {url}", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print("⏳ Waiting up to 30s for Vue to render links...", flush=True)
            # Smooth scrolling to trigger lazy loading
            for i in range(5):
                page.evaluate(f"window.scrollTo(0, {i * 1000})")
                page.wait_for_timeout(1000)

            page.wait_for_timeout(5000)

            print(f"📄 Page Title loaded: {page.title()}", flush=True)

            # Extract links and text using evaluate to ensure we get the rendered state
            found_items = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    return links.map(a => ({
                        href: a.getAttribute('href'),
                        text: a.innerText
                    }));
                }
            """)

            print(f"🔍 Found {len(found_items)} total links on page.", flush=True)

            listings = []
            seen_urls = set()

            for item in found_items:
                try:
                    href = item["href"] or ""
                    text = item["text"].strip()

                    # Updated listing detection for Ouedkniss
                    # Matches slugs ending in -d[ID] or old style annonces/details
                    is_listing = (
                        re.search(r"-d\d+", href) or
                        "/annonces/" in href or
                        "/détails-annonce-" in href or
                        (href.startswith("/%D") and len(href) > 20)
                    )

                    if not is_listing or not text or len(text) < 5:
                        continue

                    full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    title = lines[0] if lines else "Unknown"

                    price = "Check Link"
                    price_raw = None
                    for line in lines[1:]:
                        clean = line.replace(" ", "").replace("\xa0", "")
                        if ("دج" in line or "DA" in line.upper()) and any(c.isdigit() for c in clean):
                            price = line.strip()
                            numeric = "".join(c for c in clean if c.isdigit())
                            if numeric:
                                price_raw = int(numeric)
                            break
                        elif any(c.isdigit() for c in clean) and len(clean) >= 4 and len(clean) <= 10:
                            price = line.strip()
                            numeric = "".join(c for c in clean if c.isdigit())
                            if numeric:
                                price_raw = int(numeric)
                            break

                    listings.append({
                        "title": title,
                        "price": price,
                        "price_raw": price_raw,
                        "url": full_url
                    })

                except Exception:
                    continue

            print(f"📦 Extracted {len(listings)} unique listings from page {page_num}.", flush=True)
            return listings

        except Exception as e:
            print(f"❌ Scrape failed (page {page_num}): {e}", flush=True)
            return []
        finally:
            browser.close()

# ── AI Processing ─────────────────────────────────────────────────────────────

def process_item(item):
    ext_id = hashlib.md5(item["url"].encode()).hexdigest()
    if is_seen(ext_id):
        return

    print(f"🆕 New: {item['title'][:60]} — {item['price']}", flush=True)

    prompt = (
        f"You are an expert in the Algerian smartphone resale market. Analyze this listing:\n"
        f"Title: '{item['title']}'\n"
        f"Price: '{item['price']}' (raw numeric value if parsed: {item.get('price_raw')})\n\n"
        f"Return ONLY a JSON object with these exact keys:\n"
        f"- model: the iPhone model string (e.g. 'iPhone 13 Pro Max')\n"
        f"- price_dzd: integer price in Algerian Dinar (DZD), your best estimate\n"
        f"- estimated_market_price_dzd: integer typical market price for this model in Algeria\n"
        f"- market_price_dzd: same as estimated_market_price_dzd\n"
        f"- is_steal: boolean, true if price is 20%+ below market value\n"
        f"- reason: one sentence explanation"
    )

    try:
        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        ai = json.loads(res.choices[0].message.content)

        steal = ai.get("is_steal", False)
        model = ai.get("model", "Unknown")
        price_dzd = ai.get("price_dzd", 0) or 0
        market = ai.get("estimated_market_price_dzd") or ai.get("market_price_dzd") or 0

        print(f"   🤖 {model} | {price_dzd:,} DZD (market: {market:,}) | steal={steal}", flush=True)

        if steal:
            send_ntfy(
                f"🚨 DEAL: {model}",
                f"Price: {price_dzd:,} DZD (market ~{market:,} DZD)\n"
                f"Reason: {ai.get('reason', '')}\n{item['url']}"
            )

        # Ensure both keys are in metadata for frontend compatibility
        ai["estimated_market_price_dzd"] = market
        ai["market_price_dzd"] = market

        supabase.table("listings").upsert({
            "external_id": ext_id,
            "title": item["title"],
            "url": item["url"],
            "price": price_dzd,
            "category": model,
            "is_steal": steal,
            "metadata": ai
        }, on_conflict="external_id").execute()
        supabase.table("seen_listings").upsert({"external_id": ext_id}, on_conflict="external_id").execute()

    except Exception as e:
        print(f"⚠️  AI/DB error for '{item['title'][:40]}': {e}", flush=True)

# ── Health Server & Main Loop ─────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - SwoopDZ Running")
    def log_message(self, *args):
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever(),
        daemon=True
    ).start()
    print(f"🚀 SwoopDZ v4.8 - Native Stealth Active (health :{port})", flush=True)
    print(f"🔔 Alerts → ntfy.sh/{NTFY_TOPIC}", flush=True)

    page_num = 1
    MAX_PAGES = 3

    while True:
        print(f"\n{'='*60}", flush=True)
        print(f"🔄 Starting scrape cycle — pages 1-{MAX_PAGES}", flush=True)

        for page_num in range(1, MAX_PAGES + 1):
            try:
                items = get_listings_stealth(page_num)
                new_count = 0
                for item in items:
                    ext_id = hashlib.md5(item["url"].encode()).hexdigest()
                    if not is_seen(ext_id):
                        process_item(item)
                        new_count += 1
                print(f"✅ Page {page_num}: {len(items)} listings, {new_count} new.", flush=True)
            except Exception as e:
                print(f"💥 Error on page {page_num}: {e}", flush=True)

        print(f"\n😴 Cycle complete. Sleeping 120s...", flush=True)
        time.sleep(120)