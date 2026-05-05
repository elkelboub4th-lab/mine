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
from playwright_stealth import Stealth  # Fixed import
from groq import Groq

load_dotenv()

# Initialize API clients
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")           
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh")

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

# ── Helpers ──────────────────────────────────────────────────────────────────

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

# ── AI Classification (Groq) ────────────────────────────────

def classify_listing_with_ai(title: str, raw_price: str):
    prompt = f"""
You are an expert in the Algerian iPhone market.
Analyze this Facebook Marketplace listing:
Title: "{title}"
Price as string: "{raw_price}"

Tasks:
1. Determine the iPhone model.
2. Extract actual price in DZD (Handle '15 million' as 150000).
3. Identify fake prices (1111, 1234, etc).
4. Estimate market price.
5. Determine if STEAL (Margin > 20,000 DZD).

Output JSON ONLY:
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
    print(f"Found {len(listings)} items. Filtering duplicates...")
    for item in listings:
        ext_id = hash_id(item["url"], item["title"])
        if is_seen(ext_id):
            continue

        print(f"Analyzing: {item['title']}")
        ai_result = classify_listing_with_ai(item["title"], item["price"])
        
        if not ai_result or ai_result.get("is_fake_price"):
            mark_as_seen(ext_id) # Don't check fake prices again
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

# ── Scraper ──────────────────────────────────────────────────────────────────

def scrape_facebook():
    with sync_playwright() as p:
        # Optimized for Render with the correct Stealth wrapper
        stealth_obj = Stealth()
        browser = p.chromium.launch(headless=True, channel="chromium-headless-shell")
        context = browser.new_context()
        page = context.new_page()
        stealth_obj.apply_sync(page) # Fixed stealth call

        url = "https://www.facebook.com/marketplace/algiers/search/?query=iphone"
        try:
            page.goto(url, timeout=60000)
            page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=20000)

            listings = []
            links = page.query_selector_all('a[href*="/marketplace/item/"]')
            for link in links:
                href = link.get_attribute("href")
                if not href: continue
                full_url = f"https://www.facebook.com{href}" if href.startswith("/") else href
                text_content = link.inner_text()
                if text_content:
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                    if len(lines) >= 2:
                        listings.append({"price": lines[0], "title": lines[1], "url": full_url})

            process_listings(listings)
        except Exception as e:
            print(f"Scrape Error: {e}")
        finally:
            browser.close()

# ── Server & Loop ──────────────────────────────────────────────────────────

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SwoopDZ Active")
    def log_message(self, format, *args): pass

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    print("SwoopDZ Scraper v2.1 Starting...")
    while True:
        scrape_facebook()
        time.sleep(60)
