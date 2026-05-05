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

# Correct URL (what Ouedkniss actually resolves to)
SCRAPE_URL = "https://www.ouedkniss.com/s/1?keywords=iphone"

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
        return len(
            supabase.table("seen_listings")
            .select("external_id")
            .eq("external_id", external_id)
            .execute().data
        ) > 0
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
Analyze this Ouedkniss listing:
Title: "{title}"
Price string: "{raw_price}"

1. Detect iPhone model (e.g. "iPhone 13 Pro Max"). Unknown → "Unknown".
2. Extract real price in DZD. Algerians write "150 000", "150k", "150.000", "150 000 DA".
3. Is the price fake or missing? (0, 1, "Prix non spécifié", empty) → is_fake_price: true.
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
    print(f"Processing {len(listings)} unique listings...")
    for item in listings:
        ext_id = hash_id(item["url"], item["title"])
        if is_seen(ext_id):
            continue

        print(f"  Analyzing: {item['title']} — {item['price']}")
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

def scrape_ouedkniss():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="fr-DZ",
        )
        page = context.new_page()
        print(f"Scraping {SCRAPE_URL} ...")

        try:
            page.goto(SCRAPE_URL, timeout=60000)

            # Wait for Vue to mount cards — use "attached" not "visible"
            # because Ouedkniss cards exist in DOM before they're fully painted
            page.wait_for_selector(".v-card", state="attached", timeout=30000)

            # Extra wait for prices/lazy content to populate
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(1500)

            print(f"Page title: {page.title()}")

            listings   = []
            seen_urls  = set()

            # Ouedkniss detail links look like /detail/some-title/12345678
            cards = page.query_selector_all("a[href*='/detail/']")
            print(f"Found {len(cards)} detail links.")

            for card in cards:
                href = card.get_attribute("href")
                if not href:
                    continue
                full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                text  = card.inner_text().strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                if not lines:
                    continue

                title = lines[0]

                # Price line: first line after title that contains a digit
                price = next(
                    (l for l in lines[1:] if any(c.isdigit() for c in l)),
                    "Prix non spécifié"
                )

                if len(title) > 5:  # skip empty/garbage titles
                    listings.append({"title": title, "price": price, "url": full_url})

            print(f"Found {len(listings)} unique listings.")

            if not listings:
                # Debug: dump a snippet so we can fix selectors
                print("No listings extracted. HTML snippet:")
                print(page.content()[2000:3000])
            else:
                process_listings(listings)

        except Exception as e:
            print(f"Scrape error: {e}")
            # Dump page content to debug selector issues
            try:
                print("Page snippet for debugging:")
                print(page.content()[2000:3000])
            except Exception:
                pass
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
    print("SwoopDZ Scraper v3.1 — Ouedkniss Edition")
    while True:
        scrape_ouedkniss()
        print("Sleeping 60s...")
        time.sleep(60)
