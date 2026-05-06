from flask import Flask, request, jsonify
import requests
import json
import re

app = Flask(__name__)

@app.route("/api/zabihah", methods=["POST"])
def scrape_zabihah():
    lat = request.json.get("latitude")
    lng = request.json.get("longitude")
    if not lat or not lng:
        return jsonify({"error": "latitude and longitude required"}), 400

    url = f"https://www.zabihah.com/search?lat={lat}&lng={lng}"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
    }

    resp = requests.get(url, headers=headers)
    restaurants = []
    match = re.search(r'"initialRestaurants":(\[.*?\])', resp.text, re.DOTALL)
    if match:
        try:
            restaurants = json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return jsonify({"restaurants": restaurants})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
