# -*- coding: utf-8 -*-
"""data/*.json の整合性チェック。crj の入稿後・手動編集後に実行する。
使い方: python validate.py  (異常があれば exit 1)
"""
import json, io, re, sys

ERRORS = []
def err(msg): ERRORS.append(msg)

def load(path):
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception as e:
        err(f"{path}: パース失敗 ({e})")
        return None

spots_doc = load("data/spots.json")
videos    = load("data/videos.json")
config    = load("data/config.json")
pipeline  = load("data/pipeline.json")
ledger    = load("data/ledger.json")
aliases   = load("data/aliases.json")
pending   = load("data/pending.json")

if spots_doc and videos and pipeline:
    areas = spots_doc.get("areas", [])
    spots = spots_doc.get("spots", [])
    area_ids = [a["id"] for a in areas]
    if len(area_ids) != len(set(area_ids)):
        err("area id が重複")
    for a in areas:
        if re.search(r"[<>&\"'`]", str(a.get("name", ""))):
            err(f"area '{a.get('id')}': name に HTML 特殊文字 '{a.get('name')}'")

    bbox = pipeline["bbox"]
    slugs = set()
    x_url_re = re.compile(r"^https://(x|twitter)\.com/.+/status(es)?/\d+")

    for s in spots:
        tag = f"spot '{s.get('slug', '?')}'"
        for key in ("slug", "name", "area", "cat", "lat", "lng", "approx", "desc", "sources"):
            if key not in s:
                err(f"{tag}: 必須キー {key} がない")
        if s.get("slug") in slugs:
            err(f"{tag}: slug 重複")
        slugs.add(s.get("slug"))
        if s.get("area") not in area_ids:
            err(f"{tag}: 未知の area '{s.get('area')}'")
        lat, lng = s.get("lat"), s.get("lng")
        if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
            err(f"{tag}: lat/lng が数値でない")
        elif not (bbox["lat_min"] <= lat <= bbox["lat_max"] and bbox["lng_min"] <= lng <= bbox["lng_max"]):
            err(f"{tag}: 座標 ({lat},{lng}) がマージン付きbbox外")
        if s.get("url") is not None and not re.match(r"^https?://\S+$", str(s["url"])):
            err(f"{tag}: url が http(s) でない '{s['url']}'")
        if not isinstance(s.get("sources"), list) or not s["sources"]:
            err(f"{tag}: sources が空")
            continue
        for src in s["sources"]:
            t = src.get("type")
            if t == "youtube":
                if src.get("id") not in videos:
                    err(f"{tag}: videos.json にない動画ID '{src.get('id')}'")
            elif t == "x":
                if not x_url_re.match(src.get("url", "")):
                    err(f"{tag}: 不正なX URL '{src.get('url')}'")
                # /i/web/status/<id> は X のリダイレクト頼みで、年齢制限付きアカウントの
                # ポストだと未ログイン閲覧者に404が出る。著者不明で正規化できない場合の
                # フォールバックなので入稿は止めず、警告だけ出す
                elif "/i/web/status/" in src["url"]:
                    print(f"note: {tag}: X URL が /i/web/ 形式 (著者ハンドル不明) "
                          f"'{src['url']}'")
                if not src.get("date"):
                    err(f"{tag}: x source に date がない")
            else:
                err(f"{tag}: 未知の source type '{t}'")

if ledger is not None and ledger is not False:
    for key in ("processed_posts", "authors"):
        if not isinstance(ledger.get(key), dict):
            err(f"ledger.json: {key} が dict でない")

if pending is not None and not isinstance(pending.get("items"), list):
    err("pending.json: items が list でない")

if aliases is not None and spots_doc:
    names = {s["name"] for s in spots_doc.get("spots", [])}
    for k, v in aliases.items():
        if v not in names:
            print(f"note: alias '{k}' -> '{v}' は既存スポット名に一致しない (新規スポット用なら問題なし)")

if ERRORS:
    print("NG:", len(ERRORS), "件")
    for e in ERRORS:
        print(" -", e)
    sys.exit(1)

n_spots = len(spots_doc["spots"]) if spots_doc else 0
n_src = sum(len(s["sources"]) for s in spots_doc["spots"]) if spots_doc else 0
print(f"OK: spots={n_spots} sources={n_src} areas={len(spots_doc['areas'])} "
      f"videos={len(videos)} pending={len(pending['items'])}")
