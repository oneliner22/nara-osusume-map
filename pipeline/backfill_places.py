# -*- coding: utf-8 -*-
"""既存スポットの座標・メタ情報を Places API で一括裏取りする一回きりのスクリプト。
リポジトリルートで実行: python pipeline/backfill_places.py [--apply]
--apply なしはレポートのみ。判定は daily_job と同じ pro モデルで同一施設か確認する。

必要な環境変数: PLACES_API_KEY, GCP_PROJECT (+ ADC)
"""
import io
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("GITHUB_REPO", "-")   # daily_job import 用のダミー (未使用)
os.environ.setdefault("GITHUB_TOKEN", "-")
from daily_job import gen_json, places_search, official_url, load_url_blocklist, MODEL_JUDGE  # noqa: E402

APPLY = "--apply" in sys.argv

MATCH_SCHEMA = {"type": "object", "properties": {
    "match": {"type": "boolean"},
    "place_index": {"type": "integer"},
    "confidence": {"type": "string", "enum": ["high", "low"]}},
    "required": ["match", "confidence"]}


def dist_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


doc = json.load(io.open("data/spots.json", encoding="utf-8"))
pipeline_cfg = json.load(io.open("data/pipeline.json", encoding="utf-8"))
bbox = pipeline_cfg["bbox"]
load_url_blocklist(pipeline_cfg)
area_name = {a["id"]: a["name"] for a in doc["areas"]}

for s in doc["spots"]:
    if s.get("place_id"):
        print(f"{s['name']}: place_id あり skip")
        continue
    places = places_search(s["name"], area_name.get(s["area"], ""), bbox)
    if not places:
        print(f"{s['name']}: Places ヒットなし")
        continue
    desc = "\n".join(
        f"[{i}] {p.get('displayName', {}).get('text')} / {p.get('formattedAddress')} / "
        f"({p['location']['latitude']:.4f},{p['location']['longitude']:.4f})"
        for i, p in enumerate(places))
    j = gen_json(MODEL_JUDGE, MATCH_SCHEMA, [
        "地図データのスポットと Google Places の検索結果を照合し、同一施設があるか判定してください。"
        "支店違い・同名別施設は match=false または confidence=low とすること。\n"
        f"スポット: {s['name']}（{area_name.get(s['area'], '')} / {s['cat']} / 説明: {s['desc'][:60]}）\n"
        f"現在の座標: ({s['lat']}, {s['lng']})\nPlaces結果:\n{desc}"])
    if not (j.get("match") and j.get("confidence") == "high"):
        print(f"{s['name']}: 一致確信なし ({j.get('confidence')}) -> 手動確認")
        continue
    p = places[j.get("place_index", 0)]
    lat, lng = p["location"]["latitude"], p["location"]["longitude"]
    d = dist_m(s["lat"], s["lng"], lat, lng)
    status = p.get("businessStatus")
    flag = f" ⚠status={status}" if status not in (None, "OPERATIONAL") else ""
    print(f"{s['name']}: ずれ {d:,.0f}m (approx={s['approx']}){flag}"
          f" -> {p['displayName']['text']}")
    if APPLY:
        s["lat"], s["lng"] = round(lat, 5), round(lng, 5)
        s["place_id"] = p["id"]
        s["approx"] = False
        if p.get("formattedAddress"):
            s["address"] = p["formattedAddress"]
        if official_url(p) and not s.get("url"):
            s["url"] = official_url(p)
        hours = p.get("regularOpeningHours", {}).get("weekdayDescriptions")
        if hours and not s.get("hours"):
            s["hours"] = list(hours)   # 週7日ぶん (先頭2日だけだと月火の店に見える)

if APPLY:
    json.dump(doc, io.open("data/spots.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("applied -> data/spots.json")
