from flask import Flask, request, jsonify
from scrapling import Fetcher
import json
import re

app = Flask(__name__)
fetcher = Fetcher(auto_match=True)


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


def scrape_zabihah(lat, lng):
    try:
        url = f"https://www.zabihah.com/search?lat={lat}&lng={lng}"
        page = fetcher.get(url)
        for script in page.css("script"):
            text = script.text or ""
            if "initialRestaurants" in text:
                match = re.search(r'"initialRestaurants":(\[.*?\])', text, re.DOTALL)
                if match:
                    return json.loads(match.group(1))
        return []
    except Exception:
        return []


def scrape_yelp(lat, lng):
    try:
        results = []
        seen = set()
        for query in ["halal+restaurant", "halal+food", "mediterranean+restaurant"]:
            url = f"https://www.yelp.com/search?find_desc={query}&latitude={lat}&longitude={lng}"
            page = fetcher.get(url)

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
        return results
    except Exception:
        return []


def find_businesses(data, depth=0):
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
        "businessHours": [],
        "halalSummary": {
            "description": "Found on Yelp - verify halal status",
            "meatHalalStatus": None
        }
    }


def merge_results(zabihah, yelp):
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
                if not zr.get("rating") and yr.get("rating"):
                    zr["rating"] = yr["rating"]
                if not zr.get("reviewCount") and yr.get("reviewCount"):
                    zr["reviewCount"] = yr["reviewCount"]
                if not zr.get("coverImage") and yr.get("coverImage"):
                    zr["coverImage"] = yr["coverImage"]
                is_dup = True
                break

        if not is_dup:
            merged.append(yr)

    return merged


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
