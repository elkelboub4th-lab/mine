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

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")
NTFY_URL     = os.getenv("NTFY_URL", "https://ntfy.sh")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# ── Notifications ────────────────────────────────────────────────────────────

def send_ntfy(title, body):
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

# ── Optimized Scraper (Inspired by abdelhak-k/OuedKniss-Scraper) ──────────────

def get_ouedkniss_listings():
    url = "https://api.ouedkniss.com/graphql"
    
    # These specific headers prevent the "empty response" error
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "X-Ouedkniss-Version": "2024-05-15",
        "X-App-Language": "fr",
        "Origin": "https://www.ouedkniss.com",
        "Referer": "https://www.ouedkniss.com/",
    }

    # Enhanced query that includes 'status' and 'user' to verify listing quality
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
            status
            user {
              username
            }
            category {
              name
            }
          }
        }
      }
    }
    """
    
    variables = {"q": "iphone", "page": 1}
    
    try:
        print("🔍 Searching Ouedkniss...")
        response = requests.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"❌ HTTP Error {response.status_code}")
            return []

        json_data = response.json()
        
        # Check for GraphQL specific errors inside a 200 response
        if "errors" in json_data:
            print(f"❌ GraphQL Error: {json_data['errors'][0]['message']}")
            return []

        items = json_data.get("data", {}).get("search", {}).get("announcements", {}).get("data", [])
        return items

    except Exception as e:
        print(f"❌ Fatal Scrape Error: {e}")
        return []

# ── AI Analyzer ──────────────────────────────────────────────────────────────

def analyze_and_save(item):
    slug = item.get('slug')
    title = item.get('title', 'Unknown')
    url = f"https://www.ouedkniss.com/annonces/{slug}"
    ext_id = hashlib.md5(url.encode()).hexdigest()
    
    if is_seen(ext_id): return

    # Extract price context
    price_val = item.get('price', '')
    unit = item.get('priceUnit', '')
    display_price = f"{price_val} {unit}" if item.get('hasPrice') else "Prix non spécifié"
    
    print(f"🆕 Processing: {title} | {display_price}")

    prompt = f"""Analyze this Ouedkniss listing:
    Title: {title}
    Price: {display_price}
    Category: {item.get('category', {}).get('name', 'N/A')}
    
    Tasks:
    1. Extract actual price in DZD (e.g. 15 million = 150000).
    2. Determine if it's a STEAL (profit margin > 20,000 DZD).
    3. Output JSON: {{"model": "...", "price_dzd": 0, "is_steal": false}}"""
    
    try:
        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        ai = json.loads(res.choices[0].message.content)
        
        if ai.get("is_steal"):
            print("🔥 Alert: Steal detected!")
            send_ntfy(f"🚨 DEAL: {ai.get('model')}", f"Price: {ai.get('price_dzd'):,} DZD\nLink: {url}")

        supabase.table("listings").insert({
            "external_id": ext_id, "title": title, "url": url,
            "price": ai.get("price_dzd", 0), "is_steal": ai.get("is_steal"), "metadata": ai
        }).execute()
        mark_seen(ext_id)
    except Exception as e:
        print(f"⚠️  AI/DB Error: {e}")

# ── Health & Loop ────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ALIVE")
    def log_message(self, *args): pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    print("🚀 SwoopDZ v4.3 Live")
    
    while True:
        listings = get_ouedkniss_listings()
        print(f"📦 Fetched {len(listings)} potential deals.")
        
        for l in listings:
            analyze_and_save(l)
        
        print("😴 Waiting 90s for next scan...")
        time.sleep(90)
