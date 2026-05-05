"""
Debug v2: Properly waits for Vue.js search results on Ouedkniss
and dumps all found links + a screenshot after scroll.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

SEARCH_URLS = [
    "https://www.ouedkniss.com/s/1?keywords=iphone",
    "https://www.ouedkniss.com/chercher?keywords=iphone",
    "https://www.ouedkniss.com/recherche?keywords=iphone",
    "https://www.ouedkniss.com/electronics/telephone-portable/iphone",
]

def scan_page(page, label):
    print(f"\n--- Scanning: {label} ---", flush=True)
    # Scroll to trigger lazy loading
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    page.wait_for_timeout(3000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(3000)

    links = page.query_selector_all("a[href]")
    print(f"Total links: {len(links)}", flush=True)

    for link in links:
        try:
            href = link.get_attribute("href") or ""
            text = link.inner_text().strip()[:80]
            if href and len(text) > 3:
                print(f"  HREF: {href!r:70s} | TEXT: {text!r}", flush=True)
        except Exception:
            pass

    page.screenshot(path=f"debug_{label}.png", full_page=False)
    print(f"Screenshot saved: debug_{label}.png", flush=True)

    with open(f"debug_{label}.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    print(f"HTML saved: debug_{label}.html ({len(page.content())} chars)", flush=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="fr-DZ",
        timezone_id="Africa/Algiers",
        viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()

    # Try URL 1: Direct search with wait for dynamic content
    url = "https://www.ouedkniss.com/s/1?keywords=iphone"
    print(f"Navigating to {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Give Vue router time to process
    print("Waiting 8 seconds for Vue to render search results...", flush=True)
    page.wait_for_timeout(8000)

    scan_page(page, "search_v1")

    # Now try using the search box directly
    print("\nTrying via search box interaction...", flush=True)
    page.goto("https://www.ouedkniss.com", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    try:
        search_input = page.query_selector("input[type='search'], input[placeholder*='Recherche'], input[placeholder*='Cherchez'], .ok-search input, [class*='search'] input")
        if search_input:
            search_input.click()
            search_input.fill("iphone")
            page.keyboard.press("Enter")
            print("Typed 'iphone' and pressed Enter", flush=True)
            page.wait_for_timeout(8000)
            scan_page(page, "search_via_input")
        else:
            print("Could not find search input", flush=True)
    except Exception as e:
        print(f"Search box interaction failed: {e}", flush=True)

    browser.close()
print("\nDone!", flush=True)
