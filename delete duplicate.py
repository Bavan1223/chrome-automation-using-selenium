"""
delete_duplicates.py
For each duplicate title from Excel:
  - Searches it on the portal using the search box
  - If more than 1 result found, deletes all extras (keeps only 1)
Usage: python delete_duplicates.py
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import pandas as pd
import time

# ── CONFIG ──────────────────────────────────────────────────
KARNATAKA_BASE = "https://november2025karnataka.dicewebfreelancers.com"
USERNAME       = "Akshatha rao"
PASSWORD       = "dice@123"
MY_ADS_URL     = KARNATAKA_BASE + "/index.php/my-ads"
EXCEL_FILE     = "duplicate_names_column_b.xlsx"
# ────────────────────────────────────────────────────────────

def load_duplicate_titles(excel_file):
    df = pd.read_excel(excel_file, header=0)
    col = df.iloc[:, 0]
    titles = [str(t).strip() for t in col.dropna()
              if str(t).strip() and str(t).strip().lower() != 'duplicate names']
    print(f"Loaded {len(titles)} duplicate titles from Excel\n")
    return titles

def make_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver

def login(driver):
    print("Logging in...")
    driver.get(KARNATAKA_BASE + "/index.php/login")
    time.sleep(3)
    driver.find_element(By.NAME, "username").send_keys(USERNAME)
    driver.find_element(By.NAME, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()
    time.sleep(3)
    print("Logged in!\n")

def search_title(driver, title):
    """Type title into the My Ads search box and submit."""
    driver.get(MY_ADS_URL)
    time.sleep(2)
    try:
        # Exact selectors from DevTools inspection:
        # input[type="text"][name="search"][id="search"][placeholder="Filter by title"]
        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "search"))
        )
        search_box.clear()
        time.sleep(0.3)
        search_box.send_keys(title)
        time.sleep(0.5)

        # Button: button[type="button"][onclick="this.form.submit()"]
        search_btn = driver.find_element(By.CSS_SELECTOR,
            "button[onclick*='form.submit']"
        )
        search_btn.click()
        time.sleep(2)
    except Exception as e:
        print(f"    ⚠ Search box error: {e}")

def get_delete_buttons(driver):
    """Return all Selenium delete button elements currently visible on the page."""
    return driver.find_elements(By.CSS_SELECTOR,
        'a[href*="delete"], a[title="Delete"]')

def click_delete_and_confirm(driver, btn):
    """Click a trash icon element and accept the confirm dialog."""
    try:
        driver.execute_script("arguments[0].scrollIntoView(true);", btn)
        time.sleep(0.3)
        btn.click()
        time.sleep(1)
        alert = WebDriverWait(driver, 6).until(EC.alert_is_present())
        alert.accept()
        time.sleep(2)
        return True
    except Exception as e:
        print(f"    ⚠ Click/confirm error: {e}")
        return False

def main():
    duplicate_titles = load_duplicate_titles(EXCEL_FILE)
    driver = make_driver()
    total_deleted = 0

    try:
        login(driver)

        for i, title in enumerate(duplicate_titles, 1):
            print(f"[{i}/{len(duplicate_titles)}] Searching: {title[:70]}")

            # Search for this title
            search_title(driver, title)

            # Count how many delete buttons appear (= how many copies exist)
            delete_btns = get_delete_buttons(driver)
            count = len(delete_btns)

            if count == 0:
                print(f"  → Not found on portal, skipping")
                continue
            elif count == 1:
                print(f"  → Only 1 copy found, nothing to delete")
                continue
            else:
                print(f"  → Found {count} copies — deleting {count - 1} duplicate(s)")

            # Delete all except the first one (keep 1, delete the rest)
            deleted_this = 0
            extras = count - 1   # number to delete

            for d in range(extras):
                # Re-search each time so buttons are fresh
                search_title(driver, title)
                time.sleep(1)
                btns = get_delete_buttons(driver)

                if len(btns) <= 1:
                    print(f"    → Only 1 left, stopping")
                    break

                # Always delete the LAST button (keep the first)
                target_btn = btns[-1]
                success = click_delete_and_confirm(driver, target_btn)
                if success:
                    deleted_this += 1
                    total_deleted += 1
                    print(f"    ✓ Deleted copy {d+1}/{extras}")
                else:
                    print(f"    ✗ Failed to delete copy {d+1}, skipping")
                    break

            print(f"  → Done: deleted {deleted_this} duplicate(s) of '{title[:50]}'")

        print(f"\n{'='*55}")
        print(f"✅ Finished! Total duplicates deleted: {total_deleted}")
        print(f"{'='*55}")

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
