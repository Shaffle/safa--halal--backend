from flask import Flask, request, jsonify
from scrapling import Fetcher, StealthyFetcher
import json
import re

app = Flask(__name__)

# Scrapling fetchers with auto_match for resilient element matching
# even when site structure changes
fetcher = Fetcher(auto_match=True)
stealth = StealthFetcher(auto_match=True)


# ──────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────

@app.route("/api/restaurants", methods=["POST"])
def get_restaurants():
    lat = request.json.get("latitude")
    lng = request.json.get("longitude")
    if not lat or not lng:
        return jsonify({"error": "latitude and longitude required"}), 400

    zabihah = scrape_zabihah(lat, lng)
    yelp = scrape_yelp(lat, lng)
    merged = merge_results(zabihah, yelp)
    return jsonify({"restaurants": merged})


@app.route("/api/zabihah", methods=["POST"])
def zabihah_only():
    lat = request.json.get("latitude")
    lng = request.json.get("longitude")
    if not lat or not lng:
        return jsonify({"error": "latitude and longitude required"}), 400
    return jsonify({"restaurants": scrape_zabihah(lat, lng)})


@app.route("/api/menu", methods=["POST"])
def get_menu():
    name = request.json.get("name", "")
    address = request.json.get("address", "")
    lat = request.json.get("latitude")
    lng = request.json.get("longitude")
    if not name:
        return jsonify({"error": "name is required"}), 400
    menu = scrape_menu(name, address, lat, lng)
    return jsonify({"menu": menu})


# ──────────────────────────────────────────────
# ZABIHAH SCRAPING
# ──────────────────────────────────────────────

def scrape_zabihah(lat, lng):
    try:
        url = f"https://www.zabihah.com/search?lat={lat}&lng={lng}"
        page = fetcher.get(url)

        # Extract embedded JSON from script tags using Scrapling CSS selectors
        for script in page.css("script"):
            text = script.text or ""
            if "initialRestaurants" in text:
                match = re.search(r'"initialRestaurants":(\[.*?\])', text, re.DOTALL)
                if match:
                    restaurants = json.loads(match.group(1))
                    return enrich_zabihah(restaurants, page)

        # Fallback: try parsing HTML cards directly with Scrapling
        return parse_zabihah_html(page)
    except Exception as e:
        print(f"Zabihah error: {e}")
        return []


def enrich_zabihah(restaurants, page):
    """Enrich Zabihah JSON data with any additional HTML-scraped info"""
    try:
        # Use Scrapling to find additional details from the page
        for card in page.css("[class*='restaurant'], [class*='listing'], [data-id]"):
            data_id = card.attrib.get("data-id", "")
            if not data_id:
                continue
            for r in restaurants:
                if r.get("id") == data_id:
                    # Extract any extra info Scrapling finds
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
    """Fallback HTML parsing when JSON is not available"""
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
                "latitude": "0",
                "longitude": "0",
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
# YELP SCRAPING (StealthFetcher for anti-bot)
# ──────────────────────────────────────────────

def scrape_yelp(lat, lng):
    try:
        results = []
        seen = set()

        for query in ["halal+restaurant", "halal+food", "mediterranean+restaurant"]:
            url = f"https://www.yelp.com/search?find_desc={query}&latitude={lat}&longitude={lng}"
            # Use StealthFetcher to bypass Yelp's bot detection
            page = stealth.get(url)

            # Strategy 1: Extract from embedded JSON (most reliable)
            for script in page.css("script[type='application/json']"):
                text = script.text or ""
                try:
                    data = json.loads(text)
                    for biz in find_businesses(data):
                        bid = biz.get("id") or biz.get("bizId", "")
                        if bid and bid not in seen:
                            seen.add(bid)
                            results.append(normalize_yelp(biz))
                except (json.JSONDecodeError, Exception):
                    pass

            # Strategy 2: Extract from JSON-LD structured data
            for script in page.css("script[type='application/ld+json']"):
                text = script.text or ""
                try:
                    ld = json.loads(text)
                    if isinstance(ld, list):
                        for item in ld:
                            biz = extract_ld_business(item)
                            if biz and biz.get("id") not in seen:
                                seen.add(biz["id"])
                                results.append(biz)
                    elif isinstance(ld, dict):
                        biz = extract_ld_business(ld)
                        if biz and biz.get("id") not in seen:
                            seen.add(biz["id"])
                            results.append(biz)
                except Exception:
                    pass

            # Strategy 3: Parse HTML cards directly with Scrapling selectors
            if not results:
                for card in page.css("[data-testid*='serp'], [class*='container'] [class*='business']"):
                    try:
                        name_el = card.css_first("a[href*='/biz/'], h3, [class*='businessName']")
                        rating_el = card.css_first("[aria-label*='star'], [class*='rating']")
                        review_el = card.css_first("[class*='reviewCount'], span")
                        addr_el = card.css_first("[class*='address'], address, span[class*='secondary']")
                        img_el = card.css_first("img[src*='http']")

                        if not name_el:
                            continue

                        name = (name_el.text or "").strip()
                        if not name or name in seen:
                            continue
                        seen.add(name)

                        rating = ""
                        if rating_el:
                            label = rating_el.attrib.get("aria-label", "")
                            rmatch = re.search(r'([\d.]+)\s*star', label)
                            rating = rmatch.group(1) if rmatch else ""

                        review_count = 0
                        if review_el:
                            rmatch = re.search(r'(\d+)', review_el.text or "")
                            review_count = int(rmatch.group(1)) if rmatch else 0

                        results.append({
                            "id": f"yelp-html-{len(results)}",
                            "name": name,
                            "address": (addr_el.text or "").strip() if addr_el else "Nearby",
                            "latitude": str(lat),
                            "longitude": str(lng),
                            "cuisine": ["Halal"],
                            "rating": rating,
                            "reviewCount": review_count,
                            "handSlaughtered": False,
                            "restaurantType": 1,
                            "coverImage": img_el.attrib.get("src", "") if img_el else None,
                            "galleryPhotos": [],
                            "businessHours": [],
                            "halalSummary": {
                                "description": "Found on Yelp - verify halal status",
                                "meatHalalStatus": None
                            }
                        })
                    except Exception:
                        continue

        return results
    except Exception as e:
        print(f"Yelp error: {e}")
        return []


def extract_ld_business(ld):
    """Extract business from JSON-LD structured data"""
    if ld.get("@type") not in ("Restaurant", "FoodEstablishment", "LocalBusiness"):
        return None
    geo = ld.get("geo", {})
    addr = ld.get("address", {})
    return {
        "id": f"yelp-ld-{ld.get('name', '')}",
        "name": ld.get("name", ""),
        "address": f"{addr.get('streetAddress', '')}, {addr.get('addressLocality', '')}",
        "latitude": str(geo.get("latitude", 0)),
        "longitude": str(geo.get("longitude", 0)),
        "cuisine": [ld.get("servesCuisine", "Halal")] if ld.get("servesCuisine") else ["Halal"],
        "rating": str(ld.get("aggregateRating", {}).get("ratingValue", "")),
        "reviewCount": ld.get("aggregateRating", {}).get("reviewCount", 0),
        "handSlaughtered": False,
        "restaurantType": 1,
        "coverImage": ld.get("image"),
        "galleryPhotos": [],
        "businessHours": [],
        "halalSummary": {
            "description": "Found on Yelp - verify halal status",
            "meatHalalStatus": None
        }
    }


def find_businesses(data, depth=0):
    """Recursively find business objects in nested JSON"""
    if depth > 10:
        return []
    if isinstance(data, dict):
        if "name" in data and ("rating" in data or "reviewCount" in data or "review_count" in data):
            if "coordinates" in data or "latitude" in data:
                return [data]
        results = []
        for v in data.values():
            results.extend(find_businesses(v, depth + 1))
        return results
    elif isinstance(data, list):
        results = []
        for item in data:
            results.extend(find_businesses(item, depth + 1))
        return results
    return []


def normalize_yelp(biz):
    """Normalize Yelp business data to our standard format"""
    coords = biz.get("coordinates", {})
    location = biz.get("location", {})
    categories = biz.get("categories", [])

    parts = [location.get("address1"), location.get("city"), location.get("state")]
    address = ", ".join(p for p in parts if p) or "Nearby"

    photos = []
    if biz.get("photos"):
        photos = biz["photos"]
    elif biz.get("imageUrl") or biz.get("image_url"):
        photos = [biz.get("imageUrl") or biz.get("image_url")]

    cuisine = []
    for cat in categories:
        if isinstance(cat, dict):
            cuisine.append(cat.get("title", cat.get("alias", "")))
        elif isinstance(cat, str):
            cuisine.append(cat)

    hours = []
    for h in biz.get("hours", []):
        for slot in h.get("open", []):
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            day_idx = slot.get("day", 0)
            day = day_names[day_idx] if day_idx < len(day_names) else "Unknown"
            start = format_time(slot.get("start", ""))
            end = format_time(slot.get("end", ""))
            hours.append({"day": day, "hours": f"{start} - {end}"})

    return {
        "id": f"yelp-{biz.get('id', '')}",
        "name": biz.get("name", ""),
        "address": address,
        "latitude": str(coords.get("latitude", biz.get("latitude", 0))),
        "longitude": str(coords.get("longitude", biz.get("longitude", 0))),
        "cuisine": cuisine or ["Halal"],
        "rating": str(biz.get("rating", "")),
        "reviewCount": biz.get("reviewCount") or biz.get("review_count", 0),
        "handSlaughtered": False,
        "restaurantType": 1,
        "coverImage": photos[0] if photos else None,
        "galleryPhotos": photos[1:] if len(photos) > 1 else [],
        "businessHours": hours,
        "halalSummary": {
            "description": "Found on Yelp - verify halal status",
            "meatHalalStatus": None
        }
    }


# ──────────────────────────────────────────────
# MENU SCRAPING (multi-source with Scrapling)
# ──────────────────────────────────────────────

def scrape_menu(name, address, lat, lng):
    # Try multiple sources in order
    menu = scrape_yelp_menu(name, address, lat, lng)
    if menu:
        return menu

    menu = scrape_restaurant_website_menu(name, address, lat, lng)
    if menu:
        return menu

    menu = scrape_google_menu(name, address)
    if menu:
        return menu

    return []


def scrape_yelp_menu(name, address, lat, lng):
    """Scrape menu from Yelp using StealthFetcher"""
    try:
        query = f"{name}".replace(" ", "+")
        url = f"https://www.yelp.com/search?find_desc={query}&latitude={lat}&longitude={lng}"
        page = stealth.get(url)

        # Find the business alias from search results
        biz_alias = None

        # Method 1: From <a> tags
        for a in page.css("a[href*='/biz/']"):
            href = a.attrib.get("href", "")
            if "/biz/" in href:
                alias = href.split("/biz/")[1].split("?")[0].split("#")[0]
                if alias and len(alias) > 2:
                    biz_alias = alias
                    break

        # Method 2: From embedded JSON
        if not biz_alias:
            for script in page.css("script[type='application/json']"):
                text = script.text or ""
                try:
                    data = json.loads(text)
                    aliases = find_values(data, ["alias", "businessUrl", "bizId"])
                    for a in aliases:
                        if isinstance(a, str) and len(a) > 2 and "/" not in a:
                            biz_alias = a
                            break
                    if biz_alias:
                        break
                except Exception:
                    pass

        if not biz_alias:
            return None

        # Scrape the Yelp menu page
        menu_url = f"https://www.yelp.com/menu/{biz_alias}"
        menu_page = stealth.get(menu_url)

        categories = []
        current_cat = None

        # Parse menu sections using Scrapling CSS selectors
        for section in menu_page.css("section, [class*='menu-section'], [class*='MenuSection']"):
            heading = section.css_first("h2, h3, h4, [class*='heading'], [class*='title']")
            if heading and heading.text:
                cat_name = heading.text.strip()
                if len(cat_name) < 60:
                    current_cat = {"category": cat_name, "items": []}
                    categories.append(current_cat)

            for item in section.css("[class*='menu-item'], [class*='MenuItem'], li, [class*='dish']"):
                item_name = item.css_first("h3, h4, [class*='name'], [class*='title'], strong, b")
                item_price = item.css_first("[class*='price'], [class*='Price']")
                item_desc = item.css_first("p, [class*='desc'], [class*='description']")

                if item_name and item_name.text:
                    menu_item = {
                        "name": item_name.text.strip(),
                        "price": (item_price.text or "").strip() if item_price else "",
                        "description": (item_desc.text or "").strip() if item_desc else ""
                    }
                    if current_cat:
                        current_cat["items"].append(menu_item)
                    else:
                        current_cat = {"category": "Menu", "items": [menu_item]}
                        categories.append(current_cat)

        # Fallback: extract from JSON-LD or embedded JSON
        if not any(cat["items"] for cat in categories):
            categories = extract_menu_from_json(menu_page)

        # Fallback: parse any text with $ prices
        if not any(cat.get("items") for cat in categories):
            categories = parse_menu_from_text(menu_page)

        # Also try the main business page for popular items
        if not any(cat.get("items") for cat in categories):
            biz_url = f"https://www.yelp.com/biz/{biz_alias}"
            biz_page = stealth.get(biz_url)
            categories = extract_popular_from_biz(biz_page)

        return categories if any(cat.get("items") for cat in categories) else None

    except Exception as e:
        print(f"Yelp menu error: {e}")
        return None


def scrape_restaurant_website_menu(name, address, lat, lng):
    """Find and scrape the restaurant's own website menu"""
    try:
        # Search Google for the restaurant's website
        query = f"{name} {address} official website".replace(" ", "+")
        search_url = f"https://www.google.com/search?q={query}"
        page = stealth.get(search_url)

        # Find the restaurant's website link
        website_url = None
        for a in page.css("a[href*='http']"):
            href = a.attrib.get("href", "")
            # Skip aggregator sites
            skip = ["yelp.com", "google.com", "facebook.com", "instagram.com",
                    "tripadvisor.com", "doordash.com", "ubereats.com", "grubhub.com"]
            if any(s in href for s in skip):
                continue
            if href.startswith("http") and "." in href:
                website_url = href
                break

        if not website_url:
            return None

        # Fetch restaurant website
        site_page = fetcher.get(website_url)

        # Look for menu link on the homepage
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
                menu_page = fetcher.get(menu_url)
                break

        # Parse the menu page
        categories = []
        current_cat = None

        # Look for structured menu data
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

        # Fallback: parse text for price patterns
        if not any(cat.get("items") for cat in categories):
            categories = parse_menu_from_text(menu_page)

        return categories if any(cat.get("items") for cat in categories) else None

    except Exception as e:
        print(f"Website menu error: {e}")
        return None


def scrape_google_menu(name, address):
    """Scrape menu data from Google search results"""
    try:
        query = f"{name} {address} menu prices".replace(" ", "+")
        url = f"https://www.google.com/search?q={query}"
        page = stealth.get(url)

        items = []

        # Look for Google's menu cards
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

        # Fallback: look for any price-containing text blocks
        if not items:
            for el in page.css("span, div, li"):
                text = (el.text or "").strip()
                if "$" in text and len(text) > 4 and len(text) < 80:
                    name_part, price_part = split_name_price(text)
                    if name_part and len(name_part) > 2:
                        items.append({"name": name_part, "price": price_part, "description": ""})

        if items:
            return [{"category": "Menu", "items": items[:25]}]
        return []

    except Exception as e:
        print(f"Google menu error: {e}")
        return []


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def extract_menu_from_json(page):
    """Extract menu data from any embedded JSON on the page"""
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
    """Recursively find menu items in nested JSON"""
    if depth > 10:
        return []
    items = []
    if isinstance(data, dict):
        has_name = "name" in data or "title" in data or "itemName" in data
        has_price = "price" in data or "cost" in data or "amount" in data
        has_desc = "description" in data or "desc" in data

        if has_name and (has_price or has_desc):
            name = str(data.get("name") or data.get("title") or data.get("itemName", ""))
            price = data.get("price") or data.get("cost") or data.get("amount", "")
            if isinstance(price, dict):
                price = price.get("amount") or price.get("formatted") or price.get("display", "")
            desc = str(data.get("description") or data.get("desc", ""))

            if name and len(name) > 1 and len(name) < 80:
                items.append({"name": name, "price": str(price), "description": desc[:200]})

        if "hasMenuSection" in data:
            for section in (data["hasMenuSection"] if isinstance(data["hasMenuSection"], list) else [data["hasMenuSection"]]):
                sec_name = section.get("name", "Menu")
                sec_items = []
                for menu_item in (section.get("hasMenuItem", []) if isinstance(section.get("hasMenuItem"), list) else [section.get("hasMenuItem", {})]):
                    if isinstance(menu_item, dict) and menu_item.get("name"):
                        offer = menu_item.get("offers", {})
                        sec_items.append({
                            "name": menu_item["name"],
                            "price": str(offer.get("price", "")) if isinstance(offer, dict) else "",
                            "description": str(menu_item.get("description", ""))[:200]
                        })
                if sec_items:
                    return sec_items  # Return directly as items

        for v in data.values():
            items.extend(find_menu_items(v, depth + 1))
    elif isinstance(data, list):
        for item in data:
            items.extend(find_menu_items(item, depth + 1))
    return items


def find_values(data, keys, depth=0):
    """Find all values for given keys in nested data"""
    if depth > 8:
        return []
    results = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and isinstance(v, str):
                results.append(v)
            results.extend(find_values(v, keys, depth + 1))
    elif isinstance(data, list):
        for item in data:
            results.extend(find_values(item, keys, depth + 1))
    return results


def parse_menu_from_text(page):
    """Parse menu items from page text using price patterns"""
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
    """Extract popular/featured items from a Yelp business page"""
    items = []

    # Check for menu highlights or popular dishes
    for el in page.css("[class*='popular'], [class*='highlight'], [class*='dish'], [aria-label*='menu']"):
        name_el = el.css_first("p, span, h4, [class*='name']")
        price_el = el.css_first("[class*='price']")
        img_el = el.css_first("img")

        if name_el and name_el.text:
            text = name_el.text.strip()
            if len(text) > 2 and len(text) < 60:
                items.append({
                    "name": text,
                    "price": (price_el.text or "").strip() if price_el else "",
                    "description": ""
                })

    # Also check embedded JSON for menu/popular items
    for script in page.css("script[type='application/json']"):
        text = script.text or ""
        try:
            data = json.loads(text)
            json_items = find_menu_items(data)
            items.extend(json_items)
        except Exception:
            pass

    seen = set()
    unique = []
    for item in items:
        if item["name"] not in seen:
            seen.add(item["name"])
            unique.append(item)

    if unique:
        return [{"category": "Popular Items", "items": unique[:20]}]
    return []


def extract_price(text):
    """Extract price from text"""
    if not text:
        return ""
    match = re.search(r'\$[\d,.]+', text)
    return match.group() if match else ""


def split_name_price(text):
    """Split a line into name and price parts"""
    match = re.search(r'\$[\d,.]+', text)
    if match:
        price = match.group()
        name = text[:match.start()].strip().rstrip("-").rstrip(".").rstrip("…")
        return name or text, price
    return text, ""


def format_time(military):
    """Convert military time to 12-hour format"""
    if len(military) != 4:
        return military
    try:
        hour, minute = int(military[:2]), int(military[2:])
        period = "PM" if hour >= 12 else "AM"
        display = 12 if hour == 0 else (hour - 12 if hour > 12 else hour)
        return f"{display}:{minute:02d} {period}"
    except ValueError:
        return military


def merge_results(zabihah, yelp):
    """Merge and deduplicate results, enriching Zabihah with Yelp data"""
    merged = list(zabihah)
    for yr in yelp:
        y_name = yr.get("name", "").lower()[:8]
        y_lat = float(yr.get("latitude", 0))
        y_lng = float(yr.get("longitude", 0))
        is_dup = False

        for zr in merged:
            z_name = zr.get("name", "").lower()[:8]
            z_lat = float(zr.get("latitude", 0))
            z_lng = float(zr.get("longitude", 0))

            if (y_name in z_name or z_name in y_name) and \
               ((y_lat - z_lat)**2 + (y_lng - z_lng)**2)**0.5 < 0.002:
                # Enrich Zabihah data with Yelp data
                if not zr.get("rating") and yr.get("rating"):
                    zr["rating"] = yr["rating"]
                if not zr.get("reviewCount") and yr.get("reviewCount"):
                    zr["reviewCount"] = yr["reviewCount"]
                if not zr.get("coverImage") and yr.get("coverImage"):
                    zr["coverImage"] = yr["coverImage"]
                if not zr.get("galleryPhotos") and yr.get("galleryPhotos"):
                    zr["galleryPhotos"] = yr["galleryPhotos"]
                if not zr.get("businessHours") and yr.get("businessHours"):
                    zr["businessHours"] = yr["businessHours"]
                is_dup = True
                break

        if not is_dup:
            merged.append(yr)

    return merged


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
