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

# ── Configuration & Initialization ───────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")
NTFY_URL     = os.getenv("NTFY_URL", "https://ntfy.sh")

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("Missing environment variables. Please check your Render Environment tab.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

# The target URL for iPhones on Ouedkniss
SCRAPE_URL = "https://www.ouedkniss.com/s/1?keywords=iphone"


# ── Notifications ────────────────────────────────────────────────────────────

def send_ntfy_notification(title: str, body: str, priority: str = "high", tags: str = "iphone,money_bag"):
    endpoint = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    try:
        response = requests.post(endpoint, data=body.encode("utf-8"), headers=headers)
        response.raise_for_status()
        print(f"ntfy notification sent to {endpoint}")
    except Exception as e:
        print(f"Error sending ntfy notification: {e}")


# ── Database Helpers ─────────────────────────────────────────────────────────

def hash_id(url: str, title: str) -> str:
    combined = f"{title}_{url.split('?')[0]}"
    return hashlib.md5(combined.encode()).hexdigest()

def is_seen(external_id: str) -> bool:
    try:
        response = (
            supabase.table("seen_listings")
            .select("external_id")
            .eq("external_id", external_id)
            .execute()
        )
        return len(response.data) > 0
    except Exception as e:
        print(f"Error checking seen_listings: {e}")
        return False

def mark_as_seen(external_id: str):
    try:
        supabase.table("seen_listings").insert({"external_id": external_id}).execute()
    except Exception as e:
        print(f"Error inserting into seen_listings: {e}")


# ── AI Classification (Groq) ─────────────────────────────────────────────────

def classify_listing_with_ai(title: str, raw_price: str):
    prompt = f"""
You are an expert in the Algerian iPhone market.
Analyze this Ouedkniss listing:
Title: "{title}"
Price as string: "{raw_price}"

Tasks:
1. Determine the iPhone model.
2. Extract actual price in DZD (Handle '15 million' as 150000, or '1 DA' as fake).
3. Identify fake prices (e.g., 1, 1111, 1234, or 'Prix non spécifié').
4. Estimate market price for this specific model in DZD.
5. Determine if it is a STEAL (Margin > 20,000 DZD).

Output JSON ONLY (no markdown blocks, no extra text):
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
            completion = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            return json.loads(completion.choices[0].message.content)
        except Exception as e:
            print(f"Groq {model} failed, trying next...")
    return None


# ── Listing Processing ───────────────────────────────────────────────────────

def process_listings(listings):
    print(f"Found {len(listings)} raw items. Processing...")
    
    for item in listings:
        ext_id = hash_id(item["url"], item["title"])
        if is_seen(ext_id):
            continue

        print(f"Analyzing: {item['title']} - {item['price']}")
        ai_result = classify_listing_with_ai(item["title"], item["price"])
        
        if not ai_result or ai_result.get("is_fake_price"):
            mark_as_seen(ext_id) # Skip and don't process fake prices again
            continue

        is_steal = ai_result.get("is_steal", False)
        parsed_price = ai_result.get("price_dzd", 0)

        if is_steal:
            print("🔥 STEAL DETECTED 🔥")
            notif_title = f"🚨 STEAL: {ai_result.get('model')}"
            notif_body = f"Price: {parsed_price:,} DZD\nMarket: {ai_result.get('estimated_market_price_dzd'):,} DZD\nLink: {item['url']}"
            send_ntfy_notification(title=notif_title, body=notif_body)

        try:
            supabase.table("listings").insert({
                "external_id": ext_id,
                "title": item["title"],
                "price": parsed_price,
                "url": item["url"],
                "category": ai_result.get("model", "Unknown"),
                "is_steal": is_steal,
                "metadata": ai_result,
            }).execute()
            mark_as_seen(ext_id)
        except Exception as e:
            print(f"DB Error: {e}")


# ── Scraper Core ─────────────────────────────────────────────────────────────

def scrape_ouedkniss():
    with sync_playwright() as p:
        # Optimized for Render's environment
        browser = p.chromium.launch(
            headless=True,
            channel="chromium-headless-shell",
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="fr-DZ",
        )
        page = context.new_page()
        print(f"Scraping {SCRAPE_URL} ...")

        try:
            page.goto(SCRAPE_URL, timeout=60000)

            # Wait for Ouedkniss Vue framework to mount the item cards
            page.wait_for_selector(".v-card", state="attached", timeout=30000)
            
            # Wait for lazy-loaded prices to populate
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)

            listings = []
            seen_urls = set()

            cards = page.query_selector_all(".v-card")
            print(f"Found {len(cards)} v-card elements.")

            for card in cards:
                href = card.get_attribute("href")
                if not href:
                    link = card.query_selector("a")
                    if link:
                        href = link.get_attribute("href")
                
                if not href or href == "#" or len(href) < 10:
                    continue
                    
                full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href
                
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                text = card.inner_text().strip()
                if not text:
                    continue
                    
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                if len(lines) < 2:
                    continue

                title = lines[0]
                price = next((l for l in lines[1:] if any(c.isdigit() for c in l)), "Prix non spécifié")

                if len(title) > 3:
                    listings.append({"title": title, "price": price, "url": full_url})

            print(f"Found {len(listings)} unique listings.")

            if listings:
                process_listings(listings)
            else:
                print("No listings extracted. UI may have changed.")

        except Exception as e:
            print(f"Scrape error: {e}")
        finally:
            browser.close()


# ── Health-Check Server (Render Requirement) ─────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SwoopDZ Active")
    def log_message(self, *args):
        pass # Silence access logs to keep terminal clean

def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start the dummy server in the background so Render doesn't kill the app
    threading.Thread(target=run_server, daemon=True).start()
    
    print("SwoopDZ Scraper v3.2 — Ouedkniss Edition Starting...")
    
    while True:
        scrape_ouedkniss()
        print("Sleeping 60s...")
        time.sleep(60)
