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

# ── Configuration ────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# ── Notifications & DB ───────────────────────────────────────────────────────

def send_ntfy(title, body):
    try: requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers={"Title": title})
    except: pass

def is_seen(ext_id: str) -> bool:
    try:
        res = supabase.table("seen_listings").select("external_id").eq("external_id", ext_id).execute()
        return len(res.data) > 0
    except: return False

# ── The Stealth Scraper (Bypasses "JavaScript Required" error) ────────────────

def get_listings_stealth():
    with sync_playwright() as p:
        # Launch browser with specific flags to hide "headless" mode
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        url = "https://www.ouedkniss.com/s/1?keywords=iphone"
        print(f"📡 Opening {url} and waiting for JS to execute...")
        
        try:
            # wait_until="networkidle" ensures the Vue/Nuxt apps load completely
            page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Wait specifically for the listing links to appear
            page.wait_for_selector("a[href*='/annonces/']", timeout=20000)
            
            # Extract items directly from the DOM (bypass the broken GraphQL call)
            cards = page.query_selector_all("a[href*='/annonces/']")
            listings = []
            
            for card in cards:
                text = card.inner_text().strip()
                href = card.get_attribute("href")
                if not text or not href: continue
                
                full_url = f"https://www.ouedkniss.com{href}" if href.startswith("/") else href
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                
                if len(lines) >= 2:
                    listings.append({
                        "title": lines[0],
                        "price": lines[1] if "DA" in lines[1] or any(c.isdigit() for c in lines[1]) else "Check Link",
                        "url": full_url
                    })
            
            return listings
        except Exception as e:
            print(f"❌ Stealth Scrape Failed: {e}")
            return []
        finally:
            browser.close()

# ── AI Processing ────────────────────────────────────────────────────────────

def process_item(item):
    ext_id = hashlib.md5(item['url'].encode()).hexdigest()
    if is_seen(ext_id): return

    print(f"🆕 Found: {item['title']} - {item['price']}")

    prompt = f"Analyze: Title '{item['title']}', Price '{item['price']}'. Return JSON: {{'model': '...', 'price_dzd': 123, 'is_steal': true}}"
    
    try:
        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        ai = json.loads(res.choices[0].message.content)
        
        if ai.get("is_steal"):
            send_ntfy(f"🚨 DEAL: {ai.get('model')}", f"Price: {ai.get('price_dzd'):,} DZD\n{item['url']}")

        supabase.table("listings").insert({
            "external_id": ext_id, "title": item['title'], "url": item['url'],
            "price": ai.get("price_dzd", 0), "is_steal": ai.get("is_steal"), "metadata": ai
        }).execute()
        supabase.table("seen_listings").insert({"external_id": ext_id}).execute()
    except Exception as e:
        print(f"⚠️  AI Error: {e}")

# ── Render Health Server & Main ───────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), HealthHandler).serve_forever(), daemon=True).start()
    print("🚀 SwoopDZ v4.4 Stealth Mode Active")
    
    while True:
        items = get_listings_stealth()
        print(f"📦 Extracted {len(items)} items.")
        for item in items: process_item(item)
        print("😴 Sleeping 120s...")
        time.sleep(120)
