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

# Ouedkniss iPhone search — no login required, fully public
SCRAPE_URL = "https://www.ouedkniss.com/s?q=iphone&category=telephonie"

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
2. Extract real price in DZD. Algerians sometimes write "150 000", "150k", or "150.000".
3. Is the price fake or missing? (e.g. 0, 1, "Prix non spécifié") → is_fake_price: true.
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
    print(f"Processing {len(listings)} new listings...")
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
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
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

            # Ouedkniss is a Vue SPA — wait for the listing cards to render
            page.wait_for_selector(".ok-announce-list-item, .v-card, article", timeout=30000)
            page.wait_for_timeout(2000)  # let lazy images / prices finish loading

            print(f"Page title: {page.title()}")

            listings = []

            # --- Strategy 1: find all announce card links ---
            # Ouedkniss listing URLs look like /detail/some-slug/XXXXXXX
            cards = page.query_selector_all("a[href*='/detail/']")
            print(f"Strategy 1 found {len(cards)} card links.")

            for card in cards:
                href = card.get_attribute("href")
                if not href:
                    continue
                full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href

                # Each card contains the title and price as text
                text = card.inner_text().strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                if len(lines) >= 2:
                    # Ouedkniss cards: title first, price somewhere below
                    title = lines[0]
                    # Find the price line — it usually contains digits and "DA" or "DZD"
                    price = next(
                        (l for l in lines[1:] if any(c.isdigit() for c in l)),
                        lines[-1]
                    )
                    listings.append({"title": title, "price": price, "url": full_url})

            # --- Strategy 2 fallback: grab titles and prices separately ---
            if not listings:
                print("Strategy 1 empty, trying strategy 2...")
                titles = page.query_selector_all(".ok-announce-title, h3, h2")
                prices = page.query_selector_all(".ok-announce-price, .price")
                links  = page.query_selector_all("a[href*='/detail/']")

                for i, title_el in enumerate(titles):
                    title = title_el.inner_text().strip()
                    price = prices[i].inner_text().strip() if i < len(prices) else "Prix non spécifié"
                    href  = links[i].get_attribute("href") if i < len(links) else ""
                    if title and href:
                        full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href
                        listings.append({"title": title, "price": price, "url": full_url})

            # Deduplicate by URL
            seen_urls = set()
            unique = []
            for l in listings:
                if l["url"] not in seen_urls:
                    seen_urls.add(l["url"])
                    unique.append(l)
            listings = unique

            print(f"Found {len(listings)} unique listings.")

            if not listings:
                print("No listings found. Page snippet:")
                print(page.content()[:600])
            else:
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
    print("SwoopDZ Scraper v3.0 — Ouedkniss Edition")
    while True:
        scrape_ouedkniss()
        print("Sleeping 60s...")
        time.sleep(60)
