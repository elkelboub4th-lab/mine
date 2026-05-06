import os
import sys
import time
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
from playwright_stealth import stealth_sync
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
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="fr-DZ",
            timezone_id="Africa/Algiers",
            viewport={"width": 1280, "height": 900}
        )
        page = context.new_page()
        stealth_sync(page)

        try:
            print(f"📡 Navigating to: {url}", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print("⏳ Waiting 8s for Vue to render listings...", flush=True)
            page.wait_for_timeout(8000)

            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)

            print(f"📄 Page Title loaded: {page.title()}", flush=True)
            all_links = page.query_selector_all("a[href]")
            print(f"🔍 Found {len(all_links)} total links on page.", flush=True)

            listings = []
            seen_urls = set()

            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()

                    is_listing = (
                        href.startswith("/%D") or  
                        (href.startswith("/") and
                         len(href) > 30 and           
                         "/store/" not in href and
                         "/auth" not in href and
                         "/categorie" not in href)
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
        f"- market_price_dzd: integer typical market price for this model in Algeria\n"
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
        price_dzd = ai.get("price_dzd", 0)
        market = ai.get("market_price_dzd", 0)

        print(f"   🤖 {model} | {price_dzd:,} DZD (market: {market:,}) | steal={steal}", flush=True)

        if steal:
            send_ntfy(
                f"🚨 DEAL: {model}",
                f"Price: {price_dzd:,} DZD (market ~{market:,} DZD)\n"
                f"Reason: {ai.get('reason', '')}\n{item['url']}"
            )

        supabase.table("listings").insert({
            "external_id": ext_id,
            "title": item["title"],
            "url": item["url"],
            "price": price_dzd,
            "is_steal": steal,
            "metadata": ai
        }).execute()
        supabase.table("seen_listings").insert({"external_id": ext_id}).execute()

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
    print(f"🚀 SwoopDZ v4.7 - Native Stealth Active (health :{port})", flush=True)
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