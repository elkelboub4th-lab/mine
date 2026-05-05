import os
import time
import hashlib
import json
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from supabase import create_client, Client
from groq import Groq
from typing import List, Dict, Optional, Any

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")
NTFY_URL     = os.getenv("NTFY_URL", "https://ntfy.sh")

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("Error: Missing environment variables.")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = ["llama-3.3-70b-versatile", "llama3-8b-8192", "mixtral-8x7b-32768"]

# GraphQL Config
GRAPHQL_URL = "https://api.ouedkniss.com/graphql"
SEARCH_KEYWORDS = "iphone"

# ── Notifications ────────────────────────────────────────────────────────────

def send_ntfy(title: str, body: str):
    try:
        requests.post(f"{NTFY_URL}/{NTFY_TOPIC}", 
                      data=body.encode("utf-8"), 
                      headers={"Title": title, "Priority": "high", "Tags": "iphone,money_bag"})
    except: pass

# ── Database Helpers ─────────────────────────────────────────────────────────

def is_seen(ext_id: str) -> bool:
    try:
        res = supabase.table("seen_listings").select("external_id").eq("external_id", ext_id).execute()
        return len(res.data) > 0
    except: return False

def mark_seen(ext_id: str):
    try: supabase.table("seen_listings").insert({"external_id": ext_id}).execute()
    except: pass

# ── GraphQL Scraper (Fixed with Headers) ─────────────────────────────────────

def get_ouedkniss_listings():
    # MANDATORY HEADERS: Without these, the API returns an empty char 0 error
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.ouedkniss.com",
        "Referer": "https://www.ouedkniss.com/",
        "X-Ouedkniss-Version": "2024-01-01", # Sometimes required for their internal routing
    }

    query = """
    query Search($q: String, $page: Int) {
      search(q: $q, page: $page) {
        announcements {
          data {
            id
            title
            slug
            price
            priceUnit
            hasPrice
            mainMedia {
              thumbnail
            }
          }
        }
      }
    }
    """
    
    variables = {"q": SEARCH_KEYWORDS, "page": 1}
    
    try:
        print(f"🔍 Sending GraphQL request for '{SEARCH_KEYWORDS}'...")
        response = requests.post(
            GRAPHQL_URL, 
            json={"query": query, "variables": variables}, 
            headers=headers,
            timeout=20
        )
        
        if response.status_code != 200:
            print(f"❌ Server returned status {response.status_code}")
            return []

        data = response.json()
        items = data.get("data", {}).get("search", {}).get("announcements", {}).get("data", [])
        return items
    except Exception as e:
        print(f"❌ Scrape Failed: {e}")
        return []

# ── AI Logic ─────────────────────────────────────────────────────────────────

def process_item(item):
    url = f"https://www.ouedkniss.com/annonces/{item['slug']}"
    ext_id = hashlib.md5(url.encode()).hexdigest()
    
    if is_seen(ext_id): return

    title = item.get("title", "")
    price = f"{item.get('price', '')} {item.get('priceUnit', '')}" if item.get("hasPrice") else "Prix non spécifié"
    
    print(f"🆕 New: {title} - {price}")

    prompt = f"Analyze this Ouedkniss iPhone deal: Title: '{title}', Price: '{price}'. Tasks: 1. Model? 2. Price in DZD (fix '15 million' to 150000)? 3. Is it a STEAL (20,000+ profit)? Return JSON only: {{'model': '...', 'price_dzd': 123, 'is_steal': true, 'market_dzd': 123}}"
    
    for model in GROQ_MODELS:
        try:
            res = groq_client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            ai = json.loads(res.choices[0].message.content)
            
            is_steal = ai.get("is_steal", False)
            
            if is_steal:
                print("🔥 STEAL!")
                send_ntfy(f"🚨 STEAL: {ai['model']}", f"Price: {ai['price_dzd']:,} DZD\nLink: {url}")

            supabase.table("listings").insert({
                "external_id": ext_id, "title": title, "url": url,
                "price": ai.get("price_dzd", 0), "is_steal": is_steal, "metadata": ai
            }).execute()
            mark_seen(ext_id)
            break
        except: continue

# ── Server & Loop ────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"Active")
    def log_message(self, *args): pass

def run_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), HealthHandler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    print("🚀 SwoopDZ v4.1 GraphQL Fixed")
    
    while True:
        listings = get_ouedkniss_listings()
        print(f"📦 Found {len(listings)} items.")
        for l in listings: process_item(l)
        print("😴 Sleeping 90s...")
        time.sleep(90)
