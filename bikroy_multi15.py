"""
bikroy_multi.py - Multi-instance parallel automation
Uses EXACT same scraping/posting code as bikroy_automation.py
Multiple Chrome instances search Bikroy in parallel, post 1 by 1 to Karnataka.

Usage: python bikroy_multi.py
"""

import os, re, time, requests, logging, threading, queue
from datetime import datetime
from PIL import Image
import io
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ── CONFIG ──────────────────────────────────────────────────
BIKROY_URL      = "https://bikroy.com/en/ads/bangladesh/cars?sort=date&order=desc&buy_now=0&urgent=0&type=for_sale"
KARNATAKA_BASE  = "https://november2025karnataka.dicewebfreelancers.com"
KARNATAKA_LOGIN = KARNATAKA_BASE + "/index.php/login"
KARNATAKA_MYADS = KARNATAKA_BASE + "/index.php/my-ads/user"
KARNATAKA_ADD   = KARNATAKA_BASE + "/index.php/my-ads/user/add"
USERNAME        = "Akshatha rao"
PASSWORD        = "dice@123"
IMAGE_DIR       = "downloaded_images"
LOG_FILE        = "posted_ads_log.txt"

NUM_INSTANCES   = 4    # parallel Chrome windows searching Bikroy
PAGES_PER_INST  = 25   # pages each instance handles
ADS_TO_POST     = 340 # total new ads to post then stop
# ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)
os.makedirs(IMAGE_DIR, exist_ok=True)

# Shared state
log_lock   = threading.Lock()   # for reading/writing log file
post_lock  = threading.Lock()   # only 1 instance posts at a time
titles_lock = threading.Lock()  # for shared posted_titles set
post_counter = [0]              # mutable counter shared across threads
posted_titles = set()           # shared in-memory set of posted titles


# ─────────────────────────────────────────────
# SHARED HELPERS (identical to bikroy_automation.py)
# ─────────────────────────────────────────────
def load_posted_titles():
    titles = set()
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "|" in line:
                    titles.add(line.split("|", 1)[1].strip().lower())
        log.info(f"Loaded {len(titles)} already-posted ads from log")
    except FileNotFoundError:
        log.info("No log file found - starting fresh")
    return titles


def save_to_log(title):
    with log_lock:
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                if title in f.read():
                    return
        except FileNotFoundError:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  {title}\n")
        log.info(f"  Logged: {title}")


def make_driver(headless=True):
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    else:
        opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Fix Bengali/Unicode rendering in headless mode
    opts.add_argument("--lang=en-US")
    opts.add_argument("--accept-lang=en-US,en;q=0.9")
    opts.add_argument("--force-renderer-accessibility")
    opts.add_experimental_option("prefs", {
        "intl.accept_languages": "en-US,en",
    })
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.implicitly_wait(5)
    return driver


def fill_field(driver, by, selector, value):
    try:
        el = driver.find_element(by, selector)
        el.clear()
        el.send_keys(str(value))
    except Exception as e:
        log.warning(f"  Could not fill {selector}: {e}")


def download_images(image_urls, folder):
    import urllib3
    urllib3.disable_warnings()  # suppress SSL warnings
    paths = []
    os.makedirs(folder, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for i, url in enumerate(image_urls):
        try:
            url = re.sub(r'_[0-9]+x[0-9]+', '_1200x900', url).split('?')[0]
            resp = requests.get(url, timeout=15, headers=headers, verify=False)
            resp.raise_for_status()
            path = os.path.join(folder, f"img_{i+1}.jpg")
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.save(path, "JPEG", quality=95)
            paths.append(os.path.abspath(path))
        except Exception as e:
            log.warning(f"  Image download failed: {e}")
    return paths


# ─────────────────────────────────────────────
# SCRAPE - identical to bikroy_automation.py
# ─────────────────────────────────────────────
def scrape_bikroy_ad(driver, ad_url):
    driver.get(ad_url)
    time.sleep(2)
    data = {}

    # Title
    try:
        data["title"] = driver.find_element(By.CSS_SELECTOR, "h1.title-text, h1").text.strip()
    except:
        data["title"] = ""

    # Details table
    detail_map = {
        "Brand": "brand", "Model": "model", "Trim / Edition": "trim",
        "Year of Manufacture": "year_manufacture", "Registration Year": "registration_year",
        "Year of Production": "year_production", "Condition": "condition",
        "Transmission": "transmission", "Body type": "body_type",
        "Fuel type": "fuel_type", "Engine capacity": "engine_capacity",
        "Kilometers run": "km_run",
    }
    rows = driver.find_elements(By.CSS_SELECTOR, "div[class*='full-width']")
    for row in rows:
        try:
            label_el = row.find_elements(By.CSS_SELECTOR, "div[class*='label']")
            value_el = row.find_elements(By.CSS_SELECTOR, "div[class*='value']")
            if label_el and value_el:
                key = label_el[0].text.strip().rstrip(":")
                val = value_el[0].text.strip()
                if key in detail_map:
                    data[detail_map[key]] = val
        except:
            pass

    # Price - confirmed selector: div[class*="amount--3NTpl"]
    try:
        price_text = driver.execute_script("""
            var el = document.querySelector('div[class*="amount--3NTpl"]');
            if (el) return el.innerText.trim();
            var els = ['div[class*="price-section"] div[class*="amount"]',
                       'strong[class*="price"]', 'div[class*="price--"]'];
            for (var s of els) {
                var e = document.querySelector(s);
                if (e && e.innerText.includes('Tk')) return e.innerText.trim();
            }
            return '';
        """)
        if price_text:
            price_clean = price_text.replace("Tk", "").replace("Negotiable", "").strip()
            price_clean = re.sub(r"[^\d,]", "", price_clean).strip(",")
            data["price"] = price_clean.replace(",", "") if price_clean else "0"
        else:
            data["price"] = "0"
    except:
        data["price"] = "0"

    # Price Final Status - "Negotiable" if shown on Bikroy, else "."
    try:
        price_section = driver.execute_script("""
            var el = document.querySelector('div[class*="price-section"]');
            return el ? el.innerText : '';
        """)
        data["price_status"] = "Negotiable" if "Negotiable" in (price_section or "") else "."
    except:
        data["price_status"] = "."

    # Version - separate field from Trim/Edition
    try:
        version_text = driver.execute_script("""
            var rows = document.querySelectorAll('div[class*="full-width"]');
            for (var row of rows) {
                var label = row.querySelector('div[class*="label"]');
                var value = row.querySelector('div[class*="value"]');
                if (label && value && label.innerText.trim() === 'Version')
                    return value.innerText.trim();
            }
            return '';
        """)
        data["version"] = version_text if version_text else "."
    except:
        data["version"] = "."

    # Description - click Show more first, then grab full text with UTF-8
    try:
        # Step 1: Click the Show more button to expand hidden content
        try:
            driver.execute_script("""
                document.querySelectorAll('button, span, a').forEach(function(el) {
                    var t = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (t === 'show more' || t === 'see more' || t === 'read more') {
                        el.click();
                    }
                });
            """)
            time.sleep(1.5)
        except:
            pass

        # Step 2: Remove the Show more button from DOM, then get full innerText
        full_desc = driver.execute_script("""
            var selectors = [
                'div[class*="description-section"]',
                'div[class*="collapsible-content"]',
                'div[class*="description--"]',
                'div[class*="description-text"]',
                'div[class*="expandable"]',
                'div[class*="collapsible"]'
            ];
            for (var s of selectors) {
                var el = document.querySelector(s);
                if (el && el.innerText.trim().length > 5) {
                    // Fully expand
                    el.style.maxHeight = 'none';
                    el.style.height = 'auto';
                    el.style.overflow = 'visible';
                    el.querySelectorAll('*').forEach(function(c) {
                        c.style.maxHeight = 'none';
                        c.style.overflow = 'visible';
                    });
                    // Remove Show more / See more buttons before reading text
                    el.querySelectorAll('button, span, a').forEach(function(b) {
                        var t = (b.innerText || b.textContent || '').trim().toLowerCase();
                        if (t === 'show more' || t === 'see more' || t === 'read more') {
                            b.remove();
                        }
                    });
                    return el.innerText.trim();
                }
            }
            return '';
        """)

        # Fallback: get plain text line by line
        if not full_desc or len(full_desc.strip()) < 5:
            paras = driver.find_elements(By.CSS_SELECTOR,
                "div[class*='description'] p, div[class*='list'] p")
            full_desc = "\n".join([p.text.strip() for p in paras if p.text.strip()])

        data["description"] = full_desc if full_desc else "."
    except:
        data["description"] = "."

    # Seller name
    try:
        data["seller_name"] = driver.find_element(By.CSS_SELECTOR, "div[class*='contact-name']").text.strip()
    except:
        data["seller_name"] = ""

    # Phone
    try:
        btn = driver.find_element(By.CSS_SELECTOR, "button[class*='contact-section']")
        btn.click()
        time.sleep(2)
        phones = driver.find_elements(By.CSS_SELECTOR, "div[class*='phone-numbers--2C']")
        data["phone"] = ", ".join([p.text.strip() for p in phones if p.text.strip()])
    except:
        data["phone"] = ""

    # Posted date
    try:
        subtitle = driver.find_element(By.CSS_SELECTOR, "div[class*='subtitle-wrapper'], div[class*='sub-title']").text.strip()
        if "Posted on" in subtitle:
            data["posted_on"] = subtitle.replace("Posted on", "").strip().split(",")[0].strip()
        else:
            data["posted_on"] = subtitle.split(",")[0].strip()
    except:
        data["posted_on"] = ""

    # Location
    try:
        loc_parts = driver.find_elements(By.CSS_SELECTOR, "div[class*='subtitle-wrapper'] a[class*='location-link']")
        data["location"] = ", ".join([l.text.strip() for l in loc_parts if l.text.strip()])
    except:
        data["location"] = "Dhaka"

    data["source_link"] = ad_url

    # Images - click each thumbnail li to load full res in main viewer
    img_urls = []
    try:
        # Confirmed from Bikroy HTML: thumbnails are li inside ul[class*="thumbnail-list"]
        thumbnail_items = driver.find_elements(By.CSS_SELECTOR,
            "ul[class*='thumbnail-list'] li")

        seen_urls = set()

        def grab_main_img():
            """Get full-res src from main image container - confirmed: button[class*='main-image-container'] img"""
            for sel in [
                "button[class*='main-image-container'] img",
                "button[class*='selected-image'] img",
                "div[class*='gallery-wrapper'] button img",
                "div[class*='gallery'] > button img",
            ]:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    src = el.get_attribute("src") or ""
                    # Remove /cropped.jpg or similar suffix, upgrade resolution
                    src = re.sub(r"/cropped\.jpg$", ".jpg", src)
                    src = re.sub(r"_[0-9]+x[0-9]+", "_1200x900", src).split("?")[0]
                    if src and "bikroy" in src and src not in seen_urls:
                        seen_urls.add(src)
                        return src
                except:
                    pass
            return None

        # Grab first image already showing
        first = grab_main_img()
        if first:
            img_urls.append(first)

        # Click each thumbnail li - max 3 more clicks = 4 images total
        for item in thumbnail_items[:3]:
            try:
                driver.execute_script("arguments[0].click();", item)
                time.sleep(1)
                src = grab_main_img()
                if src:
                    img_urls.append(src)
            except:
                pass

    except:
        img_urls = []

    data["image_urls"] = img_urls
    return data


# ─────────────────────────────────────────────
# POST - identical to bikroy_automation.py
# ─────────────────────────────────────────────
def login_karnataka(driver):
    driver.get(KARNATAKA_LOGIN)
    time.sleep(2)
    wait = WebDriverWait(driver, 15)
    wait.until(EC.presence_of_element_located((By.NAME, "username")))
    driver.find_element(By.NAME, "username").clear()
    driver.find_element(By.NAME, "username").send_keys(USERNAME)
    driver.find_element(By.NAME, "password").clear()
    driver.find_element(By.NAME, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']").click()
    time.sleep(1)


def post_ad_karnataka(driver, data, image_paths):
    # Retry page load up to 2 times if title field not found
    for page_attempt in range(2):
        driver.get(KARNATAKA_ADD)
        time.sleep(3)
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "title"))
            )
            break  # page loaded OK
        except:
            log.warning(f"  Page load attempt {page_attempt+1} failed - retrying...")
            # If session expired, re-login and try again
            if "login" in driver.current_url.lower() or page_attempt == 1:
                log.warning("  Session expired - re-logging in...")
                login_karnataka(driver)
                driver.get(KARNATAKA_ADD)
                time.sleep(3)
    else:
        log.error("  Could not load add page after retries - skipping this ad")
        return False

    wait = WebDriverWait(driver, 15)

    fill_field(driver, By.ID, "title", data.get("title", ""))

    # Categories - retry up to 3 times, WebDriverWait for each AJAX level
    cat_loaded = False
    for cat_attempt in range(3):
        try:
            cat1 = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "select[name='category[]']")))
            Select(cat1).select_by_value("6")

            WebDriverWait(driver, 15).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "select[name='category[]']")) >= 2)
            cats = driver.find_elements(By.CSS_SELECTOR, "select[name='category[]']")
            Select(cats[1]).select_by_value("8")

            WebDriverWait(driver, 15).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "select[name='category[]']")) >= 3)
            cats = driver.find_elements(By.CSS_SELECTOR, "select[name='category[]']")
            Select(cats[2]).select_by_value("31")

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.NAME, "exf_8")))
            cat_loaded = True
            log.info(f"  Categories loaded (attempt {cat_attempt+1})")
            break

        except Exception as e:
            log.warning(f"  Category attempt {cat_attempt+1} failed: {e} — reloading...")
            driver.get(KARNATAKA_ADD)
            time.sleep(4)
            fill_field(driver, By.ID, "title", data.get("title", ""))

    if not cat_loaded:
        log.error("  Categories failed after 3 attempts - skipping ad")
        return False

    def val(key, default="."):
        return data.get(key, "") or default

    fill_field(driver, By.NAME, "exf_8",  val("trim"))
    fill_field(driver, By.NAME, "exf_9",  val("transmission"))
    fill_field(driver, By.NAME, "exf_10", val("registration_year"))
    fill_field(driver, By.NAME, "exf_11", val("fuel_type"))
    fill_field(driver, By.NAME, "exf_12", val("km_run"))
    fill_field(driver, By.NAME, "exf_13", val("model"))
    fill_field(driver, By.NAME, "exf_14", val("year_manufacture"))
    fill_field(driver, By.NAME, "exf_15", val("condition"))
    fill_field(driver, By.NAME, "exf_16", val("body_type"))
    fill_field(driver, By.NAME, "exf_17", val("price_status"))  # Negotiable or "."
    fill_field(driver, By.NAME, "exf_18", val("engine_capacity"))
    fill_field(driver, By.NAME, "exf_19", val("posted_on"))
    fill_field(driver, By.NAME, "exf_20", val("seller_name"))
    fill_field(driver, By.NAME, "exf_21", val("phone"))
    fill_field(driver, By.NAME, "exf_22", val("source_link"))
    fill_field(driver, By.NAME, "exf_23", val("year_production"))
    fill_field(driver, By.NAME, "exf_24", val("version"))  # Version field separate from Trim

    # Price - remove commas, Karnataka field only accepts plain number
    try:
        price_plain = (data.get("price", "0") or "0").replace(",", "")
        driver.find_element(By.NAME, "price").clear()
        driver.find_element(By.NAME, "price").send_keys(price_plain)
    except: pass

    # Description
    desc_content = data.get("description", "") or "."
    try:
        driver.execute_script("tinymce.activeEditor.setContent(arguments[0]);", desc_content)
        log.info("  Description set via TinyMCE")
    except:
        try:
            toggle = driver.find_element(By.XPATH, "//button[contains(text(),'Toggle editor')]")
            toggle.click(); time.sleep(1)
            ta = driver.find_element(By.NAME, "description")
            driver.execute_script("arguments[0].value = arguments[1];", ta, desc_content)
        except: pass

    # Images
    if image_paths:
        try:
            upload_input = driver.find_element(By.CSS_SELECTOR, "input[type='file']")
            upload_input.send_keys("\n".join(image_paths))
            time.sleep(2)
        except Exception as e:
            log.warning(f"  Image upload failed: {e}")

    # Tag, Price+Currency, Address, Location
    try: Select(driver.find_element(By.CSS_SELECTOR, "select[name='tagid']")).select_by_value("1")
    except: pass
    try:
        price_plain = (data.get("price", "0") or "0").replace(",", "")
        pf = driver.find_element(By.NAME, "price")
        pf.clear()
        pf.send_keys(price_plain)
        Select(driver.find_element(By.CSS_SELECTOR, "select[name='currency']")).select_by_value("TK")
        log.info(f"  Price: {price_plain} TK")
    except: pass
    try:
        fill_field(driver, By.NAME, "address", data.get("location", "Dhaka"))
        Select(driver.find_element(By.CSS_SELECTOR, "select[name='location[]']")).select_by_value("33")
    except: pass

    # Privacy checkbox
    try:
        cb = driver.find_element(By.CSS_SELECTOR, "input[name='privacy[]']")
        if not cb.is_selected(): driver.execute_script("arguments[0].click();", cb)
    except: pass

    # Wait for uploads
    for _ in range(20):
        if not driver.find_elements(By.CSS_SELECTOR, "div[class*='loading'], div[class*='spinner']"):
            break
        time.sleep(1)
    time.sleep(2)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)

    # Re-tick privacy after scroll
    try:
        cb = driver.find_element(By.CSS_SELECTOR, "input[name='privacy[]']")
        if not cb.is_selected(): driver.execute_script("arguments[0].click();", cb)
        log.info(f"  Privacy checked: {cb.is_selected()}")
    except: pass

    # Submit with retry
    for attempt in range(3):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button.btn-success")
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(3)
        except Exception as e:
            log.warning(f"  Click failed: {e}")
        try:
            alert = driver.switch_to.alert
            log.warning(f"  Alert: {alert.text} — retrying")
            alert.accept(); time.sleep(3); continue
        except: pass
        if "add" not in driver.current_url:
            log.info(f"  ✅ Posted: {data.get('title')}")
            return True
        log.warning(f"  Still on add page, attempt {attempt+1}")

    log.error("  ❌ All submit attempts failed")
    return False


# ─────────────────────────────────────────────
# WORKER - each instance runs this
# ─────────────────────────────────────────────
def worker(inst_id, start_page, end_page):
    prefix = f"[Inst{inst_id}]"
    log.info(f"{prefix} Starting — Bikroy pages {start_page} to {end_page}")

    driver = make_driver(headless=True)  # set False to see browser
    try:
        login_karnataka(driver)
        log.info(f"{prefix} Logged in to Karnataka")

        for page_num in range(start_page, end_page + 1):

            # Stop if total posted ads reached
            with titles_lock:
                if post_counter[0] >= ADS_TO_POST:
                    log.info(f"{prefix} Target reached, stopping.")
                    break

            # Load Bikroy listing page and get all titles+urls via JS
            page_url = f"{BIKROY_URL}&page={page_num}"
            driver.get(page_url)
            time.sleep(2)

            page_data = driver.execute_script("""
                var items = [];
                document.querySelectorAll("a[class*='card-link'][href*='/en/ad/']").forEach(function(a) {
                    var titleEl = a.querySelector("h2, div[class*='heading'], div[class*='title'], [class*='name']");
                    items.push({ url: a.href, title: titleEl ? titleEl.innerText.trim() : "" });
                });
                return items;
            """)

            if not page_data:
                log.info(f"{prefix} No ads on page {page_num}, stopping.")
                break

            log.info(f"{prefix} Page {page_num}: {len(page_data)} ads found")

            for ad in page_data:

                with titles_lock:
                    if post_counter[0] >= ADS_TO_POST:
                        break
                    # Instant skip using shared memory
                    if ad["title"] and ad["title"].lower() in posted_titles:
                        log.info(f"{prefix} ⚡ Skip: {ad['title']}")
                        continue

                # Scrape full ad
                data = scrape_bikroy_ad(driver, ad["url"])
                if not data.get("title"):
                    continue

                # Check again after scraping
                with titles_lock:
                    if data["title"].lower() in posted_titles:
                        log.info(f"{prefix} ⚡ Skip (scraped): {data['title']}")
                        continue

                # POST — only 1 at a time across all instances
                # POST — only 1 at a time (images downloaded INSIDE lock)
                with post_lock:
                    safe = re.sub(r'[\/*?"<>|]', "", data["title"])[:50]
                    img_paths = download_images(
                        data["image_urls"],
                        os.path.join(IMAGE_DIR, f"inst{inst_id}_{safe}")
                    )
                    safe = re.sub(r'[\\/*?:"<>|]', "", data["title"])[:50]
                    img_paths = download_images(
                        data["image_urls"],
                        os.path.join(IMAGE_DIR, f"inst{inst_id}_{safe}")
                    )
                    # Check one more time inside post lock
                    with titles_lock:
                        if data["title"].lower() in posted_titles:
                            continue
                        if post_counter[0] >= ADS_TO_POST:
                            break

                    log.info(f"{prefix} 📝 Posting: {data['title']}")
                    success = post_ad_karnataka(driver, data, img_paths)

                    if success:
                        with titles_lock:
                            posted_titles.add(data["title"].lower())
                            post_counter[0] += 1
                        save_to_log(data["title"])
                        log.info(f"{prefix} ✅ [{post_counter[0]}/{ADS_TO_POST}] {data['title']}")

    finally:
        driver.quit()
        log.info(f"{prefix} Browser closed")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    global posted_titles

    # Load existing log
    posted_titles = load_posted_titles()

    log.info(f"Starting {NUM_INSTANCES} parallel instances...")
    log.info(f"Each covers {PAGES_PER_INST} pages | Target: {ADS_TO_POST} new posts")

    threads = []
    for i in range(NUM_INSTANCES):
        start_page = i * PAGES_PER_INST + 1
        end_page   = start_page + PAGES_PER_INST - 1
        t = threading.Thread(
            target=worker,
            args=(i + 1, start_page, end_page),
            name=f"inst{i+1}"
        )
        threads.append(t)
        t.start()
        time.sleep(4)  # stagger browser starts

    for t in threads:
        t.join()

    log.info(f"\n✅ Done! Total posted: {post_counter[0]}")


if __name__ == "__main__":
    main()
