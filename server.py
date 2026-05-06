from flask import Flask, request, jsonify
from scrapling import Fetcher
import json
import re

app = Flask(__name__)
fetcher = Fetcher(auto_match=True)

@app.route("/api/zabihah", methods=["POST"])
def scrape_zabihah():
    lat = request.json.get("latitude")
    lng = request.json.get("longitude")
    if not lat or not lng:
        return jsonify({"error": "latitude and longitude required"}), 400

    url = f"https://www.zabihah.com/search?lat={lat}&lng={lng}"
    page = fetcher.get(url)

    restaurants = []
    for item in page.css("script"):
        text = item.text or ""
        if "initialRestaurants" in text:
            match = re.search(r'"initialRestaurants":(\[.*?\])', text, re.DOTALL)
            if match:
                restaurants = json.loads(match.group(1))
            break

    return jsonify({"restaurants": restaurants})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
