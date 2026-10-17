# -*- coding: utf-8 -*-
"""spots.json の url を一括チェックして、フィッシング・失効ドメイン・誤リンクの疑いを洗い出す (公開前に必ず実行)。
2026-10 奈良版で確立。safe_url.py と同じフォルダに置いて、リポ直下で:
  python tools/url_check.py [data/spots.json]
出力: url_check.json (全件) / url_check.txt (要確認のみ。目視して外す・差し替える)

フラグの読み方:
  - safe_url NG: 取得失敗 / 4xx / 別サイトへリダイレクト / 不審 title / パスワード欄 → 原則 url を外す
    (TLS の SSLError は Windows の証明書ストア起因の誤検知がある → curl / openssl で確認してから判断)
  - ページに店名なし: 運営会社名で出ている正規サイトが多いが、同一ドメイン内の「別の店」を指す誤りもここで見つかる
  - http(非TLS): safe_url が返す https の最終URLに差し替えればよい (final 列)
"""
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safe_url import inspect, SNS, site_of  # noqa: E402
import requests  # noqa: E402

SPOTS = sys.argv[1] if len(sys.argv) > 1 else "data/spots.json"
BLOCKED = set()
if os.path.exists("data/pipeline.json"):
    BLOCKED = set(json.load(io.open("data/pipeline.json", encoding="utf-8")).get("url_blocked_domains", []))


def norm(s):
    return re.sub(r"[\s　・\-‐（）()「」『』【】]", "", s or "").lower()


def check(s):
    u = s["url"]
    r = {"slug": s["slug"], "name": s["name"], "url": u, "flags": []}
    final, why = inspect(u, BLOCKED)
    r["final"] = final
    if why:
        r["flags"].append(f"safe_url NG: {why}")
    elif u.startswith("http://"):
        r["flags"].append("http(非TLS)")
    if final and site_of(re.sub(r"^https?://([^/]+).*", r"\1", final)) not in SNS:
        # 店名 (括弧書きを除いた主要部分) がページに出てくるか
        try:
            html = requests.get(final, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text[:300000]
        except Exception:
            html = ""
        body = norm(re.sub(r"<[^>]+>", " ", html))
        base = re.sub(r"[（(].*?[）)]", "", s["name"])
        parts = [p for p in re.split(r"\s+", base) if len(norm(p)) >= 2]
        if body and norm(base) not in body and not any(norm(p) in body for p in parts):
            r["flags"].append("ページに店名なし")
    return r


doc = json.load(io.open(SPOTS, encoding="utf-8"))
targets = [s for s in doc["spots"] if s.get("url")]
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(check, targets))
json.dump(res, io.open("url_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
with io.open("url_check.txt", "w", encoding="utf-8") as out:
    for r in res:
        if r["flags"]:
            out.write(f"{r['slug']} | {r['name']} | {r['url']} | final={r['final']} | {'; '.join(r['flags'])}\n")
print(len(res), "checked,", sum(1 for r in res if r["flags"]), "flagged -> url_check.txt")
