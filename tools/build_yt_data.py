# -*- coding: utf-8 -*-
"""YouTube旅Vlog の Gemini 解析結果から初期 spots.json / videos.json を組み立てる一回きりのスクリプト。

使い方 (作業ディレクトリ = 解析結果の置き場):
  1) python build_yt_data.py collect <analysis_dir> <videos_selected.json>
       → raw_spots.json (座標からエリアを決めて city に入れる。merge.py はこの city 単位で名寄せする)
  2) python ~/.claude/skills/region-spot-map/assets/merge.py raw_spots.json merged/
  3) python build_yt_data.py build merged/ <videos_selected.json> <repo>/data
       → spots.json / videos.json (会場マーカー入り)
"""
import glob
import io
import json
import math
import os
import re
import sys
import unicodedata

AREAS = [
    {"id": "park",      "name": "奈良公園・東大寺・春日大社",   "color": "#c2543f"},
    {"id": "naramachi", "name": "ならまち・猿沢池",             "color": "#e0489a"},
    {"id": "city",      "name": "奈良駅周辺・きたまち",         "color": "#e8913f"},
    {"id": "west",      "name": "西ノ京・斑鳩・生駒",           "color": "#3f7fd6"},
    {"id": "east",      "name": "天理・桜井・宇陀・柳生",       "color": "#2f9e7d"},
    {"id": "asuka",     "name": "明日香・橿原・葛城",           "color": "#b07d2a"},
    {"id": "yoshino",   "name": "吉野・奈良南部",               "color": "#7b5cd6"},
    {"id": "other",     "name": "県外（大阪・京都・三重寄り）",  "color": "#8a7350"},
]
CATS = {"グルメ", "カフェ・喫茶", "観光", "レトロ", "雑貨・土産", "自然", "動物", "体験", "宿", "温泉"}
# 全国チェーン (動画内で立ち寄っただけで「おすすめ」ではない) は載せない
DROP_PREFIX = ("すき家", "松屋 ", "プロント", "スターバックス", "マクドナルド", "セブン", "ローソン", "ファミリーマート")
PREF = (33.85, 34.79, 135.53, 136.24)      # 奈良県本体のおよその範囲
BBOX = (33.75, 34.88, 135.5, 136.35)       # マージン付き (pipeline.json と同じ)
# ぽんぽこちゃんねるの奈良回 (聖地) → spot.mark / videos[].mark
MARK_VIDEOS = {"PLKr7oD-Td8", "TDY4tE3Vsdg", "pVc8aRoLKGA", "F2sNswWhgCo"}

VENUE = {
    "slug": "nara-100nen-kaikan", "name": "なら100年会館（回覧板 in 奈良 会場）",
    "area": "city", "cat": "会場", "lat": 34.68057, "lng": 135.81683, "approx": False,
    "desc": "「ぽこぴーの回覧板」奈良公演の会場（2026年12月27日・大ホール、18:00開演）。JR奈良駅西口のすぐ隣にあり、近鉄奈良駅からも徒歩圏。",
    "address": "奈良県奈良市三条宮前町７−１", "url": "http://www.nara100.com/",
    "place_id": "ChIJi0CpvTE6AWARLNktIchDaT0",
    "sources": [{"type": "x", "url": "https://x.com/ponpokoka/status/2031704199483019571",
                 "author": "ponpokoka", "date": "2026-03-11",
                 "quote": "回覧板2026 追加開催決定‼️ 今年はこの5か所だっ！！！ 4月福島！！ 6月大分！！ 8月石川！！ 10月愛媛！！ 12月奈良！！"}],
}


def area_of(lat, lng, name=""):
    if not (PREF[0] <= lat <= PREF[1] and PREF[2] <= lng <= PREF[3]):
        return "other"
    if lat < 34.42:                       # 吉野・五條・十津川・天川
        return "yoshino"
    if lat < 34.56:                       # 橿原・明日香・葛城 / 桜井・宇陀・室生
        return "asuka" if lng < 135.835 else "east"
    if lat < 34.64:                       # 斑鳩・郡山南 / 天理
        return "west" if lng < 135.81 else "east"
    if lng < 135.79:                      # 西ノ京・生駒・学園前
        return "west"
    if lng >= 135.87:                     # 柳生・月ヶ瀬・奈良市東部山間
        return "east"
    if lat < 34.6815 and 135.822 <= lng <= 135.845:   # 三条通以南 (ならまち・餅飯殿・猿沢池)
        return "naramachi"
    if lat >= 34.6855 and lng < 135.836:              # きたまち (奈良監獄・奈良女子大周辺)
        return "city"
    if lng >= 135.829:
        return "park"
    return "city"


def collect(analysis_dir, videos_json):
    vids = [v["id"] for v in json.load(io.open(videos_json, encoding="utf-8"))]
    raw = []
    for vid in vids:
        p = os.path.join(analysis_dir, vid + ".json")
        if not os.path.exists(p):
            print("MISSING analysis:", vid)
            continue
        d = json.load(io.open(p, encoding="utf-8"))
        for s in d["spots"]:
            s = dict(s)
            s["video"] = vid
            s["city_raw"] = s.get("city", "")
            s["city"] = area_of(s["lat"], s["lng"], s["name_ja"])
            raw.append(s)
    json.dump(raw, io.open("raw_spots.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from collections import Counter
    print("raw spots:", len(raw), Counter(s["city"] for s in raw))


def slugify(name_local, name_ja, used):
    cands = [name_local or "", name_ja]
    s = ""
    for c in cands:
        t = unicodedata.normalize("NFKD", c)
        t = "".join(ch for ch in t if not unicodedata.combining(ch))
        t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
        if len(t) >= 3:
            s = t
            break
    s = (s or "spot")[:40].rstrip("-")
    base, n = s, 2
    while s in used:
        s = f"{base}-{n}"
        n += 1
    used.add(s)
    return s


def build(merged_dir, videos_json, out_dir):
    raw = json.load(io.open("raw_spots.json", encoding="utf-8"))
    for i, s in enumerate(raw):
        s["idx"] = i
    spots, used = [VENUE], {VENUE["slug"]}
    for area in AREAS:
        p = os.path.join(merged_dir, f"merged_{area['id']}.json")
        if not os.path.exists(p):
            continue
        merged = json.load(io.open(p, encoding="utf-8"))
        for m in merged["spots"]:
            if m["name_ja"].startswith(DROP_PREFIX):
                print("DROP chain:", m["name_ja"])
                continue
            vids, seen = [], set()
            for idx in m["members"]:
                v = raw[idx]["video"]
                if v not in seen:
                    seen.add(v)
                    vids.append(v)
            lat, lng = m["lat"], m["lng"]
            aid = area_of(lat, lng, m["name_ja"])   # 統合後の座標で再判定
            if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lng <= BBOX[3]):
                print("BBOX OUT (skip):", m["name_ja"], lat, lng)
                continue
            spot = {
                "slug": slugify(m.get("name_local"), m["name_ja"], used),
                "name": m["name_ja"], "area": aid,
                "cat": m["category"] if m["category"] in CATS else "観光",
                "lat": round(lat, 5), "lng": round(lng, 5), "approx": bool(m["approx"]),
                "desc": m["desc"],
                "sources": [{"type": "youtube", "id": v} for v in vids],
            }
            if aid == "other":
                spot["out_of_pref"] = True
            if MARK_VIDEOS & set(vids):
                spot["mark"] = True
            spots.append(spot)
    used_areas = {s["area"] for s in spots}
    areas = [a for a in AREAS if a["id"] in used_areas]
    videos = {v["id"]: dict({"title": v["title"], "channel": v["channel"]},
                            **({"mark": True} if v["id"] in MARK_VIDEOS else {}))
              for v in json.load(io.open(videos_json, encoding="utf-8"))}
    os.makedirs(out_dir, exist_ok=True)
    json.dump({"areas": areas, "spots": spots},
              io.open(os.path.join(out_dir, "spots.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(videos, io.open(os.path.join(out_dir, "videos.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    from collections import Counter
    print("spots:", len(spots), Counter(s["area"] for s in spots))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "collect":
        collect(sys.argv[2], sys.argv[3])
    elif cmd == "build":
        build(sys.argv[2], sys.argv[3], sys.argv[4])
