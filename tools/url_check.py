# -*- coding: utf-8 -*-
"""spots.json の url を1件ずつ取得し、フィッシング・乗っ取り・無関係サイトの疑いを洗い出す。
出力: work/url_check.json (全件) と標準出力 (要確認のみ)"""
import io, json, re, sys
from urllib.parse import urlparse
import requests

SPOTS = sys.argv[1] if len(sys.argv) > 1 else "data/spots.json"
SNS = ("instagram.com", "twitter.com", "x.com", "facebook.com")
# フィッシング・詐欺・乗っ取り (失効ドメインの再取得) でよく出る語
BAD = re.compile(r"(casino|カジノ|slot|スロット|bitcoin|crypto|仮想通貨|loan|ローン|出会い|アダルト|porn|viagra|"
                 r"verify your account|パスワード|password|ログインして|お支払い|クレジットカード|当選|"
                 r"this domain (is|may be) for sale|ドメイン.{0,6}(販売|売却|購入)|parked|お名前\.com|ムームードメイン)", re.I)


def norm(s):
    return re.sub(r"[\s　・\-‐（）()「」『』【】]", "", s or "").lower()


def check(s):
    u = s["url"]
    r = {"slug": s["slug"], "name": s["name"], "url": u, "host": urlparse(u).netloc, "flags": []}
    if u.startswith("http://"):
        r["flags"].append("http(非TLS)")
    if any(h in r["host"] for h in SNS):
        r["sns"] = True
    try:
        resp = requests.get(u, timeout=20, allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130"})
        r["status"] = resp.status_code
        r["final"] = resp.url
        fh = urlparse(resp.url).netloc
        if fh.split(":")[0].removeprefix("www.") != r["host"].removeprefix("www."):
            r["flags"].append(f"別ホストへリダイレクト→{fh}")
        if resp.status_code >= 400:
            r["flags"].append(f"HTTP {resp.status_code}")
        resp.encoding = resp.apparent_encoding if resp.encoding in (None, "ISO-8859-1") else resp.encoding
        html = resp.text[:300000]
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        r["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""
        text = re.sub(r"<[^>]+>", " ", html)
        bad = BAD.search(r["title"] + " " + text[:20000])
        if bad:
            r["flags"].append(f"不審語: {bad.group(0)}")
        if re.search(r'<input[^>]+type=["\']?password', html, re.I):
            r["flags"].append("パスワード入力欄あり")
        # 店名 (主要部分) がページに出てくるか
        key = norm(re.sub(r"[（(].*?[）)]", "", s["name"]))
        parts = [p for p in re.split(r"\s+", re.sub(r"[（(].*?[）)]", "", s["name"])) if len(norm(p)) >= 2]
        body = norm(r["title"] + text)
        if not r.get("sns") and key and key not in body and not any(norm(p) in body for p in parts):
            r["flags"].append("ページに店名なし")
    except Exception as e:
        r["flags"].append(f"取得失敗: {type(e).__name__}")
    return r


from concurrent.futures import ThreadPoolExecutor
doc = json.load(io.open(SPOTS, encoding="utf-8"))
targets = [s for s in doc["spots"] if s.get("url")]
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(check, targets))
json.dump(res, io.open("url_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
out = io.open("url_check.txt", "w", encoding="utf-8")
for r in res:
    if r["flags"]:
        out.write(f"{r['slug']} | {r['name']} | {r['url']} | final={r.get('final')} | title={r.get('title')} | {'; '.join(r['flags'])}\n")
print(len(res), "checked,", sum(1 for r in res if r["flags"]), "flagged")
