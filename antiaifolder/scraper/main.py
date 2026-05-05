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
from playwright_stealth import stealth
from groq import Groq

load_dotenv()

# Initialize API clients
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")           # e.g. "swoopdz-alerts"
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh")  # defaults to public free server

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("Missing environment variables. Please check your .env file.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# All Groq models to rotate through — if one fails, the next is tried
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


# ── Notifications ────────────────────────────────────────────────────────────

def send_ntfy_notification(title: str, body: str, priority: str = "high", tags: str = "iphone,money_bag"):
    """Send a push notification via ntfy.sh (free public server)."""
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


# ── AI Classification (Groq, rotating models) ────────────────────────────────

def classify_listing_with_ai(title: str, raw_price: str):
    """
    Uses Groq (rotating through all available models) to classify the deal.
    Falls back to the next model if one fails or returns invalid JSON.
    """
    prompt = f"""
You are an expert in the Algerian iPhone market.
Analyze this Facebook Marketplace listing:
Title: "{title}"
Price as string: "{raw_price}"

Tasks:
1. Determine the iPhone model (e.g., "iPhone 13 Pro Max", "iPhone 11", etc.). If unknown, return "Unknown".
2. Extract the actual price in DZD (Algerian Dinars). Algerian users often use fake/placeholder prices
   like "1111", "123456", or "1 DZD". They may also write amounts like "15 million" meaning 150,000 DZD.
3. Check if the price is a fake price (e.g. 1111, 1234, 1 DZD).
4. Estimate the current typical market price of this model in DZD.
5. Determine if this is a STEAL (Estimated Market Price - Actual Listing Price > 20,000 DZD).

Output ONLY valid JSON, no extra text:
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
            print(f"  Trying Groq model: {model}")
            completion = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            result = json.loads(completion.choices[0].message.content)
            print(f"  Classification success with {model}")
            return result
        except Exception as e:
            print(f"  Groq model {model} failed: {e} — trying next model...")

    print("All Groq models failed for this listing.")
    return None


# ── Listing Processing ───────────────────────────────────────────────────────

def process_listings(listings):
    print(f"Processing {len(listings)} listings...")
    for item in listings:
        ext_id = hash_id(item["url"], item["title"])
        if is_seen(ext_id):
            continue

        print(f"New listing found: {item['title']} - {item['price']}")

        ai_result = classify_listing_with_ai(item["title"], item["price"])
        if not ai_result:
            continue

        is_steal = False
        parsed_price = ai_result.get("price_dzd", 0)

        if not ai_result.get("is_fake_price") and ai_result.get("is_steal"):
            is_steal = True
            print(">>> STEAL FOUND! <<<")

            notif_title = f"🚨 STEAL: {ai_result.get('model')} — {parsed_price:,} DZD"
            notif_body = (
                f"Title: {item['title']}\n"
                f"Price: {parsed_price:,} DZD\n"
                f"Est. Market: {ai_result.get('estimated_market_price_dzd'):,} DZD\n"
                f"Link: {item['url']}"
            )
            send_ntfy_notification(title=notif_title, body=notif_body)

        # Save to DB
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
            print(f"Error saving to DB: {e}")


# ── Scraper ──────────────────────────────────────────────────────────────────

def scrape_facebook():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        stealth_sync(page)

        url = "https://www.facebook.com/marketplace/algiers/search/?query=iphone"
        print(f"Scraping {url}...")
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector(
                'div[role="feed"] a, a[href*="/marketplace/item/"]',
                timeout=10000,
            )

            listings = []
            links = page.query_selector_all('a[href*="/marketplace/item/"]')
            for link in links:
                href = link.get_attribute("href")
                if not href:
                    continue
                full_url = (
                    f"https://www.facebook.com{href}"
                    if href.startswith("/")
                    else href
                )

                text_content = link.inner_text()
                if text_content:
                    lines = [line.strip() for line in text_content.split("\n") if line.strip()]
                    if len(lines) >= 2:
                        price_str = lines[0]
                        title_str = lines[1]
                        listings.append({
                            "title": title_str,
                            "price": price_str,
                            "url": full_url,
                        })

            process_listings(listings)

        except Exception as e:
            print(f"Error during scraping: {e}")
        finally:
            browser.close()


# ── Health Check Server (for Render) ─────────────────────────────────────────

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SwoopDZ Scraper is healthy and running.")

    def log_message(self, format, *args):
        pass  # Silence request logs


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    print(f"Started health-check HTTP server on port {port}.")
    server.serve_forever()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()

    print("Starting SwoopDZ Scraper Engine...")
    while True:
        scrape_facebook()
        print("Sleeping for 60 seconds...")
        time.sleep(60)
