# -*- coding: utf-8 -*-
"""公式サイトURL (Places の websiteUri 等) を spot.url に載せてよいか、実際に取得して確かめる共通モジュール。
2026-10 奈良版で確立。backfill_places.py / url_check.py / 各リポの pipeline/daily_job.py (同等の実装) が使う。

    from safe_url import safe_url
    u = safe_url(place.get("websiteUri"), blocked_domains={"japoncompany.business"})
    if u: spot["url"] = u

載せない (None を返す) 条件:
  - http(s) 以外 / 除外ドメイン (第三者のディレクトリ系サイト。サブドメイン含む)
  - 取得失敗・4xx/5xx … 失効ドメインは第三者に再取得されフィッシングに転用され得る
  - 別サイト (登録ドメイン違い) へのリダイレクト (twitter.com→x.com 等の SNS 間は許容)
  - パスワード入力欄 / パーキング・ギャンブル・ログイン系の <title>
  - TLS 検証失敗 (中間証明書不足の正規サイトも落ちるが安全側に倒す。Windows の Python は GlobalSign 系で
    SSLError になりがちなので、初期データの目視確認では curl / openssl で証明書を見ること)
載せる場合はリダイレクト後の最終URL (http→https 等) から utm_* を落としたものを返す。
※ 同じサイト内で「別の店のページ」を指している誤りは機械判定できない → url_check.py の「ページに店名なし」を目視。
"""
import re
from urllib.parse import urlparse

import requests

SNS = ("twitter.com", "x.com", "instagram.com", "facebook.com")
# title だけを見る (本文は通販サイトの「お支払い」「クレジットカード」等で誤検知が多い)
BAD_TITLE = re.compile(
    r"casino|カジノ|slot|スロット|bitcoin|crypto|仮想通貨|出会い|アダルト|porn|"
    r"for sale|domain|ドメイン|parked|ログイン|login|sign in|verify|認証", re.I)
_SLD2 = ("co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "lg.jp", "gr.jp", "ed.jp", "ad.jp",
         "com.vn", "co.uk", "com.au", "co.kr", "com.tw")
_CACHE = {}


def site_of(host):
    """登録ドメイン相当 (www.kohfukuji.com → kohfukuji.com / www.pref.nara.lg.jp → nara.lg.jp)。"""
    labels = (host or "").lower().split(".")
    n = 3 if ".".join(labels[-2:]) in _SLD2 else 2
    return ".".join(labels[-n:])


def blocked(host, blocked_domains):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in blocked_domains)


def inspect(u, blocked_domains=(), timeout=15):
    """(載せてよい最終URL or None, 理由) を返す。理由は None なら合格。"""
    pu = urlparse((u or "").strip())
    if pu.scheme not in ("http", "https") or not pu.hostname:
        return None, "http(s) 以外"
    if blocked(pu.hostname, blocked_domains):
        return None, "除外ドメイン"
    try:
        r = requests.get(u, timeout=timeout, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"})
    except Exception as e:
        return None, f"取得失敗 {type(e).__name__}"
    final = r.url
    h1 = urlparse(final).hostname or ""
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    s0, s1 = site_of(pu.hostname), site_of(h1)
    if s0 != s1 and not (s0 in SNS and s1 in SNS):
        return None, f"別サイトへリダイレクト → {h1}"
    if blocked(h1, blocked_domains):
        return None, "除外ドメインへリダイレクト"
    html = r.text[:300000] if "html" in r.headers.get("content-type", "") else ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = m.group(1) if m else ""
    if s1 not in SNS:
        if BAD_TITLE.search(title):
            return None, f"不審な title: {title.strip()[:60]}"
        if re.search(r"<input[^>]+type=[\"']?password", html, re.I):
            return None, "パスワード入力欄"
    return re.sub(r"[?&]utm_[^&#]*", "", final).rstrip("?&"), None


def safe_url(u, blocked_domains=(), log=print):
    """載せてよい最終URL、ダメなら None (理由を log に出す)。同一URLは結果をキャッシュ。"""
    if u not in _CACHE:
        url, why = inspect(u, blocked_domains)
        if why:
            log(f"url drop ({why}): {u}")
        _CACHE[u] = url
    return _CACHE[u]
