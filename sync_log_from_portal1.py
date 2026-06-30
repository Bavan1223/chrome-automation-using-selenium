"""
sync_log_from_portal.py
Scrapes ALL ad titles from the Karnataka portal (up to 50 ads per page)
and saves them to posted_ads_log.txt
Usage: python sync_log_from_portal.py
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────
KARNATAKA_BASE = "https://november2025karnataka.dicewebfreelancers.com"
USERNAME       = "Ramya Krishna"
PASSWORD       = "dice@123"
LOG_FILE       = "ammu_posted_ads_log.txt"
MY_ADS_URL     = KARNATAKA_BASE + "/index.php/my-ads"
ADS_PER_PAGE   = 50
# ────────────────────────────────────────────────────────────

def main():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts
    )

    try:
        # ── Login ──
        print("Logging in...")
        driver.get(KARNATAKA_BASE + "/index.php/login")
        time.sleep(3)
        driver.find_element(By.NAME, "username").send_keys(USERNAME)
        driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()
        time.sleep(3)
        print("Logged in!\n")

        all_titles = []
        page = 1

        while True:
            offset = (page - 1) * ADS_PER_PAGE
            url = f"{MY_ADS_URL}?start={offset}"
            driver.get(url)
            time.sleep(2)
            print(f"── Page {page} (offset={offset}) ──")

            # Extract titles using multiple selectors
            titles = []
            for selector in [
                "h3 a", "h2 a",
                ".jomcl-col-content h3 a",
                "div[class*='col-content'] h3 a",
                "div[class*='col-content'] a",
                "td.jomcl-col-content a",
            ]:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
                found = [e.text.strip() for e in els if e.text.strip() and len(e.text.strip()) > 3]
                if found:
                    titles = found
                    print(f"  Selector '{selector}' → {len(titles)} titles")
                    break

            # JS fallback
            if not titles:
                titles = driver.execute_script("""
                    var all = [];
                    document.querySelectorAll('a[href*="advert"]').forEach(function(a) {
                        var t = (a.innerText || a.textContent || '').trim();
                        if (t.length > 3) all.push(t);
                    });
                    return [...new Set(all)];
                """)
                print(f"  JS fallback → {len(titles)} titles")

            if not titles:
                print(f"  No titles found on page {page} — stopping.")
                break

            # Print titles
            for i, title in enumerate(titles, 1):
                print(f"  {(page-1)*ADS_PER_PAGE + i:>4}. {title}")

            all_titles.extend(titles)

            # ── Stop condition: NO Next button (not ad count) ──
            next_btn = driver.find_elements(By.CSS_SELECTOR,
                "a[title='Next'], .pagination-next a, li.next a, a[rel='next']")
            if not next_btn:
                print(f"\n  No Next button — this is the last page.")
                break

            page += 1

        # ── Write to log ──
        print(f"\n{'='*50}")
        print(f"Total titles found: {len(all_titles)}")
        print(f"{'='*50}\n")

        if all_titles:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                for title in all_titles:
                    f.write(f"{timestamp}  |  {title}\n")
            print(f"✅ Saved {len(all_titles)} titles to '{LOG_FILE}'")
        else:
            print("⚠️  No titles found.")

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
