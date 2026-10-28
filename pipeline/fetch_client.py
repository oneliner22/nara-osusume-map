# -*- coding: utf-8 -*-
"""orchestrator 側の薄いクライアント。daily_job から元の関数と同一シグネチャで呼べる。

  - FETCHER_URL が設定されていれば fetcher Service を ID トークン付きで呼ぶ (本番)。
  - 未設定なら fetchers_core を直接呼ぶ (ローカル開発・backfill_places)。

いずれも失敗時は fail-closed:
  safe_url → None (載せない) / 画像 → 空 / embeddable → None (既存値を維持)。
"""
import base64
import os
import sys
import threading
import time

import requests

from urls_util import as_url_list

FETCHER_URL = os.environ.get("FETCHER_URL", "").rstrip("/")
_TIMEOUT = float(os.environ.get("FETCHER_TIMEOUT", "60"))


def _log(*a):
    print("[fetch_client]", *a, file=sys.stderr, flush=True)


# ---- ID トークン (audience = fetcher の URL)。約1h 有効なので50分で先回り更新、
#      401 のときだけ強制再取得して1回再試行する ----
_tok = {"val": None, "at": 0.0}
_tok_lock = threading.Lock()


def _id_token(force=False):
    with _tok_lock:
        if force or not _tok["val"] or time.time() - _tok["at"] > 3000:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token
            _tok["val"] = id_token.fetch_id_token(Request(), FETCHER_URL)
            _tok["at"] = time.time()
        return _tok["val"]


def _post(path, payload):
    """fetcher へ POST。成功で (json, None)、失敗で (None, error)。"""
    url = FETCHER_URL + path
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=payload, timeout=_TIMEOUT,
                              headers={"Authorization": f"Bearer {_id_token(force=attempt == 2)}"})
            if r.status_code == 401 and attempt == 1:
                continue  # トークン失効 → 強制再取得して1回だけ再試行
            r.raise_for_status()
            return r.json(), None
        except requests.RequestException as e:
            if attempt == 2:
                return None, e
    return None, RuntimeError("unreachable")


# ---- 公開 API (daily_job の元関数と同一シグネチャ) ----

def safe_url(u, blocked_domains=(), log=print):
    if not FETCHER_URL:
        import fetchers_core
        return fetchers_core.safe_url(u, blocked_domains, log=log)
    j, err = _post("/safe-url", {"url": u, "blocked_domains": sorted(blocked_domains)})
    if err:
        log(f"url drop (fetcher error {type(err).__name__}): {u}")
        return None  # fail-closed: 取得できなければ載せない
    if not j.get("url") and j.get("reason"):
        log(f"url drop ({j['reason']}): {u}")
    return j.get("url")


def check_embeddable(url):
    if not FETCHER_URL:
        import fetchers_core
        return fetchers_core.check_embeddable(url)
    j, err = _post("/embeddable", {"url": url})
    if err:
        _log("oembed check skip:", repr(err))
        return None
    return j.get("ok")


def download_images(media_urls, limit=8):
    # URL 正規化と件数制限は非取得・低リスクなのでここ (orchestrator) で行い、
    # 実取得だけを fetcher に 1 URL ずつ委譲する (応答サイズも 1 枚に収まる)。
    urls = as_url_list(media_urls)[:limit]
    if not FETCHER_URL:
        import fetchers_core
        return [img for u in urls if (img := fetchers_core.download_image(u))]
    out = []
    for u in urls:
        j, err = _post("/image", {"url": u})
        if err:
            _log("image skip:", u, repr(err))
            continue
        im = j.get("image")
        if im:
            out.append((base64.b64decode(im["b64"]), im["mime"]))
    return out
