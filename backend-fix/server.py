from flask import Flask, request, jsonify
from scrapling import Fetcher
from collections import OrderedDict
import json
import math
import os
import queue
import re
import threading
import time
import traceback

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 64 * 1024))

API_KEY = os.environ.get("SAFABITES_API_KEY", "").strip()
FETCH_TIMEOUT_SECONDS = float(os.environ.get("FETCH_TIMEOUT_SECONDS", "6"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "600"))
CACHE_MAX_ITEMS = int(os.environ.get("CACHE_MAX_ITEMS", "256"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "60"))
DOORDASH_SEARCH_LIMIT = int(os.environ.get("DOORDASH_SEARCH_LIMIT", "5"))
MAX_RESTAURANT_RESULTS = int(os.environ.get("MAX_RESTAURANT_RESULTS", "80"))

cache_lock = threading.Lock()
response_cache = OrderedDict()
rate_lock = threading.Lock()
rate_buckets = {}

fetcher = Fetcher(auto_match=True)

stealth = None
try:
    from scrapling import StealthyFetcher
    stealth = StealthyFetcher(auto_match=True)
    print("StealthyFetcher available")
except Exception as e:
    print(f"StealthyFetcher not available ({e}), using Fetcher only")


@app.before_request
def enforce_api_controls():
    if not request.path.startswith("/api/"):
        return None

    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    client_id = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    with rate_lock:
        bucket = [ts for ts in rate_buckets.get(client_id, []) if now - ts < RATE_LIMIT_WINDOW_SECONDS]
        if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
            rate_buckets[client_id] = bucket
            return jsonify({"error": "rate limit exceeded"}), 429
        bucket.append(now)
        rate_buckets[client_id] = bucket
    return None


def smart_get(url, prefer_stealth=False):
    if prefer_stealth and stealth:
        try:
            return bounded_fetch(stealth.get, url)
        except TimeoutError:
            raise
        except Exception as e:
            print(f"StealthyFetcher failed for {url}: {e}")
    return bounded_fetch(fetcher.get, url)


def bounded_fetch(fetch_func, url):
    result_queue = queue.Queue(maxsize=1)

    def run_fetch():
        try:
            result_queue.put((fetch_func(url), None))
        except Exception as exc:
            result_queue.put((None, exc))

    worker = threading.Thread(target=run_fetch, daemon=True)
    worker.start()
    worker.join(FETCH_TIMEOUT_SECONDS)

    if worker.is_alive():
        raise TimeoutError(f"fetch timed out after {FETCH_TIMEOUT_SECONDS}s: {url}")

    result, error = result_queue.get_nowait()
    if error:
        raise error
    return result


def request_payload():
    payload = request.get_json(silent=True) or {}
    return payload if isinstance(payload, dict) else {}


def sanitize_text(value, max_length=160):
    text = " ".join(str(value or "").split())
    return text[:max_length]


def parse_coordinates(payload):
    try:
        lat = float(payload.get("latitude"))
        lng = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return None, None

    if not math.isfinite(lat) or not math.isfinite(lng):
        return None, None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None, None
    return lat, lng


def parse_optional_coordinates(payload):
    has_lat = payload.get("latitude") not in (None, "")
    has_lng = payload.get("longitude") not in (None, "")
    if not has_lat and not has_lng:
        return None, None, True
    lat, lng = parse_coordinates(payload)
    return lat, lng, lat is not None and lng is not None


def float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def cache_key(name, payload):
    stable_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{name}:{stable_payload}"


def cache_get(key):
    now = time.time()
    with cache_lock:
        entry = response_cache.get(key)
        if not entry:
            return None
        stored_at, value = entry
        if now - stored_at > CACHE_TTL_SECONDS:
            response_cache.pop(key, None)
            return None
        response_cache.move_to_end(key)
        return value


def cache_set(key, value):
    with cache_lock:
        response_cache[key] = (time.time(), value)
        response_cache.move_to_end(key)
        while len(response_cache) > CACHE_MAX_ITEMS:
            response_cache.popitem(last=False)


# ──────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────

@app.route("/api/restaurants", methods=["POST"])
def get_restaurants():
    try:
        payload = request_payload()
        lat, lng = parse_coordinates(payload)
        if lat is None or lng is None:
            return jsonify({"error": "valid latitude and longitude required"}), 400

        key = cache_key("restaurants", {"latitude": round(lat, 4), "longitude": round(lng, 4)})
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)

        zabihah = scrape_zabihah(lat, lng)
        doordash = scrape_doordash(lat, lng)
        merged = merge_results(zabihah, doordash)
        response = {"restaurants": merged}
        cache_set(key, response)
        return jsonify(response)
    except Exception as e:
        print(f"Error in /api/restaurants: {e}")
        traceback.print_exc()
        return jsonify({"restaurants": [], "error": "restaurant lookup failed"}), 500


@app.route("/api/zabihah", methods=["POST"])
def zabihah_only():
    payload = request_payload()
    lat, lng = parse_coordinates(payload)
    if lat is None or lng is None:
        return jsonify({"error": "valid latitude and longitude required"}), 400
    key = cache_key("zabihah", {"latitude": round(lat, 4), "longitude": round(lng, 4)})
    cached = cache_get(key)
    if cached is not None:
        return jsonify(cached)
    response = {"restaurants": scrape_zabihah(lat, lng)}
    cache_set(key, response)
    return jsonify(response)


@app.route("/api/menu", methods=["POST"])
def get_menu():
    try:
        payload = request_payload()
        name = sanitize_text(payload.get("name", ""))
        address = sanitize_text(payload.get("address", ""), max_length=240)
        lat, lng, valid_coordinates = parse_optional_coordinates(payload)
        if not valid_coordinates:
            return jsonify({"error": "latitude and longitude must both be valid when provided"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400
        key = cache_key("menu", {"name": name.lower(), "address": address.lower(), "latitude": lat, "longitude": lng})
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        menu = scrape_menu(name, address, lat, lng)
        response = {"menu": menu}
        cache_set(key, response)
        return jsonify(response)
    except Exception as e:
        print(f"Error in /api/menu: {e}")
        traceback.print_exc()
        return jsonify({"menu": [], "error": "menu lookup failed"}), 500


@app.route("/api/doordash/enrich", methods=["POST"])
def get_doordash_enrichment():
    try:
        payload = request_payload()
        name = sanitize_text(payload.get("name", ""))
        address = sanitize_text(payload.get("address", ""), max_length=240)
        lat, lng, valid_coordinates = parse_optional_coordinates(payload)
        if not valid_coordinates:
            return jsonify({"error": "latitude and longitude must both be valid when provided"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400
        key = cache_key("doordash-enrich", {"name": name.lower(), "address": address.lower(), "latitude": lat, "longitude": lng})
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        response = {"enrichment": scrape_doordash_enrichment(name, address, lat, lng)}
        cache_set(key, response)
        return jsonify(response)
    except Exception as e:
        print(f"Error in /api/doordash/enrich: {e}")
        traceback.print_exc()
        return jsonify({"enrichment": {}, "error": "DoorDash enrichment failed"}), 500


@app.route("/api/doordash/photos", methods=["POST"])
def get_doordash_photos():
    try:
        payload = request_payload()
        name = sanitize_text(payload.get("name", ""))
        address = sanitize_text(payload.get("address", ""), max_length=240)
        lat, lng, valid_coordinates = parse_optional_coordinates(payload)
        if not valid_coordinates:
            return jsonify({"error": "latitude and longitude must both be valid when provided"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400
        key = cache_key("doordash-photos", {"name": name.lower(), "address": address.lower(), "latitude": lat, "longitude": lng})
        cached = cache_get(key)
        if cached is not None:
            return jsonify(cached)
        enrichment = scrape_doordash_enrichment(name, address, lat, lng)
        response = {"photos": enrichment.get("photos", [])}
        cache_set(key, response)
        return jsonify(response)
    except Exception as e:
        print(f"Error in /api/doordash/photos: {e}")
        traceback.print_exc()
        return jsonify({"photos": [], "error": "DoorDash photo lookup failed"}), 500


# ──────────────────────────────────────────────
# JSON ARRAY EXTRACTION (bracket matching)
# ──────────────────────────────────────────────

def extract_json_array(text, key):
    marker = f'"{key}":'
    idx = text.find(marker)
    if idx == -1:
        return None

    start = text.find('[', idx + len(marker))
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, min(start + 500000, len(text))):
        c = text[i]
        if escaped:
            escaped = False
            continue
        if c == '\\':
            escaped = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if not in_string:
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError as e:
                        print(f"JSON parse failed for {key}: {e}")
                        return None
    return None


# ──────────────────────────────────────────────
# ZABIHAH SCRAPING
# ──────────────────────────────────────────────

def scrape_zabihah(lat, lng):
    try:
        url = f"https://www.zabihah.com/search?lat={lat}&lng={lng}"
        page = smart_get(url)

        html = ""
        for script in page.css("script"):
            text = script.text or ""
            if "initialRestaurants" in text:
                html = text
                break

        if not html:
            body = page.body
            html = body.html if body else ""

        if "initialRestaurants" in html:
            unescaped = html.replace('\\"', '"').replace('\\\\', '\\')
            restaurants = extract_json_array(unescaped, "initialRestaurants")
            if restaurants:
                print(f"Zabihah: found {len(restaurants)} restaurants")
                return enrich_zabihah(restaurants, page)

        print("Zabihah: no initialRestaurants found")
        return []
    except Exception as e:
        print(f"Zabihah error: {e}")
        traceback.print_exc()
        return []


def enrich_zabihah(restaurants, page):
    try:
        for card in page.css("[class*='restaurant'], [class*='listing'], [data-id]"):
            data_id = card.attrib.get("data-id", "")
            if not data_id:
                continue
            for r in restaurants:
                if r.get("id") == data_id:
                    desc_el = card.css_first("[class*='desc'], [class*='summary'], p")
                    if desc_el and desc_el.text:
                        if not r.get("halalSummary"):
                            r["halalSummary"] = {}
                        if not r["halalSummary"].get("description"):
                            r["halalSummary"]["description"] = desc_el.text.strip()

                    img_el = card.css_first("img[src*='http']")
                    if img_el and not r.get("coverImage"):
                        r["coverImage"] = img_el.attrib.get("src", "")
    except Exception:
        pass
    return restaurants


def parse_zabihah_html(page):
    restaurants = []
    for card in page.css("[class*='restaurant'], [class*='listing'], .card"):
        try:
            name_el = card.css_first("h2, h3, h4, [class*='name'], [class*='title']")
            addr_el = card.css_first("[class*='address'], [class*='location'], address")
            if not name_el:
                continue
            restaurants.append({
                "id": f"zab-html-{len(restaurants)}",
                "name": (name_el.text or "").strip(),
                "address": (addr_el.text or "").strip() if addr_el else "Nearby",
                "latitude": "",
                "longitude": "",
                "cuisine": [],
                "rating": None,
                "reviewCount": 0,
                "handSlaughtered": False,
                "restaurantType": 1,
                "coverImage": None,
                "galleryPhotos": [],
                "businessHours": [],
                "halalSummary": {"description": "From Zabihah", "meatHalalStatus": None}
            })
        except Exception:
            continue
    return restaurants


# ──────────────────────────────────────────────
# DOORDASH SCRAPING
# ──────────────────────────────────────────────

def scrape_doordash(lat, lng):
    try:
        from urllib.parse import quote_plus

        results = []
        seen = set()
        queries = [
            "halal restaurant", "halal food", "halal cafe", "halal bakery",
            "middle eastern restaurant", "mediterranean restaurant",
            "pakistani restaurant", "afghan restaurant", "turkish restaurant",
            "shawarma", "kebab", "falafel", "biryani", "indian restaurant"
        ][:max(1, DOORDASH_SEARCH_LIMIT)]

        for query in queries:
            search_url = (
                "https://www.doordash.com/search/store/"
                f"{quote_plus(query)}/?event_type=search&lat={lat}&lng={lng}"
            )
            try:
                page = smart_get(search_url, prefer_stealth=True)
            except Exception as e:
                print(f"DoorDash search skipped for '{query}': {e}")
                continue

            for restaurant in extract_doordash_restaurants(page, lat, lng):
                rid = restaurant.get("id") or restaurant.get("name")
                if rid and rid not in seen:
                    seen.add(rid)
                    results.append(restaurant)
                    if len(results) >= MAX_RESTAURANT_RESULTS:
                        print(f"DoorDash: capped at {len(results)} restaurants")
                        return results

        print(f"DoorDash: found {len(results)} restaurants")
        return results
    except Exception as e:
        print(f"DoorDash error: {e}")
        traceback.print_exc()
        return []


def extract_doordash_restaurants(page, lat, lng):
    restaurants = []

    for a in page.css("a[href*='/store/']"):
        href = a.attrib.get("href", "")
        text = " ".join((a.text or "").split())
        if not href or "/store/" not in href or not text:
            continue
        name = text.split("$")[0].split("•")[0].strip()
        if len(name) < 2 or len(name) > 80:
            continue
        restaurants.append(doordash_restaurant_payload(name, href, lat, lng))

    for script in page.css("script"):
        text = normalize_embedded_json_text(script.text or "")
        if "/store/" not in text and "storeName" not in text:
            continue
        for match in re.finditer(r'"(?:name|storeName|businessName)"\s*:\s*"([^"]{2,80})"([\s\S]{0,1000})', text):
            name = match.group(1)
            if not is_likely_restaurant_name(name):
                continue
            context = match.group(2)
            url_match = re.search(r'"(?:url|storeUrl|canonicalUrl)"\s*:\s*"([^"]*/store/[^"]+)"', context)
            image_match = re.search(r'"(?:imageUrl|image_url|coverImage|headerImageUrl)"\s*:\s*"([^"]+)"', context)
            restaurant = doordash_restaurant_payload(name, url_match.group(1) if url_match else "", lat, lng)
            if image_match:
                restaurant["coverImage"] = image_match.group(1)
                restaurant["galleryPhotos"] = [image_match.group(1)]
            restaurants.append(restaurant)

    return dedupe_restaurants(restaurants)


def doordash_restaurant_payload(name, href, lat, lng):
    from urllib.parse import urljoin

    url = urljoin("https://www.doordash.com", href) if href else ""
    slug = href.split("/store/", 1)[1].split("?")[0].strip("/") if "/store/" in href else name
    return {
        "id": f"doordash-{slug}",
        "name": name,
        "address": "Nearby",
        "latitude": str(lat),
        "longitude": str(lng),
        "cuisine": ["Halal"],
        "rating": "",
        "reviewCount": 0,
        "handSlaughtered": False,
        "restaurantType": 1,
        "coverImage": None,
        "galleryPhotos": [],
        "businessHours": [],
        "doordashUrl": url,
        "halalSummary": {
            "description": "Found on DoorDash - verify halal status",
            "meatHalalStatus": None
        }
    }


def scrape_doordash_enrichment(name, address, lat, lng):
    try:
        store_url = find_doordash_store_url_for(name, address, lat, lng)
        if not store_url:
            print(f"DoorDash enrichment: no store found for '{name}'")
            return empty_doordash_enrichment()

        page = smart_get(store_url, prefer_stealth=True)
        photos = extract_doordash_photos(page)
        rating, review_count = extract_doordash_rating(page)
        phone = extract_first_json_value(page, ["phoneNumber", "phone", "telephone"])
        categories = extract_first_json_value(page, ["businessTags", "storeTags", "cuisine", "category"])

        return {
            "rating": rating,
            "review_count": review_count,
            "phone": phone or "",
            "photos": photos,
            "business_hours": [],
            "doordash_url": store_url,
            "categories": categories or ""
        }
    except Exception as e:
        print(f"DoorDash enrichment error: {e}")
        traceback.print_exc()
        return empty_doordash_enrichment()


def empty_doordash_enrichment():
    return {
        "rating": None,
        "review_count": 0,
        "phone": "",
        "photos": [],
        "business_hours": [],
        "doordash_url": None,
        "categories": ""
    }


def find_doordash_store_url_for(name, address, lat=None, lng=None):
    from urllib.parse import quote_plus, urljoin

    search_query = quote_plus(f"{name} {address}".strip())
    search_urls = [
        f"https://www.doordash.com/search/store/{quote_plus(name)}/?event_type=search",
        f"https://www.doordash.com/search/store/{search_query}/?event_type=search",
        f"https://www.doordash.com/search/store?searchTerm={search_query}",
    ]
    if lat and lng:
        search_urls.insert(
            0,
            f"https://www.doordash.com/search/store/{quote_plus(name)}/?event_type=search&lat={lat}&lng={lng}"
        )

    for search_url in search_urls:
        try:
            page = smart_get(search_url, prefer_stealth=True)
        except Exception as e:
            print(f"DoorDash store search skipped: {e}")
            continue
        store_url = find_doordash_store_url(page, name)
        if store_url:
            return urljoin("https://www.doordash.com", store_url)
    return None


def extract_doordash_photos(page):
    photos = []
    for img in page.css("img[src*='http']"):
        src = img.attrib.get("src", "")
        if is_likely_photo_url(src):
            photos.append(src)

    for script in page.css("script"):
        text = normalize_embedded_json_text(script.text or "")
        for match in re.finditer(r'"(?:imageUrl|image_url|coverImage|headerImageUrl|storeLogoUrl|photoUrl)"\s*:\s*"([^"]+)"', text):
            src = match.group(1)
            if is_likely_photo_url(src):
                photos.append(src)

    return dedupe_strings(photos)[:12]


def extract_doordash_rating(page):
    text = normalize_embedded_json_text(page.body.text if page.body else "")
    rating = None
    review_count = 0

    rating_match = re.search(r'([0-5](?:\.\d)?)\s*(?:stars?|rating)', text, re.IGNORECASE)
    if rating_match:
        rating = float(rating_match.group(1))

    review_match = re.search(r'([\d,]+)\s*(?:ratings?|reviews?)', text, re.IGNORECASE)
    if review_match:
        review_count = int(review_match.group(1).replace(",", ""))

    for script in page.css("script"):
        script_text = normalize_embedded_json_text(script.text or "")
        if rating is None:
            match = re.search(r'"(?:averageRating|rating|starRating)"\s*:\s*([0-5](?:\.\d)?)', script_text)
            if match:
                rating = float(match.group(1))
        if review_count == 0:
            match = re.search(r'"(?:numRatings|ratingCount|reviewCount)"\s*:\s*([\d]+)', script_text)
            if match:
                review_count = int(match.group(1))

    return rating, review_count


def extract_first_json_value(page, keys):
    key_pattern = "|".join(re.escape(key) for key in keys)
    for script in page.css("script"):
        text = normalize_embedded_json_text(script.text or "")
        match = re.search(rf'"(?:{key_pattern})"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    return None


def is_likely_photo_url(url):
    if not url or not isinstance(url, str):
        return False
    lower = url.lower()
    return lower.startswith("http") and any(token in lower for token in (".jpg", ".jpeg", ".png", ".webp", "image", "photo"))


def is_likely_restaurant_name(name):
    if not name:
        return False
    clean = " ".join(str(name).split())
    lower = clean.lower()
    blocked = ["doordash", "delivery", "pickup", "login", "sign up", "cart", "search"]
    return 2 <= len(clean) <= 80 and not any(token in lower for token in blocked)


def dedupe_strings(values):
    seen = set()
    unique = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def dedupe_restaurants(restaurants):
    seen = set()
    unique = []
    for restaurant in restaurants:
        name = " ".join(str(restaurant.get("name", "")).split())
        if not is_likely_restaurant_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        restaurant["name"] = name
        unique.append(restaurant)
    return unique


# ──────────────────────────────────────────────
# MENU SCRAPING
# ──────────────────────────────────────────────

def scrape_menu(name, address, lat, lng):
    menu = scrape_doordash_menu(name, address, lat, lng)
    if menu:
        return menu

    menu = scrape_restaurant_website_menu(name, address, lat, lng)
    if menu:
        return menu

    menu = scrape_google_menu(name, address)
    if menu:
        return menu

    return []


def scrape_doordash_menu(name, address, lat, lng):
    try:
        store_url = find_doordash_store_url_for(name, address, lat, lng)
        if not store_url:
            print(f"DoorDash menu: no store found for '{name}'")
            return None

        print(f"DoorDash menu: fetching {store_url}")
        store_page = smart_get(store_url, prefer_stealth=True)
        categories = extract_menu_from_json(store_page)

        if not any(cat.get("items") for cat in categories):
            categories = extract_doordash_menu_from_scripts(store_page)

        if not any(cat.get("items") for cat in categories):
            categories = parse_doordash_menu_from_text(store_page)

        has_items = any(cat.get("items") for cat in categories)
        print(f"DoorDash menu: {'found' if has_items else 'no'} menu items for '{name}'")
        return categories if has_items else None

    except Exception as e:
        print(f"DoorDash menu error: {e}")
        traceback.print_exc()
        return None


def find_doordash_store_url(page, name):
    name_tokens = [
        token for token in re.split(r"[^a-z0-9]+", (name or "").lower())
        if len(token) > 2
    ]

    for a in page.css("a[href*='/store/']"):
        href = a.attrib.get("href", "")
        text = (a.text or "").lower()
        if not href or "/store/" not in href:
            continue
        if not name_tokens or any(token in text or token in href.lower() for token in name_tokens):
            return href

    for script in page.css("script"):
        text = script.text or ""
        match = re.search(r'"/store/[^"#?]+(?:/[^"#?]+)?"', text)
        if match:
            return match.group(0).strip('"')

    return None


def extract_doordash_menu_from_scripts(page):
    for script in page.css("script"):
        text = script.text or ""
        if not any(token in text.lower() for token in ("menu", "displayprice", "baseprice", "itemname")):
            continue

        normalized = normalize_embedded_json_text(text)
        categories = menu_categories_from_embedded_json(normalized)
        if any(cat.get("items") for cat in categories):
            return categories

        items = menu_items_from_text_blob(normalized)
        if items:
            return [{"category": "DoorDash Menu", "items": items[:40]}]

    return []


def parse_doordash_menu_from_text(page):
    body = page.body.text if page.body else ""
    items = menu_items_from_text_blob(normalize_embedded_json_text(body))
    return [{"category": "DoorDash Menu", "items": items[:40]}] if items else []


def normalize_embedded_json_text(text):
    normalized = text or ""
    replacements = {
        "\\u0026": "&",
        "\\u003c": "<",
        "\\u003e": ">",
        "\\/": "/",
        '\\"': '"',
        "\\'": "'",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized


def menu_categories_from_embedded_json(text):
    categories = []
    for match in re.finditer(r'(\{[^{}]*(?:"name"|"title"|"displayName")[^{}]*(?:"items"|"children"|"entities")[\s\S]{0,12000}?\})', text):
        try:
            data = json.loads(match.group(1))
            found = menu_categories_from_object(data)
            if found:
                categories.extend(found)
        except Exception:
            continue
    return categories


def menu_categories_from_object(data):
    if isinstance(data, list):
        categories = []
        for item in data:
            categories.extend(menu_categories_from_object(item))
        return categories

    if not isinstance(data, dict):
        return []

    category_name = (
        data.get("name")
        or data.get("title")
        or data.get("displayName")
        or data.get("categoryName")
        or "DoorDash Menu"
    )
    for key in ("items", "children", "entities", "menuItems", "menu_items"):
        if key not in data:
            continue
        items = menu_items_from_doordash_object(data[key])
        if items:
            return [{"category": str(category_name)[:60], "items": items[:40]}]

    categories = []
    for value in data.values():
        categories.extend(menu_categories_from_object(value))
    return categories


def menu_items_from_doordash_object(data):
    if isinstance(data, list):
        items = []
        for item in data:
            if isinstance(item, dict):
                menu_item = menu_item_from_doordash_dict(item)
                if menu_item:
                    items.append(menu_item)
                else:
                    items.extend(menu_items_from_doordash_object(item))
        return dedupe_menu_items(items)

    if isinstance(data, dict):
        menu_item = menu_item_from_doordash_dict(data)
        if menu_item:
            return [menu_item]

        items = []
        for value in data.values():
            items.extend(menu_items_from_doordash_object(value))
        return dedupe_menu_items(items)

    return []


def menu_item_from_doordash_dict(data):
    name = (
        data.get("name")
        or data.get("title")
        or data.get("displayName")
        or data.get("itemName")
    )
    if not is_likely_menu_item_name(name):
        return None

    price = (
        data.get("displayPrice")
        or data.get("price")
        or data.get("basePrice")
        or data.get("unitAmount")
        or ""
    )
    if isinstance(price, (int, float)) and price > 100:
        price = f"${price / 100:.2f}"

    description = data.get("description") or data.get("details") or ""
    return {
        "name": str(name),
        "price": str(price),
        "description": str(description)[:200]
    }


def menu_items_from_text_blob(text):
    items = []
    patterns = [
        r'"(?:name|title|displayName|itemName)"\s*:\s*"([^"]{2,80})"([\s\S]{0,700})',
        r'(?:"name"|"title"):"([^"]{2,80})"([\s\S]{0,700})',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            name = match.group(1)
            context = match.group(2)
            if not is_likely_menu_item_name(name):
                continue

            price_match = re.search(r'(?:"displayPrice"|"price"|"basePrice")\s*:\s*"?(\$?[\d,.]+)"?', context)
            desc_match = re.search(r'"(?:description|details)"\s*:\s*"([^"]{3,200})"', context)
            price = price_match.group(1) if price_match else ""
            if price and not price.startswith("$") and price.replace(".", "", 1).isdigit():
                try:
                    numeric = float(price)
                    if numeric > 100:
                        price = f"${numeric / 100:.2f}"
                except ValueError:
                    pass

            items.append({
                "name": name,
                "price": price,
                "description": desc_match.group(1) if desc_match else ""
            })

    return dedupe_menu_items(items)


def scrape_restaurant_website_menu(name, address, lat, lng):
    try:
        query = f"{name} {address} official website".replace(" ", "+")
        search_url = f"https://www.google.com/search?q={query}"
        page = smart_get(search_url, prefer_stealth=True)

        website_url = None
        for a in page.css("a[href*='http']"):
            href = a.attrib.get("href", "")
            skip = ["google.com", "facebook.com", "instagram.com",
                    "tripadvisor.com", "doordash.com", "ubereats.com", "grubhub.com",
                    "youtube.com", "twitter.com", "tiktok.com"]
            if any(s in href for s in skip):
                continue
            if href.startswith("http") and "." in href:
                website_url = href
                break

        if not website_url:
            return None

        print(f"Website menu: trying {website_url}")
        site_page = smart_get(website_url)

        menu_page = site_page
        for a in site_page.css("a"):
            link_text = (a.text or "").lower()
            href = a.attrib.get("href", "")
            if "menu" in link_text or "menu" in href.lower():
                if href.startswith("http"):
                    menu_url = href
                elif href.startswith("/"):
                    from urllib.parse import urljoin
                    menu_url = urljoin(website_url, href)
                else:
                    continue
                menu_page = smart_get(menu_url)
                break

        categories = []
        current_cat = None

        for section in menu_page.css("[class*='menu'], [id*='menu'], section, article"):
            heading = section.css_first("h2, h3, h4")
            if heading and heading.text and len(heading.text.strip()) < 60:
                current_cat = {"category": heading.text.strip(), "items": []}
                categories.append(current_cat)

            for item_el in section.css("li, [class*='item'], [class*='dish'], tr, .row"):
                name_el = item_el.css_first("h3, h4, h5, strong, b, [class*='name'], [class*='title'], td:first-child")
                price_el = item_el.css_first("[class*='price'], .price, td:last-child")
                desc_el = item_el.css_first("p, [class*='desc'], span, td:nth-child(2)")

                item_name = (name_el.text or "").strip() if name_el else ""
                if item_name and len(item_name) > 2 and len(item_name) < 80:
                    item = {
                        "name": item_name,
                        "price": extract_price(price_el.text if price_el else ""),
                        "description": (desc_el.text or "").strip()[:200] if desc_el else ""
                    }
                    if current_cat:
                        current_cat["items"].append(item)
                    else:
                        current_cat = {"category": "Menu", "items": [item]}
                        categories.append(current_cat)

        if not any(cat.get("items") for cat in categories):
            categories = parse_menu_from_text(menu_page)

        has_items = any(cat.get("items") for cat in categories)
        print(f"Website menu: {'found' if has_items else 'no'} items")
        return categories if has_items else None

    except Exception as e:
        print(f"Website menu error: {e}")
        traceback.print_exc()
        return None


def scrape_google_menu(name, address):
    try:
        query = f"{name} {address} menu prices".replace(" ", "+")
        url = f"https://www.google.com/search?q={query}"
        page = smart_get(url, prefer_stealth=True)

        items = []

        for el in page.css("[data-attrid*='menu'], [class*='menu'] [class*='item']"):
            name_el = el.css_first("[class*='name'], [class*='title'], span")
            price_el = el.css_first("[class*='price'], [class*='cost']")
            desc_el = el.css_first("[class*='desc'], [class*='detail']")

            item_name = (name_el.text or "").strip() if name_el else ""
            if item_name and len(item_name) > 2:
                items.append({
                    "name": item_name,
                    "price": extract_price(price_el.text if price_el else ""),
                    "description": (desc_el.text or "").strip() if desc_el else ""
                })

        if not items:
            for el in page.css("span, div, li"):
                text = (el.text or "").strip()
                if "$" in text and len(text) > 4 and len(text) < 80:
                    name_part, price_part = split_name_price(text)
                    if name_part and len(name_part) > 2:
                        items.append({"name": name_part, "price": price_part, "description": ""})

        if items:
            print(f"Google menu: found {len(items)} items")
            return [{"category": "Menu", "items": items[:25]}]
        return []

    except Exception as e:
        print(f"Google menu error: {e}")
        traceback.print_exc()
        return []


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def extract_menu_from_json(page):
    categories = []
    for script in page.css("script[type='application/json'], script[type='application/ld+json']"):
        text = script.text or ""
        try:
            data = json.loads(text)
            items = find_menu_items(data)
            if items:
                categories.append({"category": "Menu", "items": items[:30]})
                break
        except Exception:
            pass
    return categories


def find_menu_items(data, depth=0):
    return find_menu_items_in_context(data, depth=depth)


def find_menu_items_in_context(data, depth=0, in_menu_context=False):
    if depth > 10:
        return []
    items = []
    if isinstance(data, dict):
        menu_context = in_menu_context or any(
            token in str(key).lower()
            for key in data.keys()
            for token in ("menu", "dish", "food", "popular", "item")
        )
        has_name = any(key in data for key in ("name", "title", "itemName", "dishName", "displayName", "label"))
        has_price = "price" in data or "cost" in data or "amount" in data
        has_desc = "description" in data or "desc" in data

        if has_name and (has_price or has_desc or menu_context):
            name = str(
                data.get("name")
                or data.get("title")
                or data.get("itemName")
                or data.get("dishName")
                or data.get("displayName")
                or data.get("label")
                or ""
            )
            price = data.get("price") or data.get("cost") or data.get("amount", "")
            if isinstance(price, dict):
                price = price.get("amount") or price.get("formatted") or price.get("display", "")
            desc = str(data.get("description") or data.get("desc", ""))

            if is_likely_menu_item_name(name):
                items.append({"name": name, "price": str(price), "description": desc[:200]})

        if "hasMenuSection" in data:
            sections = data["hasMenuSection"]
            if not isinstance(sections, list):
                sections = [sections]
            for section in sections:
                if not isinstance(section, dict):
                    continue
                menu_items = section.get("hasMenuItem", [])
                if not isinstance(menu_items, list):
                    menu_items = [menu_items]
                for menu_item in menu_items:
                    if isinstance(menu_item, dict) and menu_item.get("name"):
                        offer = menu_item.get("offers", {})
                        items.append({
                            "name": menu_item["name"],
                            "price": str(offer.get("price", "")) if isinstance(offer, dict) else "",
                            "description": str(menu_item.get("description", ""))[:200]
                        })

        for key, value in data.items():
            key_context = menu_context or any(token in str(key).lower() for token in ("menu", "dish", "food", "popular", "item"))
            items.extend(find_menu_items_in_context(value, depth + 1, key_context))
    elif isinstance(data, list):
        for item in data:
            items.extend(find_menu_items_in_context(item, depth + 1, in_menu_context))
    return dedupe_menu_items(items)


def is_likely_menu_item_name(name):
    if not name:
        return False

    clean = " ".join(str(name).split())
    if len(clean) < 2 or len(clean) > 80:
        return False

    lower = clean.lower()
    blocked = [
        "reviews", "review", "restaurants", "restaurant", "directions",
        "phone", "website", "menu", "home", "photos", "see all", "write a review",
        "start order", "claim this business", "hours", "location", "sign up", "log in"
    ]
    if any(blocked_text == lower or blocked_text in lower for blocked_text in blocked):
        return False

    if lower.startswith(("http", "www.")) or "@" in lower:
        return False

    return bool(re.search(r"[A-Za-z]", clean))


def dedupe_menu_items(items):
    seen = set()
    unique = []
    for item in items:
        name = " ".join(str(item.get("name", "")).split())
        if not is_likely_menu_item_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        item["name"] = name
        unique.append(item)
    return unique


def parse_menu_from_text(page):
    raw = page.body.text if page.body else ""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    items = []
    for line in lines:
        if "$" in line and len(line) > 4 and len(line) < 100:
            name_part, price_part = split_name_price(line)
            if name_part and len(name_part) > 2:
                items.append({"name": name_part, "price": price_part, "description": ""})
    if items:
        return [{"category": "Menu", "items": items[:25]}]
    return []


def extract_popular_from_biz(page):
    items = []

    for el in page.css("[class*='popular'], [class*='highlight'], [class*='dish'], [aria-label*='menu']"):
        name_el = el.css_first("p, span, h4, [class*='name']")
        price_el = el.css_first("[class*='price']")

        if name_el and name_el.text:
            text = name_el.text.strip()
            if len(text) > 2 and len(text) < 60:
                items.append({
                    "name": text,
                    "price": (price_el.text or "").strip() if price_el else "",
                    "description": ""
                })

    for script in page.css("script[type='application/json']"):
        text = script.text or ""
        try:
            data = json.loads(text)
            json_items = find_menu_items(data)
            items.extend(json_items)
        except Exception:
            pass

    unique = dedupe_menu_items(items)

    if unique:
        return [{"category": "Popular Items", "items": unique[:20]}]
    return []


def extract_price(text):
    if not text:
        return ""
    match = re.search(r'\$[\d,.]+', text)
    return match.group() if match else ""


def split_name_price(text):
    match = re.search(r'\$[\d,.]+', text)
    if match:
        price = match.group()
        name = text[:match.start()].strip().rstrip("-").rstrip(".").rstrip("…")
        return name or text, price
    return text, ""


def format_time(military):
    if len(military) != 4:
        return military
    try:
        hour, minute = int(military[:2]), int(military[2:])
        period = "PM" if hour >= 12 else "AM"
        display = 12 if hour == 0 else (hour - 12 if hour > 12 else hour)
        return f"{display}:{minute:02d} {period}"
    except ValueError:
        return military


def merge_results(zabihah, doordash):
    merged = list(zabihah)
    for dr in doordash:
        d_name = " ".join(str(dr.get("name", "")).lower().split())
        d_lat = float_or_none(dr.get("latitude"))
        d_lng = float_or_none(dr.get("longitude"))
        is_dup = False

        for zr in merged:
            z_name = " ".join(str(zr.get("name", "")).lower().split())
            z_lat = float_or_none(zr.get("latitude"))
            z_lng = float_or_none(zr.get("longitude"))

            name_match = names_match(z_name, d_name)
            distance_match = (
                z_lat is not None and z_lng is not None and
                d_lat is not None and d_lng is not None and
                ((d_lat - z_lat)**2 + (d_lng - z_lng)**2)**0.5 < 0.002
            )

            if name_match and (distance_match or z_lat is None or d_lat is None):
                if not zr.get("rating") and dr.get("rating"):
                    zr["rating"] = dr["rating"]
                if not zr.get("reviewCount") and dr.get("reviewCount"):
                    zr["reviewCount"] = dr["reviewCount"]
                if not zr.get("coverImage") and dr.get("coverImage"):
                    zr["coverImage"] = dr["coverImage"]
                if not zr.get("galleryPhotos") and dr.get("galleryPhotos"):
                    zr["galleryPhotos"] = dr["galleryPhotos"]
                if not zr.get("businessHours") and dr.get("businessHours"):
                    zr["businessHours"] = dr["businessHours"]
                is_dup = True
                break

        if not is_dup:
            merged.append(dr)

    return merged


def names_match(left, right):
    if len(left) < 4 or len(right) < 4:
        return False
    left_prefix = left[:8]
    right_prefix = right[:8]
    return left_prefix in right or right_prefix in left


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True
    )
