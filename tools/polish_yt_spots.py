# -*- coding: utf-8 -*-
"""初期データ(YouTube由来)の仕上げ。一回きり。
  1) slug を日本語名のローマ字 (pykakasi) にする (build_yt_data の spot-NN フォールバック置換)
  2) 紹介文を石川版と同じ常体 (だ・である / 体言止め、です・ます禁止) に揃える (Gemini flash-lite)
使い方: リポ直下で python tools/polish_yt_spots.py
"""
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import pykakasi
from google import genai

client = genai.Client(vertexai=True,
                      project=os.environ.get("GCP_PROJECT", "central-bulwark-427114-j7"),
                      location="global")
# モデルIDの正本はリポの pipeline/models.json (無ければ既定値)
_mp = os.path.join("pipeline", "models.json")
MODEL = (json.load(io.open(_mp, encoding="utf-8"))["extract"] if os.path.exists(_mp)
         else "gemini-3.5-flash-lite")
kks = pykakasi.kakasi()

doc = json.load(io.open("data/spots.json", encoding="utf-8"))
spots = doc["spots"]

# ---- 1) slug ----
used = set()
for s in spots:
    if s["cat"] == "会場":
        used.add(s["slug"])
for s in spots:
    if s["cat"] == "会場":
        continue
    base = "-".join(x["hepburn"] for x in kks.convert(s["name"]))
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:48].rstrip("-") or "spot"
    slug, n = base, 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    s["slug"] = slug

# ---- 2) desc を常体に ----
SCHEMA = {"type": "object", "properties": {
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "slug": {"type": "string"}, "desc": {"type": "string"}},
        "required": ["slug", "desc"]}}}, "required": ["items"]}


def rewrite(batch):
    listing = "\n".join(f"- slug={s['slug']} / 名称={s['name']} / 現在の紹介文={s['desc']}"
                        for s in batch)
    res = client.models.generate_content(
        model=MODEL,
        contents=[
            "以下は旅行Vlogから抽出したスポットの紹介文です。各紹介文を、内容・事実を一切変えずに"
            "文体だけ次のルールで書き直してください:\n"
            "- 常体 (だ・である調 / 体言止め中心)。です・ます調は禁止\n"
            "- 「〜しました」「〜を注文し」のようなVloggerの行動描写は、店や場所の特徴として言い換える"
            " (例: 「鍋焼きうどんを注文しました」→「名物は鍋焼きうどん」)。動画投稿者の名前は出さない\n"
            "- 「複数の動画で紹介されています」は「複数の動画で紹介」のように簡潔に\n"
            "- 60〜110字、絵文字なし、事実の捏造・追加なし\n"
            "slug は変えずにそのまま返すこと。\n\n" + listing],
        config={"response_mime_type": "application/json", "response_schema": SCHEMA,
                "temperature": 0.2})
    return json.loads(res.text)["items"]


targets = [s for s in spots if s["cat"] != "会場"]
batches = [targets[i:i + 15] for i in range(0, len(targets), 15)]
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(rewrite, batches))
by_slug = {s["slug"]: s for s in spots}
n = 0
for items in results:
    for it in items:
        s = by_slug.get(it["slug"])
        if s and it["desc"].strip():
            s["desc"] = it["desc"].strip()
            n += 1
print(f"desc rewritten: {n}/{len(targets)}")
still = [s["name"] for s in targets if re.search(r"(です|ます|ました)[。、]?$", s["desc"])]
print("still polite:", still[:20])

json.dump(doc, io.open("data/spots.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved")
