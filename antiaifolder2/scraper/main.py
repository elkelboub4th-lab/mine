import os
import sys
import time
import hashlib
import json
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

from supabase import create_client, Client
from groq import Groq

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC")

if not all([SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY, NTFY_TOPIC]):
    print("Missing env vars! Check your Render Environment tab.", flush=True)
    sys.exit(1)

supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

# ── Ouedkniss GraphQL API ─────────────────────────────────────────────────────
# Ouedkniss is a Vue SPA backed by a GraphQL API — calling it directly is
# faster and more reliable than browser scraping.

GRAPHQL_URL = "https://api.ouedkniss.com/graphql"

GRAPHQL_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.ouedkniss.com",
    "Referer": "https://www.ouedkniss.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

SEARCH_QUERY = """
query SearchAnnounces($keywords: String, $page: Int) {
  searchAnnounces(
    filter: { keywords: $keywords, categorySlug: "telephonie" }
    page: $page
  ) {
    announcements {
      id
      title
      price
      pricePreview
      priceUnit
      slug
      category { slug }
      user { username }
    }
    count
    totalPages
  }
}
"""

def fetch_listings(page: int = 1):
    payload = {
        "operationName": "SearchAnnounces",
        "query": SEARCH_QUERY,
        "variables": {"keywords": "iphone", "page": page},
    }
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json=payload,
            headers=GRAPHQL_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # Surface any GraphQL errors
        if "errors" in data:
            print(f"  GraphQL errors: {data['errors']}", flush=True)
            return []

        announcements = (
            data.get("data", {})
                .get("searchAnnounces", {})
                .get("announcements", [])
        )
        return announcements
    except Exception as e:
        print(f"  API fetch error (page {page}): {e}", flush=True)
        return []

def announcement_to_item(a: dict) -> dict:
    listing_id = str(a.get("id", ""))
    slug       = a.get("slug", listing_id)
    url        = f"https://www.ouedkniss.com/detail/{slug}/{listing_id}"
    title      = a.get("title", "Unknown")

    # Price can be in pricePreview ("150 000 DA") or price (float)
    price_preview = a.get("pricePreview") or ""
    price_raw     = a.get("price")
    price_str     = price_preview if price_preview else (str(price_raw) if price_raw else "Prix non spécifié")

    return {
        "title":     title,
        "price":     price_str,
        "price_raw": price_raw,
        "url":       url,
        "id":        listing_id,
    }

# ── Notifications ─────────────────────────────────────────────────────────────

def send_ntfy(title, body):
    try:
        safe_title = title.encode("ascii", errors="ignore").decode("ascii").strip() or "SwoopDZ Alert"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    safe_title,
                "Tags":     "rotating_light,iphone",
                "Priority": "high",
            },
            timeout=10,
        )
        print(f"  Notification sent: {safe_title}", flush=True)
    except Exception as e:
        print(f"  ntfy error: {e}", flush=True)

# ── Supabase helpers ──────────────────────────────────────────────────────────

def is_seen(ext_id: str) -> bool:
    try:
        res = (
            supabase.table("seen_listings")
            .select("external_id")
            .eq("external_id", ext_id)
            .execute()
        )
        return len(res.data) > 0
    except Exception as e:
        print(f"  Supabase seen check error: {e}", flush=True)
        return False

def mark_seen(ext_id: str):
    try:
        supabase.table("seen_listings").insert({"external_id": ext_id}).execute()
    except Exception:
        pass

# ── AI Classification ─────────────────────────────────────────────────────────

def classify(item: dict):
    prompt = (
        f"You are an expert in the Algerian smartphone resale market.\n"
        f"Analyze this Ouedkniss listing:\n"
        f"Title: '{item['title']}'\n"
        f"Price string: '{item['price']}'\n"
        f"Raw numeric price (if available): {item.get('price_raw')}\n\n"
        f"Return ONLY a JSON object with these exact keys:\n"
        f"- model: iPhone model string (e.g. 'iPhone 13 Pro Max')\n"
        f"- price_dzd: integer price in DZD, your best estimate\n"
        f"- is_fake_price: true if price is 0, missing, or clearly placeholder\n"
        f"- market_price_dzd: integer typical resale price for this model in Algeria\n"
        f"- is_steal: true if price is more than 20000 DZD below market value\n"
        f"- reason: one sentence explanation"
    )
    for model in GROQ_MODELS:
        try:
            res = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            return json.loads(res.choices[0].message.content)
        except Exception as e:
            print(f"  Groq [{model}] failed: {e} — trying next...", flush=True)
    return None

# ── Process one item ──────────────────────────────────────────────────────────

def process_item(item: dict):
    ext_id = hashlib.md5(item["url"].encode()).hexdigest()
    if is_seen(ext_id):
        return

    print(f"  New: {item['title'][:60]} — {item['price']}", flush=True)

    ai = classify(item)
    if not ai or ai.get("is_fake_price"):
        mark_seen(ext_id)
        return

    model      = ai.get("model", "Unknown")
    price_dzd  = ai.get("price_dzd", 0)
    market     = ai.get("market_price_dzd", 0)
    is_steal   = ai.get("is_steal", False)

    print(f"    {model} | {price_dzd:,} DZD (market: {market:,}) | steal={is_steal}", flush=True)

    if is_steal:
        send_ntfy(
            f"STEAL: {model}",
            f"Price: {price_dzd:,} DZD (market ~{market:,} DZD)\n"
            f"Reason: {ai.get('reason', '')}\n"
            f"Link: {item['url']}"
        )

    try:
        supabase.table("listings").insert({
            "external_id": ext_id,
            "title":       item["title"],
            "url":         item["url"],
            "price":       price_dzd,
            "is_steal":    is_steal,
            "metadata":    ai,
        }).execute()
        mark_seen(ext_id)
    except Exception as e:
        print(f"  DB error: {e}", flush=True)

# ── Main scrape loop ──────────────────────────────────────────────────────────

MAX_PAGES = 3

def scrape_cycle():
    print(f"\n{'='*55}", flush=True)
    print(f"Starting scrape cycle — pages 1-{MAX_PAGES}", flush=True)

    for page_num in range(1, MAX_PAGES + 1):
        print(f"\nFetching page {page_num} via GraphQL...", flush=True)
        raw = fetch_listings(page=page_num)
        items = [announcement_to_item(a) for a in raw]
        print(f"  Got {len(items)} listings from API.", flush=True)

        new_count = 0
        for item in items:
            ext_id = hashlib.md5(item["url"].encode()).hexdigest()
            if not is_seen(ext_id):
                process_item(item)
                new_count += 1

        print(f"  Page {page_num}: {len(items)} total, {new_count} new.", flush=True)

    print(f"\nCycle complete. Sleeping 120s...", flush=True)

# ── Health server ─────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SwoopDZ Active")
    def log_message(self, *args):
        pass

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever(),
        daemon=True,
    ).start()

    print(f"SwoopDZ v5.0 — Ouedkniss GraphQL (no browser)", flush=True)
    print(f"Alerts -> ntfy.sh/{NTFY_TOPIC}", flush=True)

    while True:
        try:
            scrape_cycle()
        except Exception as e:
            print(f"Cycle error: {e}", flush=True)
        time.sleep(120)
