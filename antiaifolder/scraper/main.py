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
from playwright_stealth import Stealth
from openai import OpenAI

load_dotenv()

# Initialize API clients
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([SUPABASE_URL, SUPABASE_KEY, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("Missing environment variables. Please check your .env file.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def hash_id(url: str, title: str) -> str:
    # Use MD5 to generate a unique ID based on URL (or Title + URL to be safe)
    # FB marketplace URLs sometimes have tracking params, we should strip them or just use title+price
    # Simple implementation:
    combined = f"{title}_{url.split('?')[0]}"
    return hashlib.md5(combined.encode()).hexdigest()

def is_seen(external_id: str) -> bool:
    try:
        response = supabase.table("seen_listings").select("external_id").eq("external_id", external_id).execute()
        return len(response.data) > 0
    except Exception as e:
        print(f"Error checking seen_listings: {e}")
        return False

def mark_as_seen(external_id: str):
    try:
        supabase.table("seen_listings").insert({"external_id": external_id}).execute()
    except Exception as e:
        print(f"Error inserting into seen_listings: {e}")

def classify_listing_with_ai(title: str, raw_price: str):
    """
    Uses OpenAI GPT-4o-mini to classify the deal and determine if it's a steal.
    """
    prompt = f"""
    You are an expert in the Algerian iPhone market. 
    Analyze this Facebook Marketplace listing:
    Title: "{title}"
    Price as string: "{raw_price}"

    Tasks:
    1. Determine the iPhone model (e.g., "iPhone 13 Pro Max", "iPhone 11", etc.). If unknown, return "Unknown".
    2. Extract the actual price in DZD (Algerian Dinars). Often users use Darja fake prices like "1111" or "123456" to hide the real price. Or they might write "15 million" meaning 150,000 DZD.
    3. Check if the price is a "Fake Price". (e.g. 1111, 1234, 1 DZD).
    4. Estimate the current typical market price of this model in DZD.
    5. Determine if this is a 'STEAL' (i.e. Estimated Market Price - Actual Listing Price > 20000 DZD).

    Output as JSON ONLY:
    {{
        "model": "iPhone ...",
        "price_dzd": 150000,
        "is_fake_price": false,
        "estimated_market_price_dzd": 180000,
        "is_steal": true
    }}
    """
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        result = json.loads(completion.choices[0].message.content)
        return result
    except Exception as e:
        print(f"OpenAI error: {e}")
        return None

def process_listings(listings):
    print(f"Processing {len(listings)} listings...")
    for item in listings:
        ext_id = hash_id(item['url'], item['title'])
        if is_seen(ext_id):
            continue
            
        print(f"New listing found: {item['title']} - {item['price']}")
        
        ai_result = classify_listing_with_ai(item['title'], item['price'])
        if not ai_result:
            continue

        is_steal = False
        parsed_price = ai_result.get("price_dzd", 0)
        
        if not ai_result.get("is_fake_price") and ai_result.get("is_steal"):
            is_steal = True
            print(">>> STEAL FOUND! <<<")
            msg = (
                f"🚨 <b>STEAL ALERT!</b> 🚨\n\n"
                f"📱 <b>Model:</b> {ai_result.get('model')}\n"
                f"💰 <b>Price:</b> {parsed_price} DZD\n"
                f"🏷 <b>Title:</b> {item['title']}\n"
                f"📈 <b>Est. Market:</b> {ai_result.get('estimated_market_price_dzd')} DZD\n"
                f"🔗 <a href='{item['url']}'>Link</a>"
            )
            send_telegram_message(msg)

        # Insert to DB
        try:
            supabase.table("listings").insert({
                "external_id": ext_id,
                "title": item['title'],
                "price": parsed_price,
                "url": item['url'],
                "category": ai_result.get("model", "Unknown"),
                "is_steal": is_steal,
                "metadata": ai_result
            }).execute()
            
            # Mark as seen
            mark_as_seen(ext_id)
        except Exception as e:
            print(f"Error saving to DB: {e}")

def scrape_facebook():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        stealth_sync(page)
        
        # Go to FB marketplace Algiers for iPhone
        url = "https://www.facebook.com/marketplace/algiers/search/?query=iphone"
        print(f"Scraping {url}...")
        try:
            page.goto(url, timeout=30000)
            # Wait for some listings to load
            page.wait_for_selector('div[role="feed"] a, a[href*="/marketplace/item/"]', timeout=10000)
            
            # Extract basic info
            listings = []
            
            # This selector is very generic because FB obfuscates class names
            # We look for links containing /marketplace/item/
            links = page.query_selector_all('a[href*="/marketplace/item/"]')
            for link in links:
                href = link.get_attribute("href")
                if not href: continue
                full_url = f"https://www.facebook.com{href}" if href.startswith('/') else href
                
                # Try to extract text inside
                text_content = link.inner_text()
                if text_content:
                    lines = [line.strip() for line in text_content.split('\n') if line.strip()]
                    if len(lines) >= 2:
                        # Usually price is first or second line, title follows
                        price_str = lines[0]
                        title_str = lines[1]
                        
                        listings.append({
                            "title": title_str,
                            "price": price_str,
                            "url": full_url
                        })

            process_listings(listings)
            
        except Exception as e:
            print(f"Error during scraping: {e}")
        finally:
            browser.close()

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SwoopDZ Scraper is healthy and running.")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    print(f"Started dummy HTTP server on port {port} for Render health checks.")
    server.serve_forever()

if __name__ == "__main__":
    # Start dummy server in a background thread
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    print("Starting SwoopDZ Scraper Engine...")
    while True:
        scrape_facebook()
        print("Sleeping for 60 seconds...")
        time.sleep(60)
