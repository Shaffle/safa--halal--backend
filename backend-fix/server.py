from collections import OrderedDict
from flask import Flask, jsonify, request
from scrapling import Fetcher
import json
import math
import os
import queue
import re
import threading
import time
import traceback
from urllib.parse import quote_plus, urljoin

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

try:
    Fetcher.configure(auto_match=True)
except Exception:
    pass
fetcher = Fetcher()

stealth = None
try:
    from scrapling import StealthyFetcher
    try:
        StealthyFetcher.configure(auto_match=True)
    except Exception:
        pass
    stealth = StealthyFetcher()
    print("StealthyFetcher available")
except Exception as exc:
    print(f"StealthyFetcher not available ({exc}), using Fetcher only")


@app.route("/", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"})


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


def smart_get(url, prefer_stealth=False):
    if prefer_stealth and stealth:
        try:
            return bounded_fetch(stealth.get, url)
        except TimeoutError:
            raise
        except Exception as exc:
            print(f"StealthyFetcher failed for {url}: {exc}")
    return bounded_fetch(fetcher.get, url)


def request_payload():
    payload = request.get_json(silent=True) or {}
    return payload if isinstance(payload, dict) else {}


def sanitize_text(value, max_length=160):
    return " ".join(str(value or "").split())[:max_length]


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
        response = {"restaurants": merge_results(zabihah, doordash)}
        cache_set(key, response)
        return jsonify(response)
    except Exception as exc:
        print(f"Error in /api/restaurants: {exc}")
        traceback.print_exc()
        return jsonify({"restaurants": [], "error": "restaurant lookup failed"}), 500


@app.route("/api/zabihah", methods=["POST"])
def zabihah_only():
    payload = request_payload()
    lat, lng = parse_coordinates(payload)
    if lat is None or lng is None:
        return jsonify({"error": "valid latitude and longitude required"}), 400
    return jsonify({"restaurants": scrape_zabihah(lat, lng)})


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
        if any(category.get("items") for category in menu):
            cache_set(key, response)
        return jsonify(response)
    except Exception as exc:
        print(f"Error in /api/menu: {exc}")
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
        return jsonify({"enrichment": scrape_doordash_enrichment(name, address, lat, lng)})
    except Exception as exc:
        print(f"Error in /api/doordash/enrich: {exc}")
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
        return jsonify({"photos": scrape_doordash_enrichment(name, address, lat, lng).get("photos", [])})
    except Exception as exc:
        print(f"Error in /api/doordash/photos: {exc}")
        traceback.print_exc()
        return jsonify({"photos": [], "error": "DoorDash photo lookup failed"}), 500


def scrape_zabihah(lat, lng):
    try:
        page = smart_get(f"https://www.zabihah.com/search?lat={lat}&lng={lng}")
        html = ""
        for script in page.css("script"):
            text = script.text or ""
            if "initialRestaurants" in text:
                html = text
                break
        if not html:
            body = page.body
            html = body.html if body else ""
        if "initialRestaurants" not in html:
            return []
        restaurants = extract_json_array(html.replace('\\"', '"').replace("\\\\", "\\"), "initialRestaurants")
        return restaurants[:MAX_RESTAURANT_RESULTS] if restaurants else []
    except Exception as exc:
        print(f"Zabihah error: {exc}")
        traceback.print_exc()
        return []


def extract_json_array(text, key):
    marker = f'"{key}":'
    idx = text.find(marker)
    if idx == -1:
        return None
    start = text.find("[", idx + len(marker))
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(start + 500000, len(text))):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def scrape_doordash(lat, lng):
    results = []
    seen = set()
    queries = [
        "halal restaurant", "halal food", "middle eastern restaurant",
        "mediterranean restaurant", "pakistani restaurant", "shawarma", "kebab"
    ][:max(1, DOORDASH_SEARCH_LIMIT)]

    for query in queries:
        url = f"https://www.doordash.com/search/store/{quote_plus(query)}/?event_type=search&lat={lat}&lng={lng}"
        try:
            page = smart_get(url, prefer_stealth=True)
        except Exception as exc:
            print(f"DoorDash search skipped for '{query}': {exc}")
            continue
        for restaurant in extract_doordash_restaurants(page, lat, lng):
            rid = restaurant.get("id") or restaurant.get("name")
            if rid and rid not in seen:
                seen.add(rid)
                results.append(restaurant)
                if len(results) >= MAX_RESTAURANT_RESULTS:
                    return results
    return results


def extract_doordash_restaurants(page, lat, lng):
    restaurants = []
    for anchor in page.css("a[href*='/store/']"):
        href = anchor.attrib.get("href", "")
        text = " ".join((anchor.text or "").split())
        name = text.split("$")[0].split("*")[0].strip()
        if not href or "/store/" not in href or not is_likely_restaurant_name(name):
            continue
        restaurants.append(doordash_restaurant_payload(name, href, lat, lng))

    for script in page.css("script"):
        text = normalize_embedded_text(script.text or "")
        if "/store/" not in text and "storeName" not in text:
            continue
        for match in re.finditer(r'"(?:name|storeName|businessName)"\s*:\s*"([^"]{2,80})"([\s\S]{0,1200})', text):
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
    url = urljoin("https://www.doordash.com", href) if href else ""
    slug = href.split("/store/", 1)[1].split("?")[0].strip("/") if "/store/" in href else re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
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
        "halalSummary": {"description": "Found on DoorDash - verify halal status", "meatHalalStatus": None}
    }


def scrape_menu(name, address, lat, lng):
    menu = scrape_doordash_menu(name, address, lat, lng)
    if menu:
        return menu
    menu = scrape_restaurant_website_menu(name, address)
    return menu if menu else []


def scrape_doordash_menu(name, address, lat, lng):
    try:
        store_url = find_doordash_store_url_for(name, address, lat, lng)
        if not store_url:
            print(f"DoorDash menu: no store found for '{name}'")
            return []

        page = smart_get(store_url, prefer_stealth=True)
        categories = extract_menu_from_json_scripts(page)
        if any(category.get("items") for category in categories):
            return categories

        items = menu_items_from_text_blob(normalize_embedded_text(page.body.text if page.body else ""))
        if items:
            return [{"category": "DoorDash Menu", "items": items[:40]}]

        return [{
            "category": "DoorDash",
            "items": [{"name": "View menu on DoorDash", "price": "", "description": store_url}]
        }]
    except Exception as exc:
        print(f"DoorDash menu error: {exc}")
        traceback.print_exc()
        return []


def find_doordash_store_url_for(name, address, lat=None, lng=None):
    terms = [name, f"{name} {address}".strip()]
    for term in terms:
        urls = [f"https://www.doordash.com/search/store/{quote_plus(term)}/?event_type=search"]
        if lat is not None and lng is not None:
            urls.insert(0, f"https://www.doordash.com/search/store/{quote_plus(term)}/?event_type=search&lat={lat}&lng={lng}")
        for url in urls:
            try:
                page = smart_get(url, prefer_stealth=True)
            except Exception as exc:
                print(f"DoorDash store search skipped: {exc}")
                continue
            href = find_doordash_store_url(page, name)
            if href:
                return urljoin("https://www.doordash.com", href)
    return None


def find_doordash_store_url(page, name):
    tokens = [token for token in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(token) > 2]
    for anchor in page.css("a[href*='/store/']"):
        href = anchor.attrib.get("href", "")
        text = (anchor.text or "").lower()
        if href and (not tokens or any(token in text or token in href.lower() for token in tokens)):
            return href
    for script in page.css("script"):
        text = normalize_embedded_text(script.text or "")
        for match in re.finditer(r'"([^"]*/store/[^"#?]+(?:/[^"#?]+)?)"', text):
            href = match.group(1)
            lower = href.lower()
            if not tokens or any(token in lower for token in tokens):
                return href
    return None


def scrape_doordash_enrichment(name, address, lat, lng):
    store_url = find_doordash_store_url_for(name, address, lat, lng)
    if not store_url:
        return empty_doordash_enrichment()
    try:
        page = smart_get(store_url, prefer_stealth=True)
        return {
            "rating": None,
            "review_count": 0,
            "phone": extract_first_json_value(page, ["phoneNumber", "phone", "telephone"]) or "",
            "photos": extract_doordash_photos(page),
            "business_hours": [],
            "doordash_url": store_url,
            "categories": extract_first_json_value(page, ["businessTags", "storeTags", "cuisine", "category"]) or ""
        }
    except Exception:
        return empty_doordash_enrichment()


def empty_doordash_enrichment():
    return {"rating": None, "review_count": 0, "phone": "", "photos": [], "business_hours": [], "doordash_url": None, "categories": ""}


def extract_doordash_photos(page):
    photos = []
    for img in page.css("img[src*='http']"):
        src = img.attrib.get("src", "")
        if is_likely_photo_url(src):
            photos.append(src)
    for script in page.css("script"):
        text = normalize_embedded_text(script.text or "")
        for match in re.finditer(r'"(?:imageUrl|image_url|coverImage|headerImageUrl|storeLogoUrl|photoUrl)"\s*:\s*"([^"]+)"', text):
            src = match.group(1)
            if is_likely_photo_url(src):
                photos.append(src)
    return dedupe_strings(photos)[:12]


def extract_menu_from_json_scripts(page):
    for script in page.css("script[type='application/json'], script[type='application/ld+json'], script"):
        text = normalize_embedded_text(script.text or "")
        if not any(token in text.lower() for token in ("menu", "displayprice", "baseprice", "itemname", "hasmenuitem")):
            continue
        categories = []
        for obj in json_objects_from_text(text):
            categories.extend(menu_categories_from_object(obj))
        if categories:
            return categories[:12]
        items = menu_items_from_text_blob(text)
        if items:
            return [{"category": "Menu", "items": items[:40]}]
    return []


def json_objects_from_text(text):
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return [json.loads(stripped)]
        except Exception:
            pass
    objects = []
    for match in re.finditer(r'(\{[^{}]*(?:"name"|"title"|"displayName"|"itemName")[\s\S]{0,4000}?\})', text):
        try:
            objects.append(json.loads(match.group(1)))
        except Exception:
            continue
    return objects


def menu_categories_from_object(data):
    if isinstance(data, list):
        categories = []
        for item in data:
            categories.extend(menu_categories_from_object(item))
        return categories
    if not isinstance(data, dict):
        return []

    category_name = data.get("name") or data.get("title") or data.get("displayName") or data.get("categoryName") or "Menu"
    for key in ("items", "children", "entities", "menuItems", "menu_items", "hasMenuItem"):
        if key in data:
            items = menu_items_from_object(data[key])
            if items:
                return [{"category": str(category_name)[:60], "items": items[:40]}]

    categories = []
    for value in data.values():
        categories.extend(menu_categories_from_object(value))
    return categories


def menu_items_from_object(data):
    if isinstance(data, list):
        items = []
        for value in data:
            items.extend(menu_items_from_object(value))
        return dedupe_menu_items(items)
    if isinstance(data, dict):
        item = menu_item_from_dict(data)
        if item:
            return [item]
        items = []
        for value in data.values():
            items.extend(menu_items_from_object(value))
        return dedupe_menu_items(items)
    return []


def menu_item_from_dict(data):
    name = data.get("name") or data.get("title") or data.get("displayName") or data.get("itemName") or data.get("label")
    if not is_likely_menu_item_name(name):
        return None
    price = data.get("displayPrice") or data.get("price") or data.get("basePrice") or data.get("unitAmount") or ""
    if isinstance(price, dict):
        price = price.get("display") or price.get("formatted") or price.get("amount") or ""
    if isinstance(price, (int, float)) and price > 100:
        price = f"${price / 100:.2f}"
    description = data.get("description") or data.get("details") or data.get("summary") or ""
    return {"name": str(name), "price": str(price), "description": str(description)[:200]}


def menu_items_from_text_blob(text):
    items = []
    patterns = [
        r'"(?:name|title|displayName|itemName)"\s*:\s*"([^"]{2,80})"([\s\S]{0,900})',
        r'(?<![A-Za-z])([A-Z][A-Za-z][A-Za-z &\'-]{2,60})\s+(\$\d+(?:\.\d{2})?)'
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            name = match.group(1)
            if not is_likely_menu_item_name(name):
                continue
            if pattern.startswith("(?<!"):
                items.append({"name": name, "price": match.group(2), "description": ""})
                continue
            context = match.group(2)
            price_match = re.search(r'(?:"displayPrice"|"price"|"basePrice")\s*:\s*"?(\$?[\d,.]+)"?', context)
            desc_match = re.search(r'"(?:description|details|summary)"\s*:\s*"([^"]{3,200})"', context)
            items.append({"name": name, "price": price_match.group(1) if price_match else "", "description": desc_match.group(1) if desc_match else ""})
    return dedupe_menu_items(items)


def scrape_restaurant_website_menu(name, address):
    try:
        page = smart_get(f"https://duckduckgo.com/html/?q={quote_plus(name + ' ' + address + ' menu')}")
        blocked = ("duckduckgo.com", "google.com", "facebook.com", "instagram.com", "tripadvisor.com", "doordash.com", "ubereats.com", "grubhub.com", "youtube.com", "twitter.com", "tiktok.com")
        website_url = None
        for anchor in page.css("a[href*='http']"):
            href = anchor.attrib.get("href", "")
            if href.startswith("http") and not any(domain in href for domain in blocked):
                website_url = href
                break
        if not website_url:
            return []
        site_page = smart_get(website_url)
        items = menu_items_from_text_blob(site_page.body.text if site_page.body else "")
        return [{"category": "Menu", "items": items[:30]}] if items else []
    except Exception:
        return []


def normalize_embedded_text(text):
    normalized = text or ""
    replacements = {"\\u0026": "&", "\\u003c": "<", "\\u003e": ">", "\\/": "/", '\\"': '"', "\\'": "'"}
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalized


def extract_first_json_value(page, keys):
    key_pattern = "|".join(re.escape(key) for key in keys)
    for script in page.css("script"):
        match = re.search(rf'"(?:{key_pattern})"\s*:\s*"([^"]+)"', normalize_embedded_text(script.text or ""))
        if match:
            return match.group(1)
    return None


def is_likely_photo_url(url):
    lower = str(url or "").lower()
    return lower.startswith("http") and any(token in lower for token in (".jpg", ".jpeg", ".png", ".webp", "image", "photo"))


def is_likely_restaurant_name(name):
    clean = " ".join(str(name or "").split())
    lower = clean.lower()
    blocked = ["doordash", "delivery", "pickup", "login", "sign up", "cart", "search", "sponsored"]
    return 2 <= len(clean) <= 80 and not any(token in lower for token in blocked)


def is_likely_menu_item_name(name):
    clean = " ".join(str(name or "").split())
    if len(clean) < 2 or len(clean) > 80:
        return False
    lower = clean.lower()
    blocked = ["reviews", "review", "restaurants", "restaurant", "directions", "phone", "website", "menu", "home", "photos", "see all", "write a review", "start order", "claim this business", "hours", "location", "sign up", "log in"]
    if any(blocked_text == lower or blocked_text in lower for blocked_text in blocked):
        return False
    return not lower.startswith(("http", "www.")) and "@" not in lower and bool(re.search(r"[A-Za-z]", clean))


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


def float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def names_match(left, right):
    left = " ".join(str(left or "").lower().split())
    right = " ".join(str(right or "").lower().split())
    if len(left) < 4 or len(right) < 4:
        return False
    return left[:8] in right or right[:8] in left


def merge_results(zabihah, doordash):
    merged = list(zabihah)
    for dr in doordash:
        d_lat = float_or_none(dr.get("latitude"))
        d_lng = float_or_none(dr.get("longitude"))
        duplicate = False
        for zr in merged:
            z_lat = float_or_none(zr.get("latitude"))
            z_lng = float_or_none(zr.get("longitude"))
            distance_match = z_lat is not None and z_lng is not None and d_lat is not None and d_lng is not None and ((d_lat - z_lat) ** 2 + (d_lng - z_lng) ** 2) ** 0.5 < 0.002
            if names_match(zr.get("name"), dr.get("name")) and (distance_match or z_lat is None or d_lat is None):
                for key in ("rating", "reviewCount", "coverImage", "galleryPhotos", "businessHours", "doordashUrl"):
                    if not zr.get(key) and dr.get(key):
                        zr[key] = dr[key]
                duplicate = True
                break
        if not duplicate:
            merged.append(dr)
    return merged[:MAX_RESTAURANT_RESULTS]


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
