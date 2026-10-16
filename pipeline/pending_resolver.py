# -*- coding: utf-8 -*-
"""日次 pending 整理ジョブ (Cloud Run Jobs)。日次ジョブの完了後に走る。

data/pending.json の保留候補(受入ゲート不合格分)を、Gemini のツールループ
(Places再検索)で1件ずつ精査し、
  - 掲載可能   → spots.json へ登録 / 既存スポットへ出典追記
  - 表記揺れ   → aliases.json へ登録して再発防止
  - 閉業・県外 → pending から削除
  - 判断不能   → needs_human を付けて pending に残す
  reason が author_uncertain (著者の素性が判定できなかった投稿) の item は、人が
  ledger.json の authors[].verdict を "fan" に書き換えるまで処理せず pending に置いたままにする
  (掲載しないなら item を pending.json から消す)。著者の信頼性は Gemini の再調査では決められない。
を行い commit/push する。日次ジョブがワンショット判定で拾えない
「クエリ再構成が必要な表記揺れ」「既存スポットとの名寄せ」を回収するのが役割。

daily_job と同じく、掲載可否の最終判定は必ずコード側ゲート(apply_decision)で
強制する。Gemini の decision は place_id が実際の Places 検索結果に含まれる
場合のみ採用され、営業状況・bbox もコードで再検証する。

必要な環境変数は daily_job.py と同じ。追加:
  MAX_ITEMS        1回の実行で処理する件数上限 (既定 12)
  PENDING_OVERRIDE テスト用: pending.json の代わりに処理する items のJSON文字列
"""
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from google.genai import types

import daily_job as dj
from mcp_client import McpClient

MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "12"))
MAX_TOOL_STEPS = 10
BBOX = {"lat_min": 32.6, "lat_max": 34.6, "lng_min": 131.7, "lng_max": 134.1}

log = dj.log


# ---------------- 元ポスト取得 ----------------

def get_post(mcp, post_id):
    """pending item の出典ポストを xdev から引く。区画をまたいで media_urls を補完。"""
    rows = mcp.call("run_sql", {"max_rows": 5, "query":
        "SELECT t.text, t.author_name, COALESCE(t.media_urls, mm.mu) media_urls "
        "FROM tweets t LEFT JOIN (SELECT id, arg_max(media_urls, ingested_at) mu "
        "FROM tweets WHERE media_urls IS NOT NULL GROUP BY id) mm ON mm.id = t.id "
        f"WHERE t.id = '{post_id}' LIMIT 1"})
    posts = rows.get("rows", rows) if isinstance(rows, dict) else rows
    return posts[0] if posts else None


# ---------------- 調査ツール ----------------

PLACES_DECL = {
    "name": "places_search",
    "description": "Google Places でスポットを検索する。正式名称の推測を変えながら"
                   "複数回呼んでよい。query には「スポット名 + 地名やジャンル」を渡す"
                   "(例: '中谷堂 奈良', 'ほうせき箱 ならまち')。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "検索クエリ"}},
        "required": ["query"]},
}
# fetch_url ツールは廃止 (2026-08-08)。モデル駆動で任意URLを取得する構造は
# DNS rebinding を含む SSRF の攻撃面になるため、裏取りは Places 検索のみで行う。


def run_tool(name, args, collected):
    """ツール実行。places_search の結果は collected に蓄積し、後段ゲートの根拠にする。"""
    if name == "places_search":
        places = dj.places_search(str(args.get("query", "")), None, BBOX)
        out = []
        for p in places:
            collected[p["id"]] = p
            out.append({"place_id": p["id"],
                        "name": p.get("displayName", {}).get("text"),
                        "address": p.get("formattedAddress"),
                        "status": p.get("businessStatus"),
                        "lat": p["location"]["latitude"],
                        "lng": p["location"]["longitude"],
                        "website": p.get("websiteUri", "")})
        return out or "ヒットなし"
    return f"不明なツール: {name}"


# ---------------- 調査ループ + 最終判断 ----------------

DECISION_SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["add", "append_source", "drop", "hold"]},
    "place_id": {"type": "string"},
    "existing_name": {"type": "string"},
    "slug": {"type": "string"},
    "cat": {"type": "string", "enum": dj.CATS},
    "area_id": {"type": "string"},
    "desc": {"type": "string"},
    "quote": {"type": "string"},
    "aliases": {"type": "array", "items": {"type": "string"}},
    "reason": {"type": "string"}}, "required": ["action", "reason"]}


def shortlist_spots(name, doc, aliases, limit=40):
    """候補名に類似する掲載済みスポットのみ返す。全件をプロンプトに同梱すると
    トークンが spots 数×MAX_ITEMS に比例して増えるため (名寄せの最終判定は
    apply_decision 側が全件走査するので、ここは名寄せのヒント提示でよい)。"""
    rev = {}
    for k, v in aliases.items():
        rev.setdefault(v, []).append(k)
    key = dj.normalize(name)

    def score(s):
        names = [s["name"]] + rev.get(s["name"], [])
        return max(difflib.SequenceMatcher(None, key, dj.normalize(n)).ratio()
                   for n in names)
    return sorted(doc["spots"], key=score, reverse=True)[:limit]


def investigate(item, post, images, doc, aliases, style_examples, collected):
    areas_desc = "\n".join(f"- {a['id']}: {a['name']}" for a in doc["areas"])
    spots_digest = "\n".join(f"- {s['name']} (slug={s['slug']})"
                             for s in shortlist_spots(item["name"], doc, aliases))
    prompt = (
        "あなたは「ぽこピーの回覧板 in 奈良」おすすめスポットマップの編集者です。"
        "自動入稿の受入ゲートで保留になった候補1件を精査し、掲載可否を判断します。\n\n"
        f"# 保留候補\n{json.dumps(item, ensure_ascii=False)}\n\n"
        f"# 出典ポスト\n著者: {(post or {}).get('author_name', '(取得不可)')}\n"
        f"本文: {(post or {}).get('text', '(取得不可)')}\n"
        "(添付画像があれば後続に含む。おすすめ記述は画像内にあることが多い)\n\n"
        f"# 掲載済みスポット (候補名との類似上位のみ抜粋)\n{spots_digest}\n\n"
        f"# 既存エイリアス(略称→正式名)\n{json.dumps(aliases, ensure_ascii=False)}\n\n"
        f"# エリア一覧\n{areas_desc}\n\n"
        "# 調査方針\n"
        "- places_search で実在・営業状況・所在地を裏取りする。候補名が略称・表記揺れの"
        "可能性を考え、正式名称を推測してクエリを変えながら検索し直してよい"
        "(例: 中谷堂→中谷堂 (高速餅つきのよもぎ餅))。\n"
        "- チェーン店・多店舗の場合は本店または出典ポストの文脈に合う店舗を1つ選ぶ。\n"
        "- 掲載済みスポットと同一施設(別表記・別店舗の同一施設を含む)なら新規追加ではなく"
        "出典追記と判断する。\n"
        "- 実在確認が取れない・判断に迷う場合は無理に掲載しない(捏造事故の前歴があるため"
        "裏取り最優先)。\n\n"
        "調査が終わったら、判断根拠の要約を簡潔に述べてください。")
    parts = [types.Part(text=prompt)]
    for img in images:
        parts.append(types.Part.from_bytes(data=img[0], mime_type=img[1]))
    contents = [types.Content(role="user", parts=parts)]
    tools = [types.Tool(function_declarations=[PLACES_DECL])]

    for _ in range(MAX_TOOL_STEPS):
        res = dj.client.models.generate_content(
            model=dj.MODEL_JUDGE, contents=contents,
            config=types.GenerateContentConfig(tools=tools, temperature=0))
        cand = res.candidates[0]
        contents.append(cand.content)
        calls = [p.function_call for p in (cand.content.parts or [])
                 if getattr(p, "function_call", None)]
        if not calls:
            break
        resp_parts = []
        for fc in calls:
            out = run_tool(fc.name, dict(fc.args or {}), collected)
            log(f"  [{item['name']}] tool {fc.name}({dict(fc.args or {})}) -> "
                f"{str(out)[:120]}")
            resp_parts.append(types.Part.from_function_response(
                name=fc.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=resp_parts))

    decision_prompt = (
        "調査結果を踏まえ、最終判断をJSONで出力してください。\n"
        "- action: add=新規掲載 / append_source=既存スポットへ出典追記 / "
        "drop=掲載せず保留解除(閉業・県外・ノイズ・重複処理済み) / hold=人間の判断に回す\n"
        "- add の場合: place_id は今回の places_search 結果にある place_id をそのまま指定"
        "(結果にないIDは無効)。slug は小文字英数ハイフンの短い識別子。cat・area_id を選び、"
        "desc は以下の文体(常体・体言止め中心、です・ます調禁止、50〜90字、絵文字なし、"
        "事実の捏造なし)でファンの言及を織り込む:\n"
        + "\n".join(f"  例: {e}" for e in style_examples) + "\n"
        "- append_source の場合: existing_name に掲載済みスポットの name をそのまま指定。\n"
        "- quote: 出典ポスト(本文または画像)に実際にある記述の抜粋(30字程度)。"
        "実際の記述がなければ空文字。創作・脚色は厳禁。\n"
        "- aliases: 今回の候補名や判明した略称・表記揺れのうち、正式スポット名への"
        "対応として登録すべきもの(なければ空配列)。\n"
        "- reason: 判断理由を一言で。")
    contents.append(types.Content(role="user", parts=[types.Part(text=decision_prompt)]))
    res = dj.client.models.generate_content(
        model=dj.MODEL_JUDGE, contents=contents,
        config={"response_mime_type": "application/json",
                "response_schema": DECISION_SCHEMA, "temperature": 0})
    return json.loads(res.text)


# ---------------- 適用 (コード側ゲート) ----------------

def register_aliases(decision, aliases, spot_name, item_name, stats):
    cands = set(decision.get("aliases") or []) | {item_name.strip()}
    for k in cands:
        k = k.strip()
        if k and k != spot_name and k not in aliases:
            aliases[k] = spot_name
            stats["aliases"] += 1


def handle_of(url):
    """URL から著者ハンドルを取り出す (/i/web/status/<id> 形式なら空文字)。"""
    m = re.match(r"https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/status", url or "")
    return m.group(1) if m else ""


def build_source(item, post, decision):
    """pending item から X 出典を組み立てる。pending.json に入っている URL は
    /i/web/ 形式のことがあるため、著者が分かればここで正規形に直す。

    著者は原則 xdev の元ポストから取るが、xdev の tweets ストアはローリング保持で
    古い行が消えるため、pending が数日残ると引けなくなる。その場合は入稿時に
    pending.json へ書かれた URL のハンドルを使う (これも無ければ /i/web/ 形式)。"""
    author = (post or {}).get("author_name", "") or handle_of(item["post"])
    url = dj.x_post_url(dj.post_id_of(item["post"]), author) \
        if dj.post_id_of(item["post"]) else item["post"]
    src = {"type": "x", "url": url, "author": author,
           "date": item.get("date", dj.TODAY),
           "quote": decision.get("quote", "")}
    dj.set_embed_flag(src)
    return src


def append_source(spot, item, post, decision, aliases, stats):
    if not dj.has_source(spot["sources"], item["post"]):
        spot["sources"].append(build_source(item, post, decision))
        stats["appended"].append(spot["name"])
    register_aliases(decision, aliases, spot["name"], item["name"], stats)


def hold(item, pending_items, note, stats):
    reason = item.get("reason", "")
    if "needs_human" not in reason:
        item["reason"] = f"{reason} / needs_human: {note}"
    pending_items.append(item)
    stats["held"] += 1
    log(f"  hold: {note}")


def apply_decision(decision, item, post, doc, aliases, pending_items, collected, stats):
    action = decision.get("action")
    spots = doc["spots"]
    log(f"  decision: {action} ({decision.get('reason', '')})")

    if action == "hold":
        hold(item, pending_items, decision.get("reason", "判断保留"), stats)
        return
    if action == "drop":
        stats["dropped"].append(f"{item['name']}: {decision.get('reason', '')}")
        return
    if action == "append_source":
        target = dj.match_existing(decision.get("existing_name") or item["name"],
                                   aliases, spots)
        if not target:
            hold(item, pending_items, "出典追記先が見つからない", stats)
            return
        append_source(target, item, post, decision, aliases, stats)
        return
    if action != "add":
        hold(item, pending_items, f"不明なaction: {action}", stats)
        return

    # --- add: 掲載可否をコードで強制 ---
    place = collected.get(decision.get("place_id", ""))
    if not place:
        hold(item, pending_items, "place_idが検索結果に基づかない", stats)
        return
    if place.get("businessStatus") != "OPERATIONAL":
        hold(item, pending_items, f"business_status={place.get('businessStatus')}", stats)
        return
    lat = place["location"]["latitude"]
    lng = place["location"]["longitude"]
    if not (BBOX["lat_min"] <= lat <= BBOX["lat_max"]
            and BBOX["lng_min"] <= lng <= BBOX["lng_max"]):
        stats["dropped"].append(f"{item['name']}: bbox外")
        return
    existing = next((s for s in spots if s.get("place_id") == place["id"]), None) \
        or dj.match_existing(place["displayName"]["text"], aliases, spots)
    if existing:  # 名寄せ漏れ: 追加ではなく出典追記に倒す
        append_source(existing, item, post, decision, aliases, stats)
        return
    area_ids = {a["id"] for a in doc["areas"]}
    if decision.get("area_id") not in area_ids:
        hold(item, pending_items, f"area_id不正: {decision.get('area_id')}", stats)
        return
    desc = (decision.get("desc") or "").strip()
    if not 15 <= len(desc) <= 160:
        hold(item, pending_items, "desc不備", stats)
        return
    slugs = {s["slug"] for s in spots}
    slug = re.sub(r"[^a-z0-9-]", "", (decision.get("slug") or "spot").lower()) or "spot"
    while slug in slugs:
        slug += "2"
    spot = {
        "slug": slug, "name": place["displayName"]["text"],
        "area": decision["area_id"], "cat": decision.get("cat", "観光"),
        "lat": round(lat, 5), "lng": round(lng, 5), "approx": False,
        "desc": desc, "address": place.get("formattedAddress", ""),
        "place_id": place["id"], "added": dj.TODAY,
        "sources": [build_source(item, post, decision)]}
    hours = place.get("regularOpeningHours", {}).get("weekdayDescriptions")
    if hours:
        spot["hours"] = list(hours)   # 週7日ぶん (先頭2日だけだと月火の店に見える)
    if dj.official_url(place):
        spot["url"] = dj.official_url(place)
    if not (dj.PREF_STRICT["lat_min"] <= lat <= dj.PREF_STRICT["lat_max"]
            and dj.PREF_STRICT["lng_min"] <= lng <= dj.PREF_STRICT["lng_max"]):
        spot["out_of_pref"] = True
    spots.append(spot)
    stats["added"].append(spot["name"])
    register_aliases(decision, aliases, spot["name"], item["name"], stats)


# ---------------- メイン ----------------

def main():
    workdir = tempfile.mkdtemp(prefix="spots-pending-")
    dj.clone_repo(workdir)
    doc = dj.load(workdir, "spots.json")
    aliases = dj.load(workdir, "aliases.json")
    pending = dj.load(workdir, "pending.json")
    ledger = dj.load(workdir, "ledger.json")
    pipeline_cfg = dj.load(workdir, "pipeline.json")
    dj.load_url_blocklist(pipeline_cfg)
    until = pipeline_cfg.get("until")
    if until and dj.TODAY > until and os.environ.get("FORCE_RUN") != "1":
        log(f"更新期間終了 (until {until}): 何もしない")
        return

    items = pending["items"]
    override = os.environ.get("PENDING_OVERRIDE")
    if override:
        items = json.loads(override)
        log(f"PENDING_OVERRIDE: {len(items)} items")
    if not items:
        log("pending empty, nothing to do")
        return

    # 著者未判定の item は人の判断待ち。ledger で fan に変わっていれば回収対象に入れる
    def author_cleared(item):
        if not str(item.get("reason", "")).startswith("author_uncertain"):
            return True
        name = item.get("author") or handle_of(item.get("post", ""))
        verdict = next((a.get("verdict") for a in ledger.get("authors", {}).values()
                        if name and a.get("name") == name), None)
        return verdict == "fan"
    waiting = [it for it in items if not author_cleared(it)]
    items = [it for it in items if author_cleared(it)]
    if waiting:
        log(f"author_uncertain のため人の判断待ち: {len(waiting)} 件 "
            f"({', '.join(it['name'] for it in waiting[:5])})")
    if not items:
        log("処理対象なし")
        return

    todo, rest = items[:MAX_ITEMS], items[MAX_ITEMS:]
    if rest:
        log(f"cap: {len(todo)}/{len(items)} 件のみ処理 (残りは次回)")

    mcp = None
    try:
        mcp = McpClient(os.environ["XDEV_MCP_URL"])
    except Exception as e:
        log("mcp unavailable (ポスト本文なしで続行):", repr(e))

    style_examples = [s["desc"] for s in doc["spots"][:3]]
    stats = {"added": [], "appended": [], "dropped": [], "held": 0, "aliases": 0}
    new_items = []

    # 調査 (ポスト取得→画像DL→Gemini tool-loop) は item 間で独立なので並列に流し、
    # spots/aliases への反映は順序を保って逐次で行う。並列調査は他 item が同時に
    # 掲載する新規スポットを知らないが、同一施設は apply_decision の place_id 照合が
    # 出典追記に倒すので二重登録にはならない
    def resolve(item):
        post, images = None, []
        if mcp:
            try:
                post = get_post(mcp, item["post"].rstrip("/").split("/")[-1])
                if post:
                    images = dj.download_images(post.get("media_urls"), limit=4)
            except Exception as e:
                log(f"  [{item['name']}] post fetch skip:", repr(e))
        collected = {}
        try:
            decision = investigate(item, post, images, doc, aliases,
                                   style_examples, collected)
            return post, collected, decision, None
        except Exception as e:
            return post, collected, None, e

    with ThreadPoolExecutor(max_workers=dj.WORKERS) as pool:
        resolved = list(pool.map(resolve, todo))

    for item, (post, collected, decision, err) in zip(todo, resolved):
        log(f"item: {item['name']} ({item.get('reason', '')})")
        if err:
            log("  resolve error:", repr(err))
            hold(item, new_items, f"実行エラー: {type(err).__name__}", stats)
            continue
        try:
            apply_decision(decision, item, post, doc, aliases,
                           new_items, collected, stats)
        except Exception as e:
            log("  apply error:", repr(e))
            hold(item, new_items, f"実行エラー: {type(e).__name__}", stats)

    pending["items"] = new_items + rest + waiting
    dj.save(workdir, "spots.json", doc)
    dj.save(workdir, "aliases.json", aliases)
    dj.save(workdir, "pending.json", pending)

    v = subprocess.run([sys.executable, "validate.py"], cwd=workdir,
                       capture_output=True, encoding="utf-8", errors="replace")
    log(v.stdout.strip())
    if v.returncode != 0:
        dj.github_issue("[pending-resolver] validate.py 失敗のため反映中止",
                        v.stdout + v.stderr)
        return

    msg = (f"pending整理(auto) {dj.TODAY}: +{len(stats['added'])} spots"
           f"{' (' + ', '.join(stats['added'][:5]) + ')' if stats['added'] else ''}, "
           f"出典追記{len(stats['appended'])}, 削除{len(stats['dropped'])}, "
           f"保留{stats['held']}, aliases+{stats['aliases']}")
    log("summary:", msg)
    for d in stats["dropped"]:
        log("  dropped:", d)
    dj.commit_if_changed(workdir, msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 失敗を Issue で可視化してから落とす (トレースバックは伏せ字にして自前で出す)
        dj.fail("pending-resolver", e)
