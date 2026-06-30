"""
trademe_multi.py - TradeMe NZ → Karnataka automation
Scrapes TradeMe Motors NZ listings and posts them to Karnataka site.
Skips any listing that has NO asking price, starting price, or buy-now price.

Usage: python trademe_multi.py
"""

import os, re, time, requests, logging, threading
from datetime import datetime
from PIL import Image
import io
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ── CONFIG ──────────────────────────────────────────────────
TRADEME_SEARCH  = (
    "https://www.trademe.co.nz/a/motors/cars/search"
    "?price_min=1000&year_min=2025&year_max=2025&user_region=70&safety_rating=10"
)
KARNATAKA_BASE  = "https://november2025karnataka.dicewebfreelancers.com"
KARNATAKA_LOGIN = KARNATAKA_BASE + "/index.php/login"
KARNATAKA_MYADS = KARNATAKA_BASE + "/index.php/my-ads/user"
KARNATAKA_ADD   = KARNATAKA_BASE + "/index.php/my-ads/user/add"
USERNAME        = "Ramya Krishna"
PASSWORD        = "dice@123"
IMAGE_DIR       = "downloaded_images"
LOG_FILE        = "ammu_posted_ads_log.txt"

NUM_INSTANCES   = 3    # parallel Chrome windows
PAGES_PER_INST  = 30   # pages each instance handles
ADS_TO_POST     = 200  # total new ads to post then stop
# ────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)
os.makedirs(IMAGE_DIR, exist_ok=True)

# Shared state
log_lock      = threading.Lock()
post_lock     = threading.Lock()
titles_lock   = threading.Lock()
post_counter  = [0]
posted_titles = set()


# ─────────────────────────────────────────────
# HELPERS
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


def make_driver(headless=False):
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
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--accept-lang=en-US,en;q=0.9")
    opts.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
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


def parse_nzd_price(text):
    """Extract price from NZD string like '$27,950' → '$27,950' (keep $ and commas for display)"""
    if not text:
        return None
    text = str(text).strip()
    # If already has $, clean up whitespace and return formatted
    m = re.search(r'\$[\d,]+', text)
    if m:
        return m.group(0)  # e.g. "$69,990"
    # Fallback: just digits → add $
    digits = re.sub(r"[^\d]", "", text)
    return f"${int(digits):,}" if digits else None


def download_images(image_urls, folder):
    import urllib3
    urllib3.disable_warnings()
    paths = []
    os.makedirs(folder, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for i, url in enumerate(image_urls):
        try:
            resp = requests.get(url, timeout=15, headers=headers, verify=False)
            resp.raise_for_status()
            path = os.path.join(folder, f"img_{i+1}.jpg")
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.save(path, "JPEG", quality=95)
            paths.append(os.path.abspath(path))
        except Exception as e:
            log.warning(f"  Image download failed ({url}): {e}")
    return paths


# ─────────────────────────────────────────────
# SCRAPE TRADEME LISTING
# ─────────────────────────────────────────────
def scrape_trademe_ad(driver, ad_url):
    driver.get(ad_url)
    time.sleep(3)
    data = {"source_link": ad_url}

    # ── Title ──
    try:
        data["title"] = driver.execute_script("""
            var el = document.querySelector('h1[class*="listing-title"], h1.tm-motors-listing-title, h1');
            return el ? el.innerText.trim() : '';
        """)
    except:
        data["title"] = ""

    # ── Price: Asking / Starting (auction) / Buy Now ──
    # Confirmed selectors from DevTools:
    #   Listing page:  div.tm-motors-pricing-box__price  (price value)
    #                  sibling div.tm-motors-pricing-box__price-display (label text)
    #   Search page:   div.tm-search-card-price__price   (price value)
    #                  parent div.tm-search-card-price__container has label sibling
    # If NONE of the 3 price types are found → skip this ad entirely.
    # NOTE: price is only used to decide skip/keep — it is NOT posted to Karnataka.
    data["asking_price"]   = None
    data["starting_price"] = None
    data["buy_now_price"]  = None

    try:
        price_js = driver.execute_script("""
            var result = {asking: null, starting: null, buyNow: null};

            // ── Primary: confirmed listing-page selectors from DevTools ──
            // The pricing box contains a label div and a price div as siblings
            // Label:  div.tm-motors-pricing-box__price-display  e.g. "Asking price"
            // Value:  div.tm-motors-pricing-box__price           e.g. "$83,990"
            var containers = document.querySelectorAll(
                'div.tm-motors-pricing-box__container, ' +
                'div[class*="tm-motors-pricing-box__container"], ' +
                'tm-pricing-box, div[class*="tm-auction-pricing-box"]'
            );
            containers.forEach(function(box) {
                var labelEl = box.querySelector(
                    'div[class*="tm-motors-pricing-box__price-display"], ' +
                    'div[class*="price-display"], div[class*="price__display"]'
                );
                var priceEl = box.querySelector(
                    'div[class*="tm-motors-pricing-box__price"]:not([class*="display"]), ' +
                    'div[class*="pricing-box__price"]:not([class*="display"])'
                );
                if (!labelEl || !priceEl) return;
                var label = labelEl.innerText.trim().toLowerCase();
                var price = priceEl.innerText.trim();
                if (/asking/.test(label) && !result.asking)   result.asking  = price;
                if (/starting|start/.test(label) && !result.starting) result.starting = price;
                if (/buy.?now/.test(label) && !result.buyNow) result.buyNow  = price;
            });

            // ── Fallback: search-card price (search results page context) ──
            if (!result.asking && !result.starting && !result.buyNow) {
                var cardBox = document.querySelector(
                    'div[class*="tm-search-card-price__container"], ' +
                    'div[class*="tm-tier-one-search-card__price"]'
                );
                if (cardBox) {
                    var txt = cardBox.innerText;
                    var m;
                    m = txt.match(/Asking price[\\s\\S]*?(\\$[\\d,]+)/i);
                    if (m) result.asking = m[1];
                    m = txt.match(/Starting price[\\s\\S]*?(\\$[\\d,]+)/i);
                    if (m) result.starting = m[1];
                    m = txt.match(/Buy [Nn]ow[\\s\\S]*?(\\$[\\d,]+)/i);
                    if (m) result.buyNow = m[1];
                }
            }

            // ── Last resort: scan full page text of pricing section ──
            if (!result.asking && !result.starting && !result.buyNow) {
                var anyBox = document.querySelector(
                    'div[class*="pricing"], div[class*="price-box"], ' +
                    'div[class*="contact-box"], div[class*="listing-contact"]'
                );
                if (anyBox) {
                    var t = anyBox.innerText;
                    var ma = t.match(/Asking price[\\s\\S]{0,20}(\\$[\\d,]+)/i);
                    if (ma) result.asking = ma[1];
                    var ms = t.match(/Starting price[\\s\\S]{0,20}(\\$[\\d,]+)/i);
                    if (ms) result.starting = ms[1];
                    var mb = t.match(/Buy [Nn]ow[\\s\\S]{0,20}(\\$[\\d,]+)/i);
                    if (mb) result.buyNow = mb[1];
                }
            }

            return result;
        """)

        if price_js:
            data["asking_price"]   = parse_nzd_price(price_js.get("asking"))
            data["starting_price"] = parse_nzd_price(price_js.get("starting"))
            data["buy_now_price"]  = parse_nzd_price(price_js.get("buyNow"))

    except Exception as e:
        log.warning(f"  Price extraction error: {e}")

    # ── SKIP if none of the 3 price types found ──
    if not any([data["asking_price"], data["starting_price"], data["buy_now_price"]]):
        log.info(f"  ⏭ No price (asking/starting/buy-now) — skipping: {data.get('title', ad_url)}")
        return None

    # Store which price type was found (for logging only — NOT posted to Karnataka)
    data["price"] = data["asking_price"] or data["starting_price"] or data["buy_now_price"]
    data["price_label"] = (
        "Asking price"   if data["asking_price"]  else
        "Starting price" if data["starting_price"] else
        "Buy Now"
    )
    log.info(f"  {data['price_label']}: ${data['price']}")

    # ── On Road Costs — confirmed from Image 2: div[class*="tm-orc-description__value"] ──
    try:
        orc = driver.execute_script("""
            var el = document.querySelector('div[class*="tm-orc-description__value"]');
            if (el) return el.innerText.trim();
            // fallback: "On road costs" label sibling
            var headers = document.querySelectorAll('div[class*="tm-orc-description__header"]');
            for (var i=0; i<headers.length; i++) {
                if (/on road costs/i.test(headers[i].innerText)) {
                    var par = headers[i].parentElement;
                    if (par) { var v = par.querySelector('[class*="value"]'); if (v) return v.innerText.trim(); }
                }
            }
            return null;
        """)
        if orc:
            data["on_road_costs"] = orc
            log.info(f"  On road costs: {orc}")
    except Exception as e:
        log.warning(f"  ORC extraction error: {e}")

    # ── Vehicle specs via Vehicle Information modal / tab ──
    # Click "Show all basic details" or "Features" tab if present
    try:
        driver.execute_script("""
            var btns = document.querySelectorAll('button, a');
            for (var b of btns) {
                var t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('show all') || t.includes('basic details') || t.includes('all details'))
                    b.click();
            }
        """)
        time.sleep(1.5)
    except:
        pass

    spec_map = {
        "odometer":        "km_run",
        "kilometres":      "km_run",
        "body":            "body_type",
        "body style":      "body_type",
        "fuel type":       "fuel_type",
        "transmission":    "transmission",
        "engine":          "engine_capacity",
        "engine size":     "engine_capacity",
        "doors":           "doors",
        "seats":           "seats",
        "exterior colour": "colour",
        "colour":          "colour",
        "year":            "year",
        "make":            "brand",
        "model":           "model",
        "model detail":    "model_detail",
        "cylinders":       "cylinders",
        "number plate":    "number_plate",
        "plate":           "number_plate",
        "on road costs":   "on_road_costs",
        "import history":  "import_history",
    }

    try:
        specs_js = driver.execute_script("""
            var specs = {};

            // PRIMARY METHOD: icon alt= attributes confirmed from DevTools PDF
            // Each spec row has: <tg-icon alt="Kilometres"> followed by <label> with value
            var altToKey = {
                'kilometres':       'kilometres',
                'odometer':         'kilometres',
                'body style':       'body style',
                'body-style':       'body style',
                'fuel type':        'fuel type',
                'fuel-type':        'fuel type',
                'transmission':     'transmission',
                'engine size':      'engine size',
                'engine-size':      'engine size',
                'engine':           'engine size',
                'doors':            'doors',
                'seats':            'seats',
                'exterior colour':  'exterior colour',
                'exterior-colour':  'exterior colour',
                'colour':           'exterior colour',
                'cylinders':        'cylinders',
                'number plate':     'number plate',
                'number-plate':     'number plate',
                'on road costs':    'on road costs',
                'on-road-costs':    'on road costs',
                'import history':   'import history',
                'import-history':   'import history',
                'year':             'year',
                'make':             'make',
                'model':            'model',
            };
            document.querySelectorAll('tg-icon[alt], img[alt]').forEach(function(icon) {
                var altLower = (icon.getAttribute('alt') || '').toLowerCase().trim();
                if (!altToKey[altLower]) return;
                // label is the sibling or parent's label child
                var parent = icon.closest('div[class*="vehicle-basic-detail"], div[class*="basic-detail"]');
                if (!parent) parent = icon.parentElement;
                var lbl = parent ? parent.querySelector('label, span[class*="label"], div[class*="label"]') : null;
                if (lbl && lbl.innerText.trim()) {
                    specs[altToKey[altLower]] = lbl.innerText.trim();
                } else {
                    // try: next sibling text
                    var next = icon.nextElementSibling;
                    while (next) {
                        var t = (next.innerText || next.textContent || '').trim();
                        if (t) { specs[altToKey[altLower]] = t; break; }
                        next = next.nextElementSibling;
                    }
                }
            });

            // CONFIRMED METHOD (Image 2): tm-vehicle-basic-detail__basic has 2 child divs
            // First div = label text, second div = value text
            document.querySelectorAll('div[class*="tm-vehicle-basic-detail__basic"]').forEach(function(basic) {
                var divs = basic.querySelectorAll(':scope > div');
                if (divs.length >= 2) {
                    var k = divs[0].innerText.trim().toLowerCase().replace(/:/g,'').trim();
                    var v = divs[1].innerText.trim();
                    if (k && v && !specs[k]) specs[k] = v;
                }
            });

            // SECONDARY METHOD: vehicle-basic-detail rows (label + value divs)
            document.querySelectorAll(
                'div[class*="vehicle-basic-detail"], div[class*="tm-vehicle-basic-detail"]'
            ).forEach(function(row) {
                var lbl = row.querySelector('label, div[class*="label"], span[class*="label"]');
                var val = row.querySelector('label, div[class*="value"], span[class*="value"]');
                if (lbl && val && lbl !== val) {
                    var k = lbl.innerText.trim().toLowerCase().replace(/:/g,'');
                    var v = val.innerText.trim();
                    if (k && v && !specs[k]) specs[k] = v;
                }
                // alt-label pairing: icon alt + sibling label
                var icon = row.querySelector('tg-icon[alt], img[alt]');
                if (icon) {
                    var altLower = (icon.getAttribute('alt') || '').toLowerCase().trim();
                    var labelEl = row.querySelector('label');
                    if (labelEl && altToKey[altLower] && !specs[altToKey[altLower]]) {
                        specs[altToKey[altLower]] = labelEl.innerText.trim();
                    }
                }
            });

            return specs;
        """)

        if specs_js:
            for raw_key, raw_val in specs_js.items():
                for pattern, field in spec_map.items():
                    if pattern in raw_key:
                        data[field] = raw_val
                        break
    except Exception as e:
        log.warning(f"  Specs extraction error: {e}")


    # ── Ratings: Overall safety, Energy Economy, Carbon emissions, Driver Safety ──
    # Wait for tm-listing-ratings web component to fully render
    try:
        for _ in range(10):
            has_ratings = driver.execute_script("""
                var el = document.querySelector('tm-listing-ratings, div[class*="listing-ratings"]');
                if (!el) return false;
                return el.querySelectorAll('[aria-label*="out of"]').length > 0;
            """)
            if has_ratings:
                break
            time.sleep(0.8)
    except:
        pass

    try:
        ratings_js = driver.execute_script("""
            var r = {overall_safety: null, energy_economy: null, carbon: null, driver_safety: null};

            // Find each rating card by its title text
            var cards = document.querySelectorAll(
                'div[class*="listing-ratings_card"], div[class*="ratings-card"], ' +
                'div[class*="listing-ratings_rating"], div[class*="tm-listing-ratings"]'
            );

            // Also try the broader ratings section
            var ratingSection = document.querySelector(
                'div[class*="tm-listing-ratings"], div[class*="tm-motors-listing-ratings"], ' +
                'div[class*="listing-ratings"]'
            );

            function getStarsFromEl(el) {
                // aria-label="X out of Y stars" on icon or container
                var starEl = el.querySelector('[aria-label*="out of"]') || el;
                var m = (starEl.getAttribute('aria-label') || '').match(/([\d.]+)\s*out of/i);
                if (m) return parseFloat(m[1]);
                // also check name="star-fill" filled icons count
                var filled = el.querySelectorAll('[name="star-fill"][aria-hidden="true"], tg-icon[name*="star"][aria-hidden="true"]').length;
                if (filled > 0) return filled;
                return null;
            }

            // Method 1: find by card title text
            var sections = ratingSection
                ? ratingSection.querySelectorAll('div[class*="card"], div[class*="rating-card"]')
                : document.querySelectorAll('div[class*="listing-ratings_card"]');

            sections.forEach(function(card) {
                var titleEl = card.querySelector('div[class*="title"], h2, [class*="top-section"]');
                var txt = (titleEl ? titleEl.innerText : card.innerText) || '';
                var stars = getStarsFromEl(card);
                if (stars === null) return;
                if (/energy|economy|fuel economy/i.test(txt))
                    r.energy_economy = stars;
                else if (/overall.*safety|safety.*rating/i.test(txt))
                    r.overall_safety = stars;
                else if (/carbon|emission/i.test(txt))
                    r.carbon = stars;
                else if (/driver.*safety/i.test(txt))
                    r.driver_safety = stars;
            });

            // Method 2: fallback — scan all [aria-label*="out of"] with nearby title
            if (!r.overall_safety || !r.energy_economy || !r.carbon) {
                document.querySelectorAll('[aria-label*="out of"]').forEach(function(el) {
                    var m = (el.getAttribute('aria-label') || '').match(/([\d.]+)\s*out of/i);
                    if (!m) return;
                    var stars = parseFloat(m[1]);
                    var section = el.closest('div[class*="card"], div[class*="rating"]') || el.parentElement;
                    var txt = section ? (section.innerText || '') : '';
                    if (/energy|economy/i.test(txt) && !r.energy_economy) r.energy_economy = stars;
                    else if (/overall.*safety|overall safety/i.test(txt) && !r.overall_safety) r.overall_safety = stars;
                    else if (/carbon|emission/i.test(txt) && !r.carbon) r.carbon = stars;
                    else if (/driver/i.test(txt) && !r.driver_safety) r.driver_safety = stars;
                });
            }
            return r;
        """)
        if ratings_js:
            data["overall_safety"] = ratings_js.get("overall_safety")
            data["energy_economy"] = ratings_js.get("energy_economy")
            data["carbon"]         = ratings_js.get("carbon")
            data["driver_safety"]  = ratings_js.get("driver_safety")
            log.info(f'  Ratings — Safety:{data["overall_safety"]} Energy:{data["energy_economy"]} Carbon:{data["carbon"]} Driver:{data["driver_safety"]}')

        # Also scrape carbon grams/km text value
        try:
            carbon_text = driver.execute_script("""
                var el = document.querySelector(
                    'div[class*="listing-ratings_carbon"], div[class*="ratings-carbon"], ' +
                    'div[class*="carbon"]'
                );
                return el ? el.innerText.trim() : null;
            """)
            if carbon_text:
                data["carbon_text"] = carbon_text
                log.info(f'  Carbon text: {carbon_text}')
        except:
            pass

        # Fuel economy text
        try:
            fuel_eco = driver.execute_script("""
                var el = document.querySelector('div[class*="listing-ratings_fuel"], div[class*="ratings_fuel"]');
                if (!el) {
                    var els = document.querySelectorAll('div[class*="listing-ratings"] div');
                    for (var e of els) {
                        if (/l\/100km/i.test(e.innerText)) return e.innerText.trim();
                    }
                }
                return el ? el.innerText.trim() : null;
            """)
            if fuel_eco:
                data["fuel_economy_text"] = fuel_eco
        except:
            pass

    except Exception as e:
        log.warning(f"  Ratings extraction error: {e}")

    # ── Description ── (extract HTML to preserve formatting: bullets, headings, line breaks)
    try:
        # Step 1: Expand "Show more" / "Read more"
        driver.execute_script("""
            document.querySelectorAll('button, a').forEach(function(b) {
                var t = (b.innerText || '').trim().toLowerCase();
                if (t === 'show more' || t === 'read more' || t === 'show full description'
                    || t.includes('show more') || t.includes('read more')) {
                    try { b.click(); } catch(e) {}
                }
            });
        """)
        time.sleep(2)

        # Step 2: Force-expand hidden/truncated containers
        driver.execute_script("""
            document.querySelectorAll(
                'div[class*="listing-description"], div[class*="tm-motors-listing-description"], ' +
                'tm-markdown, div[class*="description"]'
            ).forEach(function(el) {
                el.style.maxHeight = 'none';
                el.style.overflow  = 'visible';
                el.style.webkitLineClamp = 'unset';
            });
        """)
        time.sleep(0.5)

        # Step 3: Extract innerHTML to preserve formatting (bullets, bold, headings, <br>)
        desc_html = driver.execute_script("""
            function cleanHtml(el) {
                var clone = el.cloneNode(true);
                // Remove "Description" headings and Show more/less buttons
                clone.querySelectorAll('h1, h2, h3').forEach(function(h) {
                    if (/^description\.?$/i.test((h.innerText || '').trim())) h.remove();
                });
                clone.querySelectorAll('button, [class*="show-more"], [class*="read-more"]').forEach(
                    function(b) { b.remove(); }
                );
                return clone.innerHTML.trim();
            }

            // METHOD 1: tm-markdown web component (TradeMe primary description element)
            var md = document.querySelector('tm-markdown');
            if (md) {
                var shadow = md.shadowRoot;
                if (shadow) {
                    var inner = shadow.querySelector('div, section, article');
                    if (inner && inner.innerText.trim().length > 10) return inner.innerHTML.trim();
                }
                if (md.innerText.trim().length > 10) return cleanHtml(md);
            }

            // METHOD 2: specific description text class selectors
            var selectors = [
                '[class*="tm-motors-listing-description__text"]',
                '[class*="listing-description__text"]',
                '[class*="listing-description__body"]',
                '[class*="description__content"]',
                '[class*="description-content"]',
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el && el.innerText.trim().length > 10) return cleanHtml(el);
            }

            // METHOD 3: container div — strip heading and buttons from clone
            var containers = document.querySelectorAll(
                'div[class*="tm-motors-listing-description"], div[class*="listing-description"]'
            );
            for (var c = 0; c < containers.length; c++) {
                if (containers[c].innerText.trim().length > 10) return cleanHtml(containers[c]);
            }

            return '';
        """)

        # Step 4: Retry after pause if empty (handles lazy-render)
        if not desc_html or len(desc_html.strip()) < 10:
            time.sleep(3)
            desc_html = driver.execute_script("""
                var md = document.querySelector('tm-markdown');
                if (md && md.innerText.trim().length > 10) return md.innerHTML.trim();
                var el = document.querySelector(
                    '[class*="tm-motors-listing-description__text"], ' +
                    '[class*="listing-description__text"], ' +
                    'div[class*="tm-motors-listing-description"]'
                );
                return el ? el.innerHTML.trim() : '';
            """)

        # Step 5: Plain-text fallback — convert newlines to <br>
        if not desc_html or len(desc_html.strip()) < 10:
            desc_plain = driver.execute_script("""
                var md = document.querySelector('tm-markdown');
                return (md && md.innerText.trim().length > 10) ? md.innerText.trim() : '';
            """)
            if desc_plain:
                plines = desc_plain.strip().split('\n')
                if plines and re.match(r'^description\.?$', plines[0].strip(), re.I):
                    plines = plines[1:]
                while plines and re.match(r'^show\s+(less|more)\.?$', plines[-1].strip(), re.I):
                    plines = plines[:-1]
                desc_html = '<br>\n'.join(plines).strip()

        data["description"] = desc_html.strip() if desc_html else ""
        log.info(f"  Description length: {len(data['description'])} chars (HTML)")
    except Exception as e:
        log.warning(f"  Description extraction error: {e}")
        data["description"] = ""

    # ── Location — strip ONLY "Seller located in" prefix, keep full address (Image 1) ──
    try:
        loc = driver.execute_script("""
            var el = document.querySelector('span[class*="tm-motors-date-city-watchlist__location"]');
            if (!el) el = document.querySelector('span[name="location"], div[class*="location"]');
            if (!el) return 'Auckland';
            var txt = el.innerText.trim();
            // Strip "Seller located in " prefix only — keep everything after it
            txt = txt.replace(/^seller\s+located\s+in\s*/i, '').trim();
            return txt || 'Auckland';
        """)
        data["location"] = loc if loc else "Auckland"
    except:
        data["location"] = "Auckland"

    # ── Listed Date — parse TradeMe relative date string into YYYY-MM-DD ──
    # "Listed more than a month ago" → exactly 1 month back (e.g. March 10 → Feb 10)
    # "Listed X months ago" → X months back
    # "Listed X days/weeks ago" → X days/weeks back
    # "Listed today/yesterday" → today/yesterday
    # "Listed 5 Mar 2026" → exact date
    # "Listed within the last 30 days" → 30 days back
    try:
        from datetime import datetime, timedelta
        import calendar

        def subtract_months(dt, months):
            """Pure-Python month subtraction — no dateutil needed."""
            month = dt.month - months
            year  = dt.year
            while month < 1:
                month += 12
                year  -= 1
            while month > 12:
                month -= 12
                year  += 1
            # clamp day to last valid day of target month
            max_day = calendar.monthrange(year, month)[1]
            day = min(dt.day, max_day)
            return dt.replace(year=year, month=month, day=day)

        listed_raw = driver.execute_script("""
            var el = document.querySelector('span[class*="tm-motors-date-city-watchlist__date"]');
            if (!el) el = document.querySelector('span[class*="date-city-watchlist__date"]');
            return el ? el.innerText.trim() : null;
        """)
        now = datetime.now()
        listed_date = now  # default = today
        if listed_raw:
            txt = listed_raw.strip().lower()
            txt = re.sub(r'^listed\s*', '', txt).strip()   # strip "listed " prefix
            if 'today' in txt:
                listed_date = now
            elif 'yesterday' in txt:
                listed_date = now - timedelta(days=1)
            elif re.search(r'\d+\s*day', txt):
                # "X days ago"
                m = re.search(r'(\d+)\s*day', txt)
                listed_date = now - timedelta(days=int(m.group(1)))
            elif re.search(r'\d+\s*week', txt):
                # "X weeks ago"
                m = re.search(r'(\d+)\s*week', txt)
                listed_date = now - timedelta(weeks=int(m.group(1)))
            elif re.search(r'within.*last.*30|last.*30.*day', txt):
                # "within the last 30 days"
                listed_date = now - timedelta(days=30)
            elif re.search(r'(more than\s+)?a\s+month', txt):
                # "more than a month ago" or "a month ago" → exactly 1 calendar month back
                # e.g. 2026-03-10 → 2026-02-10
                listed_date = subtract_months(now, 1)
            elif re.search(r'\d+\s*month', txt):
                # "X months ago" or "more than X months ago"
                m = re.search(r'(\d+)\s*month', txt)
                listed_date = subtract_months(now, int(m.group(1)))
            else:
                # Try exact date formats: "5 Mar 2026", "Mar 5, 2026", etc.
                for fmt in ["%d %b %Y", "%b %d, %Y", "%d/%m/%Y", "%Y-%m-%d"]:
                    try:
                        listed_date = datetime.strptime(txt, fmt)
                        break
                    except: pass
        data["listed_date"] = listed_date.strftime("%Y-%m-%d")
        log.info(f'  Listed date: {data["listed_date"]} (raw: "{listed_raw}")')
    except Exception as e:
        from datetime import datetime
        data["listed_date"] = datetime.now().strftime("%Y-%m-%d")
        log.warning(f"  Listed date fallback to today: {e}")

    # ── Seller / Dealer name ──
    try:
        seller = driver.execute_script("""
            var el = document.querySelector(
                'div[class*="tm-dealer-listing-gallery"] a,' +
                'a[class*="dealer"], div[class*="dealer-name"]'
            );
            return el ? el.innerText.trim() : '';
        """)
        data["seller_name"] = seller or ""
    except:
        data["seller_name"] = ""

    # ── Images — full resolution from carousel items (confirmed from DevTools Images 4 & 5) ──
    # Each tm-gallery-carousel__item wraps a <picture> with multiple <source> tags:
    #   352x264c, 480m, full, 1024sq, plus  — "full" and "plus" are highest res
    # The <img class="tm-progressive-image-loader__full"> always has src="/photoserver/full/ID.jpg"
    # IMPORTANT: There are many duplicate __item divs at different translateX offsets (slideshow).
    # We dedupe by extracting the numeric photo ID from the URL.
    img_urls = []
    try:
        raw_urls = driver.execute_script("""
            var urls = [];
            var seenIds = {};

            function getPhotoId(url) {
                // Extract numeric ID from URLs like /photoserver/full/2272236763.jpg
                var m = url.match(/\\/photoserver\\/[^/]+\\/(\\d+)\\.jpg/i);
                return m ? m[1] : url;
            }

            function getBestUrl(item) {
                // BEST: img with class containing "full" — has /photoserver/full/ID.jpg
                var fullImg = item.querySelector(
                    'img[class*="progressive-image-loader__full"][src*="trademe"],' +
                    'img[class*="image-loader__full"][src*="trademe"]'
                );
                if (fullImg) {
                    var s = fullImg.getAttribute('src') || '';
                    if (s && s.includes('trademe')) return s;
                }
                // GOOD: <source> with /full/ or /plus/ in srcset
                var sources = item.querySelectorAll('picture source');
                var best = null, bestScore = -1;
                var priority = {'plus': 4, 'full': 3, '1024sq': 2, '480m': 1, '352x264c': 0};
                sources.forEach(function(src) {
                    var srcset = src.getAttribute('srcset') || '';
                    var url = srcset.split(',')[0].trim().split(' ')[0];
                    if (!url || !url.includes('trademe')) return;
                    var m = url.match(/\\/photoserver\\/([^/]+)\\//);
                    var score = m && priority[m[1]] !== undefined ? priority[m[1]] : -1;
                    if (score > bestScore) { bestScore = score; best = url; }
                });
                if (best) return best;
                // FALLBACK: any img in item
                var img = item.querySelector('img[src*="trademe"]');
                return img ? img.getAttribute('src') : null;
            }

            // Only carousel items that contain an actual image loader (not empty transition slots)
            document.querySelectorAll('div[class*="tm-gallery-carousel__item"]').forEach(function(item) {
                var url = getBestUrl(item);
                if (!url) return;
                var id = getPhotoId(url);
                if (!seenIds[id]) {
                    seenIds[id] = true;
                    urls.push(url);
                }
            });

            // Fallback: all picture tags on page if carousel yielded nothing
            if (urls.length < 2) {
                document.querySelectorAll('picture').forEach(function(pic) {
                    var img = pic.querySelector('img[src*="trademe/photoserver"]');
                    if (img) {
                        var url = img.getAttribute('src');
                        var id = getPhotoId(url);
                        if (url && !seenIds[id]) { seenIds[id] = true; urls.push(url); }
                    }
                });
            }
            return urls;
        """)

        # Normalise: ensure /full/ path, strip query params, limit to 8
        seen_ids = set()
        for url in (raw_urls or []):
            if not url or 'trademe' not in url:
                continue
            clean = url.split('?')[0]
            # Upgrade any non-full paths to /full/
            if not re.search(r'/(?:full|plus)/', clean):
                clean = re.sub(r'/photoserver/[^/]+/', '/photoserver/full/', clean)
            # Dedupe by photo ID
            img_id_m = re.search(r'/(\d+)\.(?:jpg|jpeg|png)', clean, re.I)
            photo_id = img_id_m.group(1) if img_id_m else clean
            if photo_id not in seen_ids:
                seen_ids.add(photo_id)
                img_urls.append(clean)
            if len(img_urls) >= 8:
                break

        # If fewer than 5, try scrolling/clicking thumbnail strip to trigger lazy-load
        if len(img_urls) < 5:
            try:
                thumbs = driver.find_elements(By.CSS_SELECTOR,
                    "div[class*='tm-gallery-thumbnail-slider__item'], "
                    "button[class*='gallery-thumbnail']")
                for thumb in thumbs[:12]:
                    if len(img_urls) >= 8:
                        break
                    try:
                        driver.execute_script("arguments[0].scrollIntoView(true);", thumb)
                        driver.execute_script("arguments[0].click();", thumb)
                        time.sleep(0.6)
                        extra = driver.execute_script("""
                            var urls2 = [];
                            document.querySelectorAll('div[class*="tm-gallery-carousel__item"]').forEach(function(item) {
                                var img = item.querySelector('img[class*="progressive-image-loader__full"][src*="trademe"]');
                                if (img) urls2.push(img.getAttribute('src'));
                            });
                            return urls2;
                        """)
                        for u in (extra or []):
                            if not u or 'trademe' not in u: continue
                            u = u.split('?')[0]
                            m2 = re.search(r'/(\d+)\.(?:jpg|jpeg|png)', u, re.I)
                            pid = m2.group(1) if m2 else u
                            if pid not in seen_ids:
                                seen_ids.add(pid)
                                img_urls.append(u)
                    except: pass
            except: pass

        log.info(f"  Found {len(img_urls)} full-res unique images (min 5 target)")

    except Exception as e:
        log.warning(f"  Image extraction error: {e}")

    data["image_urls"] = img_urls
    log.info(f"  Found {len(img_urls)} images")
    return data


# ─────────────────────────────────────────────
# SCRAPE SEARCH PAGE — get listing URLs
# ─────────────────────────────────────────────
def get_listing_urls(driver, page_url):
    driver.get(page_url)
    time.sleep(3)

    # Scroll to trigger lazy load
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(1)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)

    items = driver.execute_script("""
        var results = [];
        var seen = {};

        // TradeMe listing cards link pattern: /a/motors/cars/bmw/listing/XXXXXXX
        document.querySelectorAll('a[href*="/a/motors/cars/"][href*="/listing/"]').forEach(function(a) {
            var href = a.href;
            if (!seen[href]) {
                seen[href] = true;
                var titleEl = a.querySelector(
                    'h2, div[class*="title"], span[class*="title"], ' +
                    'div[class*="listing-title"], p[class*="title"]'
                );
                results.push({
                    url: href,
                    title: titleEl ? titleEl.innerText.trim() : ''
                });
            }
        });
        return results;
    """)

    return items or []


# ─────────────────────────────────────────────
# LOGIN + POST TO KARNATAKA
# ─────────────────────────────────────────────
def login_karnataka(driver):
    driver.get(KARNATAKA_LOGIN)
    time.sleep(2)
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, "username")))
    driver.find_element(By.NAME, "username").clear()
    driver.find_element(By.NAME, "username").send_keys(USERNAME)
    driver.find_element(By.NAME, "password").clear()
    driver.find_element(By.NAME, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']").click()
    time.sleep(2)
    log.info("  Logged in to Karnataka")


def post_ad_karnataka(driver, data, image_paths):
    for page_attempt in range(2):
        driver.get(KARNATAKA_ADD)
        time.sleep(3)
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "title")))
            break
        except:
            log.warning(f"  Page load attempt {page_attempt+1} failed - retrying...")
            if "login" in driver.current_url.lower() or page_attempt == 1:
                log.warning("  Session expired - re-logging in...")
                login_karnataka(driver)
                driver.get(KARNATAKA_ADD)
                time.sleep(3)
    else:
        log.error("  Could not load add page - skipping")
        return False

    fill_field(driver, By.ID, "title", data.get("title", ""))

    # ── Categories ──
    # L1: value="6"  → Vehicles
    # L2: value="8"  → Cars - Parts
    # L3: value="32" → Second hand cars in Newzeland
    # Pure JS only — no WebDriverWait lambdas (they cause silent crashes)
    cat_loaded = False
    for cat_attempt in range(3):
        try:
            # Wait for page to be ready
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "jomcl_category_-1_1")))
            time.sleep(2)

            # L1: Vehicles (value=6)
            driver.execute_script("""
                var s = document.querySelector('#jomcl_category_-1_1 select, select[name="category[]"]');
                s.value = '6';
                s.dispatchEvent(new Event('change', {bubbles:true}));
            """)
            time.sleep(4)

            # L2: Cars - Parts (value=8)
            driver.execute_script("""
                var s = document.querySelector('#jomcl_category_6_1 select');
                if (!s) {
                    var all = Array.from(document.querySelectorAll('select[name="category[]"]'))
                                   .filter(function(x){ return x.offsetParent !== null; });
                    s = all[1];
                }
                s.value = '8';
                s.dispatchEvent(new Event('change', {bubbles:true}));
            """)
            time.sleep(4)

            # L3: Second hand cars in Newzeland (value=32)
            driver.execute_script("""
                var s = document.querySelector('#jomcl_category_8_1 select');
                if (!s) {
                    var all = Array.from(document.querySelectorAll('select[name="category[]"]'))
                                   .filter(function(x){ return x.offsetParent !== null; });
                    s = all[2];
                }
                s.value = '32';
                s.dispatchEvent(new Event('change', {bubbles:true}));
            """)
            time.sleep(4)

            # Wait for extra fields — poll JS instead of WebDriverWait (avoids crash)
            # jcsextrafields_0 confirmed in DevTools; also check any exf_ input
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 400);")
            time.sleep(1)
            found_fields = False
            for _ in range(15):
                ok = driver.execute_script("""
                    var d = document.getElementById('jcsextrafields_0');
                    if (d && d.style.display !== 'none') return true;
                    if (document.querySelector('input[name^="exf_"]')) return true;
                    return false;
                """)
                if ok:
                    found_fields = True
                    break
                time.sleep(1)
            if not found_fields:
                raise Exception("Extra fields did not appear after category selection")
            cat_loaded = True
            log.info(f"  Categories set: Vehicles > Cars-Parts > Second hand cars NZ (attempt {cat_attempt+1})")
            break

        except Exception as e:
            log.warning(f"  Category attempt {cat_attempt+1} failed: {e} — reloading...")
            driver.get(KARNATAKA_ADD)
            time.sleep(5)
            fill_field(driver, By.ID, "title", data.get("title", ""))

    if not cat_loaded:
        log.error("  Categories failed after 3 attempts - skipping ad")
        return False

    def val(key, default="."):
        return data.get(key, "") or default

    def stars_to_value(star_float, max_stars=5):
        """Convert float like 3.0 → string value for Karnataka dropdown.
        5-star scale (Overall Safety, Driver Safety):
            -None-(-1), 0Star(1), 0.5Star(2), 1Star(3), 1.5Star(4),
            2Stars(5), 2.5Stars(6), 3Stars(7), 3.5Stars(8),
            4Stars(9), 4.5Stars(10), 5Stars(11)
        6-star scale (Energy Economy, Carbon emissions):
            same as above + 5.5Stars(12), 6Stars(13)"""
        if star_float is None:
            return "-1"
        mapping_5 = {0:1, 0.5:2, 1:3, 1.5:4, 2:5, 2.5:6, 3:7, 3.5:8, 4:9, 4.5:10, 5:11}
        mapping_6 = {0:1, 0.5:2, 1:3, 1.5:4, 2:5, 2.5:6, 3:7, 3.5:8, 4:9, 4.5:10, 5:11, 5.5:12, 6:13}
        mapping = mapping_6 if max_stars == 6 else mapping_5
        rounded = round(star_float * 2) / 2
        result = mapping.get(rounded, -1)
        if result == -1:
            result = 13 if max_stars == 6 else 11  # cap at max
        return str(result)

    # ── Text fields — EXACT field names confirmed from DevTools (PDF screenshots) ──
    # exf_25=Kilometer, exf_28=Fuel, exf_29=Engine CC, exf_26=Body Type
    # exf_30=Transmission, exf_34=Cylinders, exf_32=Year, exf_31=Number Plate
    # exf_33=Exterior colour, exf_35=Doors, exf_36=Import history
    # exf_37=Ask Price, exf_38=Buy Price, exf_39=Starting Price
    # exf_27=Seats, exf_46=Source Link, exf_45=Listed On (date)
    text_fields = [
        ("exf_25", val("km_run",         ".")),  # Kilometer
        ("exf_28", val("fuel_type",       ".")),  # Fuel
        ("exf_29", val("engine_capacity", ".")),  # Engine CC
        ("exf_26", val("body_type",       ".")),  # Body Type
        ("exf_30", val("transmission",    ".")),  # Transmission
        ("exf_34", val("cylinders",       ".")),  # Cylinders
        ("exf_32", val("year",            ".")),  # Year
        ("exf_31", val("number_plate",    ".")),  # Number Plate
        ("exf_33", val("colour",          ".")),  # Exterior colour
        ("exf_35", val("doors",           ".")),  # Doors
        ("exf_36", val("import_history",  ".")),  # Import history
        ("exf_37", val("asking_price",    ".")),  # Ask Price  (e.g. "$69,990")
        ("exf_38", val("buy_now_price",   ".")),  # Buy Price
        ("exf_39", val("starting_price",  ".")),  # Starting Price
        ("exf_27", val("seats",           ".")),  # Seats
        ("exf_46", val("source_link",     ".")),  # Source Link
        ("exf_40", val("on_road_costs",   ".")),  # On Road Costs (e.g. "Included")
    ]
    for field_name, field_val in text_fields:
        try:
            el = driver.find_element(By.NAME, field_name)
            driver.execute_script("arguments[0].value = arguments[1];", el, field_val)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el)
            log.info(f"  Filled {field_name}: {str(field_val)[:40]}")
        except Exception as e:
            log.warning(f"  Could not fill {field_name}: {e}")

    # ── Listed On date (exf_45) — Joomla calendar widget (Images 1 & 2) ──
    # data-dayformat="%Y-%m-%d", must set value + data-alt-value + fire input/change/blur
    try:
        listed_date = data.get("listed_date") or __import__('datetime').datetime.now().strftime("%Y-%m-%d")
        driver.execute_script("""
            var inp = document.getElementById('exf_45') || document.querySelector('input[name="exf_45"]');
            if (!inp) return;
            inp.value = arguments[0];
            inp.setAttribute('data-alt-value', arguments[0]);
            inp.setAttribute('data-local-value', arguments[0]);
            ['input','change','blur'].forEach(function(ev) {
                inp.dispatchEvent(new Event(ev, {bubbles:true}));
            });
            var cal = document.querySelector('.js-calendar, .calendar-container');
            if (cal) cal.style.display = 'none';
        """, listed_date)
        log.info(f"  exf_45 Listed On: {listed_date}")
    except Exception as e:
        log.warning(f"  Could not fill exf_45 (Listed On): {e}")

    # ── Star rating dropdowns — EXACT field names confirmed from DevTools ──
    # exf_42=Overall safety, exf_41=Energy Economy, exf_43=Carbon emissions, exf_44=Driver Safety
    # Values: -1=None, 1=0Star, 2=0.5Star, 3=1Star, 4=1.5Stars ... 11=5Stars
    # Energy Economy & Carbon go up to 13=6Stars (wider scale)
    for field_name, data_key, scale in [
        ("exf_42", "overall_safety", 5),   # Overall safety   (max 5 stars → val 11)
        ("exf_41", "energy_economy", 6),   # Energy Economy   (max 6 stars → val 13)
        ("exf_43", "carbon",         6),   # Carbon emissions (max 6 stars → val 13)
        ("exf_44", "driver_safety",  5),   # Driver Safety    (max 5 stars → val 11)
    ]:
        try:
            star_val = stars_to_value(data.get(data_key), max_stars=scale)
            sel_el = driver.find_element(By.NAME, field_name)
            driver.execute_script(
                "arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                sel_el, star_val)
            log.info(f"  {field_name} ({data_key}): {data.get(data_key)} stars → value {star_val}")
        except Exception as e:
            log.warning(f"  {field_name} dropdown error: {e}")

    # ── Description — TinyMCE with wait + multi-fallback ──
    desc_content = data.get("description", "").strip()
    if not desc_content:
        desc_content = data.get("title", "") or "."
        log.warning(f"  Description empty — using title as fallback: {desc_content[:60]}")

    desc_set = False

    # Method 1: Wait for TinyMCE to be ready, then setContent
    for tinymce_attempt in range(10):
        try:
            ready = driver.execute_script("""
                return (typeof tinymce !== 'undefined' &&
                        tinymce.activeEditor !== null &&
                        tinymce.activeEditor !== undefined &&
                        typeof tinymce.activeEditor.setContent === 'function');
            """)
            if ready:
                driver.execute_script("tinymce.activeEditor.setContent(arguments[0]);", desc_content)
                # Verify it was actually set
                check = driver.execute_script("""
                    try { return tinymce.activeEditor.getContent({format:'text'}).trim(); }
                    catch(e) { return ''; }
                """)
                if check and len(check.strip()) > 2:
                    log.info(f"  Description set via TinyMCE ({len(desc_content)} chars)")
                    desc_set = True
                    break
                else:
                    log.warning(f"  TinyMCE setContent silent fail (attempt {tinymce_attempt+1}), retrying...")
            time.sleep(1)
        except Exception as e:
            log.warning(f"  TinyMCE attempt {tinymce_attempt+1} error: {e}")
            time.sleep(1)

    # Method 2: Toggle to textarea mode and set value directly
    if not desc_set:
        try:
            toggle_btns = driver.find_elements(By.XPATH,
                "//button[contains(text(),'Toggle') or contains(text(),'Source') or contains(@title,'Source') or contains(@class,'mce-i-code')]")
            if toggle_btns:
                driver.execute_script("arguments[0].click();", toggle_btns[0])
                time.sleep(1)
            ta = driver.find_element(By.NAME, "description")
            driver.execute_script("arguments[0].value = arguments[1];", ta, desc_content)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", ta)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", ta)
            log.info(f"  Description set via textarea toggle ({len(desc_content)} chars)")
            desc_set = True
        except Exception as e:
            log.warning(f"  Textarea toggle fallback failed: {e}")

    # Method 3: Set value directly on all description-related textareas
    if not desc_set:
        try:
            driver.execute_script("""
                var targets = [
                    document.querySelector('textarea[name="description"]'),
                    document.getElementById('description'),
                    document.querySelector('textarea[id*="description"]'),
                    document.querySelector('.mce-content-body'),
                ];
                for (var i = 0; i < targets.length; i++) {
                    if (targets[i]) {
                        targets[i].value = arguments[0];
                        targets[i].innerHTML = arguments[0];
                        ['input','change','blur'].forEach(function(ev) {
                            targets[i].dispatchEvent(new Event(ev, {bubbles:true}));
                        });
                    }
                }
                // Also try TinyMCE iframe body
                var iframes = document.querySelectorAll('iframe[id*="mce"], iframe[id*="description"]');
                iframes.forEach(function(f) {
                    try {
                        var body = f.contentDocument.body;
                        if (body) body.innerHTML = arguments[0];
                    } catch(e) {}
                });
            """, desc_content)
            log.info(f"  Description set via direct DOM injection ({len(desc_content)} chars)")
            desc_set = True
        except Exception as e:
            log.warning(f"  Direct DOM injection failed: {e}")

    if not desc_set:
        log.error("  ❌ All description injection methods failed")


    # ── Images ──
    if image_paths:
        try:
            upload_input = driver.find_element(By.CSS_SELECTOR, "input[type='file']")
            upload_input.send_keys("\n".join(image_paths))
            time.sleep(3)
        except Exception as e:
            log.warning(f"  Image upload failed: {e}")

    # ── Price: not touched — left as site default ──

    # ── Address + Location ──
    try:
        fill_field(driver, By.NAME, "address", data.get("location", "Auckland"))
        # Select New Zealand (value="34" confirmed from DevTools PDF)
        loc_sel = Select(driver.find_element(By.CSS_SELECTOR, "select[name='location[]']"))
        try:
            loc_sel.select_by_value("34")   # Newzeland
            log.info("  Location: Newzeland (value=34)")
        except Exception:
            selected_loc = False
            for opt in loc_sel.options:
                if "new zealand" in opt.text.lower() or "newzeland" in opt.text.lower():
                    loc_sel.select_by_visible_text(opt.text)
                    selected_loc = True
                    log.info(f"  Location: {opt.text} (text match)")
                    break
            if not selected_loc:
                loc_sel.select_by_index(1)
    except Exception as e:
        log.warning(f"  Location field error: {e}")

    # ── Tag ──
    try:
        Select(driver.find_element(By.CSS_SELECTOR, "select[name='tagid']")).select_by_value("1")
    except:
        pass

    # ── Privacy checkbox ──
    try:
        cb = driver.find_element(By.CSS_SELECTOR, "input[name='privacy[]']")
        if not cb.is_selected():
            driver.execute_script("arguments[0].click();", cb)
    except:
        pass

    # Wait for image uploads to finish
    for _ in range(25):
        if not driver.find_elements(By.CSS_SELECTOR, "div[class*='loading'], div[class*='spinner']"):
            break
        time.sleep(1)
    time.sleep(2)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)

    # Re-tick privacy after scroll
    try:
        cb = driver.find_element(By.CSS_SELECTOR, "input[name='privacy[]']")
        if not cb.is_selected():
            driver.execute_script("arguments[0].click();", cb)
        log.info(f"  Privacy checked: {cb.is_selected()}")
    except:
        pass

    # ── Submit with retry ──
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
        except:
            pass
        if "add" not in driver.current_url:
            log.info(f"  ✅ Posted: {data.get('title')}")
            return True
        log.warning(f"  Still on add page, attempt {attempt+1}")

    log.error("  ❌ All submit attempts failed")
    return False


# ─────────────────────────────────────────────
# WORKER
# ─────────────────────────────────────────────
def worker(inst_id, start_page, end_page):
    prefix = f"[Inst{inst_id}]"
    log.info(f"{prefix} Starting — TradeMe pages {start_page} to {end_page}")

    driver = make_driver(headless=True)   # headless — runs in background
    try:
        login_karnataka(driver)

        for page_num in range(start_page, end_page + 1):

            with titles_lock:
                if post_counter[0] >= ADS_TO_POST:
                    log.info(f"{prefix} Target reached, stopping.")
                    break

            page_url = f"{TRADEME_SEARCH}&page={page_num}"
            log.info(f"{prefix} Loading page {page_num}: {page_url}")

            page_data = get_listing_urls(driver, page_url)

            if not page_data:
                log.info(f"{prefix} No listings on page {page_num}, stopping.")
                break

            log.info(f"{prefix} Page {page_num}: {len(page_data)} listings found")

            for ad in page_data:
                with titles_lock:
                    if post_counter[0] >= ADS_TO_POST:
                        break
                    if ad["title"] and ad["title"].lower() in posted_titles:
                        log.info(f"{prefix} ⚡ Skip (cached): {ad['title']}")
                        continue

                # Scrape full ad — returns None if no price
                data = scrape_trademe_ad(driver, ad["url"])
                if not data:
                    continue
                if not data.get("title"):
                    continue

                with titles_lock:
                    if data["title"].lower() in posted_titles:
                        log.info(f"{prefix} ⚡ Skip (scraped): {data['title']}")
                        continue

                with post_lock:
                    safe_name = re.sub(r'[\\/*?:"<>|]', "", data["title"])[:50]
                    img_paths = download_images(
                        data.get("image_urls", []),
                        os.path.join(IMAGE_DIR, f"inst{inst_id}_{safe_name}")
                    )

                    with titles_lock:
                        if data["title"].lower() in posted_titles:
                            continue
                        if post_counter[0] >= ADS_TO_POST:
                            break

                    log.info(f"{prefix} 📝 Posting: {data['title']} | [{data['price_label']}: ${data['price']} — not posted to Karnataka]")
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
    posted_titles = load_posted_titles()

    log.info(f"Starting {NUM_INSTANCES} parallel instances...")
    log.info(f"Each covers {PAGES_PER_INST} pages | Target: {ADS_TO_POST} new posts")
    log.info(f"Source: {TRADEME_SEARCH}")
    log.info("SKIP RULE: Listings with NO asking/starting/buy-now price will be skipped")

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
        time.sleep(5)  # stagger browser starts

    for t in threads:
        t.join()

    log.info(f"\n✅ Done! Total posted: {post_counter[0]}")


if __name__ == "__main__":
    main()
