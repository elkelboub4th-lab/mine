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
from datetime import datetime

load_dotenv()

# ── Configuration & Initialization ───────────────────────────────────────────
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

# GraphQL API Configuration
GRAPHQL_URL = "https://api.ouedkniss.com/graphql"
GRAPHQL_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://www.ouedkniss.com",
    "Referer": "https://www.ouedkniss.com/",
}

# Search configuration
SEARCH_KEYWORDS = "iphone"
MAX_PAGES = 3  # How many pages to scrape per cycle
ITEMS_PER_PAGE = 20

# ── Notifications ───────────────────────────────────────────────────────────
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
        print(f"✅ Ntfy notification sent: {title}")
    except Exception as e:
        print(f"❌ Error sending ntfy notification: {e}")

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
        print(f"❌ Error checking seen_listings: {e}")
        return False

def mark_as_seen(external_id: str):
    try:
        supabase.table("seen_listings").insert({"external_id": external_id}).execute()
    except Exception as e:
        print(f"❌ Error inserting into seen_listings: {e}")

# ── AI Classification (Groq) ─────────────────────────────────────────────────
def classify_listing_with_ai(title: str, raw_price: str) -> Optional[Dict[str, Any]]:
    prompt = f"""
You are an expert in the Algerian iPhone market.
Analyze this Ouedkniss listing:
Title: "{title}"
Price as string: "{raw_price}"

Tasks:
1. Determine the iPhone model (e.g., "iPhone 13 Pro Max", "iPhone 11"). If unknown, use "Unknown".
2. Extract actual price in DZD. Handle formats like "150 000", "150k", "150.000 DA", "150000 دج".
   - "15 million" = 15000000 (likely fake for phones)
   - "1 DA" or "1 دج" = fake
3. Identify fake prices: 1, 1111, 1234, 999999999, or "Prix non spécifié".
4. Estimate realistic market price for this specific model in DZD (2024 prices).
5. Determine if it is a STEAL: (market_price - listing_price) > 20000 DZD.

Output JSON ONLY (no markdown, no extra text):
{{
    "model": "iPhone 13 Pro",
    "price_dzd": 85000,
    "is_fake_price": false,
    "estimated_market_price_dzd": 110000,
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
            result = json.loads(completion.choices[0].message.content)
            # Validate required fields
            if all(k in result for k in ["model", "price_dzd", "is_fake_price", "estimated_market_price_dzd", "is_steal"]):
                return result
        except Exception as e:
            print(f"⚠️  Groq {model} failed: {e}")
            continue
    return None

# ── GraphQL Scraper Core ─────────────────────────────────────────────────────
def execute_graphql_query(query: str, variables: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Execute a GraphQL query and return the response"""
    payload = {
        "query": query,
        "variables": variables
    }
    
    try:
        response = requests.post(
            GRAPHQL_URL,
            json=payload,
            headers=GRAPHQL_HEADERS,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        
        if "errors" in data:
            print(f"❌ GraphQL errors: {data['errors']}")
            return None
        
        return data.get("data")
    
    except requests.exceptions.RequestException as e:
        print(f"❌ GraphQL request failed: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse GraphQL response: {e}")
        return None

def search_announcements(keywords: str = "iphone", page: int = 1, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Search Ouedkniss announcements using GraphQL API
    Returns list of announcement objects
    """
    
    query = """
    query SearchAnnouncements($filter: AnnouncementFilterInput!) {
        announcements: announcementSearch(filter: $filter) {
            data {
                id
                title
                price
                currency
                description
                url
                createdAt
                updatedAt
                images {
                    id
                    url
                    thumbnail
                }
                location {
                    city {
                        id
                        name
                        slug
                    }
                    region {
                        id
                        name
                        slug
                    }
                }
                category {
                    id
                    name
                    slug
                }
                user {
                    id
                    name
                    type
                }
                attributes {
                    key
                    value
                }
            }
            paginatorInfo {
                currentPage
                lastPage
                total
                count
            }
        }
    }
    """
    
    variables = {
        "filter": {
            "keywords": keywords,
            "pagination": {
                "page": page,
                "limit": limit
            },
            "sort": [
                {"field": "createdAt", "order": "DESC"}
            ]
        }
    }
    
    data = execute_graphql_query(query, variables)
    
    if not data:
        return []
    
    announcements = data.get("announcements", {})
    items = announcements.get("data", [])
    paginator = announcements.get("paginatorInfo", {})
    
    print(f"📊 Page {page}/{paginator.get('lastPage', '?')} - Found {len(items)} items (Total: {paginator.get('total', '?')})")
    
    return items

def transform_announcement_to_listing(announcement: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Transform GraphQL announcement to scraper listing format"""
    try:
        title = announcement.get("title", "")
        price = announcement.get("price", 0)
        currency = announcement.get("currency", "DA")
        url_path = announcement.get("url", "")
        
        # Build full URL
        if url_path.startswith("/"):
            full_url = f"https://www.ouedkniss.com{url_path}"
        elif url_path.startswith("http"):
            full_url = url_path
        else:
            full_url = f"https://www.ouedkniss.com/annonces/{announcement.get('id', '')}"
        
        # Format price string
        if price and price > 0:
            price_str = f"{price:,} {currency}"
        else:
            price_str = "Prix non spécifié"
        
        # Get location
        location = announcement.get("location", {})
        city = location.get("city", {}).get("name", "")
        region = location.get("region", {}).get("name", "")
        location_str = f"{city}, {region}" if city and region else city or region or "Non spécifié"
        
        # Get images
        images = announcement.get("images", [])
        image_urls = [img.get("url") or img.get("thumbnail") for img in images if img.get("url") or img.get("thumbnail")]
        
        # Get category
        category = announcement.get("category", {}).get("name", "Unknown")
        
        # Get user info
        user = announcement.get("user", {})
        seller_name = user.get("name", "Anonyme")
        seller_type = user.get("type", "individual")
        
        # Get creation date
        created_at = announcement.get("createdAt", "")
        
        return {
            "title": title,
            "price": price_str,
            "price_numeric": price if price and price > 0 else 0,
            "url": full_url,
            "location": location_str,
            "city": city,
            "images": image_urls,
            "category": category,
            "seller": seller_name,
            "seller_type": seller_type,
            "created_at": created_at,
            "description": announcement.get("description", "")[:500],  # First 500 chars
            "raw_announcement": announcement  # Keep full data for debugging
        }
    
    except Exception as e:
        print(f"⚠️  Error transforming announcement: {e}")
        return None

def scrape_ouedkniss_graphql() -> List[Dict[str, Any]]:
    """
    Main GraphQL scraper function
    Scrapes multiple pages and returns all valid listings
    """
    print(f"🔍 Starting GraphQL scrape for '{SEARCH_KEYWORDS}'...")
    
    all_listings = []
    
    for page in range(1, MAX_PAGES + 1):
        print(f"\n📄 Scraping page {page}...")
        
        try:
            announcements = search_announcements(
                keywords=SEARCH_KEYWORDS,
                page=page,
                limit=ITEMS_PER_PAGE
            )
            
            if not announcements:
                print(f"⚠️  No announcements found on page {page}")
                break
            
            for announcement in announcements:
                listing = transform_announcement_to_listing(announcement)
                if listing:
                    # Filter for iPhones (case-insensitive)
                    if SEARCH_KEYWORDS.lower() in listing["title"].lower():
                        all_listings.append(listing)
            
            # Respectful delay between pages
            if page < MAX_PAGES:
                time.sleep(2)
        
        except Exception as e:
            print(f"❌ Error on page {page}: {e}")
            break
    
    print(f"\n✅ Total listings found: {len(all_listings)}")
    return all_listings

# ── Listing Processing ───────────────────────────────────────────────────────
def process_listings(listings: List[Dict[str, Any]]):
    print(f"\n🤖 Processing {len(listings)} listings through AI...")
    
    processed_count = 0
    steal_count = 0
    
    for i, item in enumerate(listings, 1):
        print(f"\n[{i}/{len(listings)}] Analyzing: {item['title'][:60]}...")
        
        ext_id = hash_id(item["url"], item["title"])
        
        if is_seen(ext_id):
            print("  ⏭️  Already seen, skipping")
            continue
        
        # AI Classification
        ai_result = classify_listing_with_ai(item["title"], item["price"])
        
        if not ai_result:
            print("  ⚠️  AI classification failed, skipping")
            mark_as_seen(ext_id)
            continue
        
        if ai_result.get("is_fake_price"):
            print(f"  🚫 Fake price detected: {item['price']}")
            mark_as_seen(ext_id)
            continue
        
        is_steal = ai_result.get("is_steal", False)
        parsed_price = ai_result.get("price_dzd", 0)
        
        # Prepare database record
        db_record = {
            "external_id": ext_id,
            "title": item["title"],
            "price": parsed_price,
            "url": item["url"],
            "category": ai_result.get("model", "Unknown"),
            "is_steal": is_steal,
            "location": item.get("location", ""),
            "city": item.get("city", ""),
            "seller": item.get("seller", ""),
            "images": item.get("images", []),
            "metadata": ai_result,
            "scraped_at": datetime.utcnow().isoformat()
        }
        
        # Send notification if steal
        if is_steal:
            steal_count += 1
            print("  🔥🔥 STEAL DETECTED! 🔥🔥🔥")
            
            notif_title = f"🚨 STEAL: {ai_result.get('model')}"
            notif_body = (
                f"💰 Price: {parsed_price:,} DZD\n"
                f"📊 Market: {ai_result.get('estimated_market_price_dzd'):,} DZD\n"
                f"📍 Location: {item.get('location', 'N/A')}\n"
                f"👤 Seller: {item.get('seller', 'N/A')}\n"
                f"🔗 Link: {item['url']}"
            )
            
            send_ntfy_notification(title=notif_title, body=notif_body)
        
        # Save to database
        try:
            supabase.table("listings").insert(db_record).execute()
            mark_as_seen(ext_id)
            processed_count += 1
            print(f"  ✅ Saved to database")
        
        except Exception as e:
            print(f"  ❌ DB Error: {e}")
    
    print(f"\n📈 Summary: Processed {processed_count} listings, Found {steal_count} steals")

# ── Health-Check Server (Render Requirement) ─────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SwoopDZ Active - GraphQL Scraper")
    
    def log_message(self, *args):
        pass  # Silence access logs

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"🏥 Health server running on port {port}")
    server.serve_forever()

# ── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start the health server in background
    threading.Thread(target=run_server, daemon=True).start()
    
    print("=" * 70)
    print("🚀 SwoopDZ Scraper v4.0 — Ouedkniss GraphQL Edition")
    print("=" * 70)
    print(f" Keywords: {SEARCH_KEYWORDS}")
    print(f"📄 Pages per cycle: {MAX_PAGES}")
    print(f"📦 Items per page: {ITEMS_PER_PAGE}")
    print(f"🗄️  Supabase: {'✅' if supabase else '❌'}")
    print(f"🤖 Groq AI: {'✅' if groq_client else '❌'}")
    print(f"🔔 Ntfy: {'✅' if NTFY_TOPIC else '❌'}")
    print("=" * 70)
    
    while True:
        try:
            start_time = time.time()
            
            # Scrape using GraphQL
            listings = scrape_ouedkniss_graphql()
            
            if listings:
                process_listings(listings)
            else:
                print("⚠️  No listings found in this cycle")
            
            elapsed = time.time() - start_time
            print(f"\n⏱️  Cycle completed in {elapsed:.2f} seconds")
            
        except Exception as e:
            print(f"❌ Critical error in main loop: {e}")
            import traceback
            traceback.print_exc()
        
        print(f"\n😴 Sleeping 90 seconds before next cycle...")
        time.sleep(90)
