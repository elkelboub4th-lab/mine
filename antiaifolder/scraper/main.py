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
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="fr-DZ",
            timezone_id="Africa/Algiers",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "fr-DZ,fr;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            },
        )
        page = context.new_page()
        
        # Stealth scripts
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['fr-DZ', 'fr', 'en']});
        """)
        
        # Log requests for debugging
        page.on("response", lambda r: print(f"  📡 {r.status} {r.url.split('?')[0][-60:]}") 
                if "ouedkniss" in r.url and r.status >= 400 else None)
        
        print(f"🔍 Scraping {SCRAPE_URL} ...")
        
        try:
            # Go to page and wait for initial load
            page.goto(SCRAPE_URL, wait_until="domcontentloaded", timeout=60000)
            
            # Handle cookie consent if present
            try:
                for btn_sel in ["button.cp-accept", "#onetrust-accept-btn-handler", "button[data-cookie-accept]"]:
                    btn = page.query_selector(btn_sel)
                    if btn and btn.is_visible():
                        print("  🍪 Accepting cookies...")
                        btn.click()
                        page.wait_for_load_state("networkidle", timeout=10000)
                        break
            except:
                pass  # No cookie banner or already accepted
            
            # ✅ CRITICAL: Wait for Vue to hydrate listings
            # Ouedkniss uses <article class="o-announ-card"> or <div class="v-card">
            card_selectors = [
                "article.o-announ-card",
                ".o-announ-card", 
                ".v-card.annonce-card",
                "[data-testid='annonce-card']",
                "article.annonce"
            ]
            
            card_found = False
            for selector in card_selectors:
                try:
                    page.wait_for_selector(selector, state="attached", timeout=12000)
                    print(f"  ✓ Cards found with: {selector}")
                    card_found = True
                    break
                except:
                    continue
            
            if not card_found:
                print("  ❌ No listing cards found — page may be blocked or empty")
                _debug_dump(page, "no_cards")
                browser.close()
                return
            
            # Wait for network to settle + Vue hydration
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2500)  # Extra buffer for lazy-loaded prices
            
            # ✅ Extract listings with robust selectors
            listings = []
            seen_urls = set()
            
            # Get all cards first, then extract data from each
            cards = page.query_selector_all("article.o-announ-card, .o-announ-card, .v-card.annonce-card")
            print(f"  📦 Found {len(cards)} card elements")
            
            for card in cards:
                try:
                    # Extract link
                    link_el = card.query_selector("a[href]")
                    if not link_el:
                        continue
                    href = link_el.get_attribute("href") or ""
                    
                    # Normalize URL
                    if href.startswith("/"):
                        full_url = f"https://www.ouedkniss.com{href}"
                    elif href.startswith("http"):
                        full_url = href
                    else:
                        continue
                    
                    # Validate it's an annonce page
                    if "/annonces/" not in full_url or (".htm" not in full_url and ".html" not in full_url):
                        continue
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    
                    # Extract title & price with CSS selectors FIRST
                    title_el = card.query_selector("h3, .o-announ-card-title, .card-title, .titre_annonce")
                    price_el = card.query_selector("span.price, .prix, .o-announ-card-price, .card-price, .montant")
                    
                    title = title_el.inner_text().strip() if title_el else ""
                    price = price_el.inner_text().strip() if price_el else ""
                    
                    # Fallback: parse from innerText if selectors fail
                    if not title or len(title) < 4:
                        text = card.inner_text().strip()
                        lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 3]
                        if lines:
                            title = lines[0]
                            # Find price: line with digits + DA/dj/دج
                            for line in lines[1:5]:
                                if any(c.isdigit() for c in line) and any(k in line.lower() for k in ["da", "دج", "dj", "000", " k", "k "]):
                                    price = line
                                    break
                    
                    # Only keep if title looks valid
                    if title and len(title) > 5 and "iphone" in title.lower():
                        listings.append({
                            "title": title,
                            "price": price if price else "Prix non spécifié",
                            "url": full_url
                        })
                        
                except Exception as e:
                    print(f"  ⚠ Error parsing card: {e}")
                    continue
            
            print(f"✅ Extracted {len(listings)} valid iPhone listings")
            
            if listings:
                process_listings(listings)
            else:
                print("  ⚠ No valid listings after filtering — dumping debug HTML")
                _debug_dump(page, "no_valid_listings")
                
        except Exception as e:
            print(f"❌ Scrape error: {e}")
            import traceback
            traceback.print_exc()
            _debug_dump(page, "scrape_error")
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
