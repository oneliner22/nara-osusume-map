# -*- coding: utf-8 -*-
"""敵対的インターネットへ実接続する純粋関数。第三者の HTTP 応答・画像バイトという
最も信用できない入力はすべてここを通る = ここが攻撃面。したがってこのコードを動かす
プロセスには鍵束 (SA トークン・シークレット・push 権限) を持たせないこと。

利用者は 2 つ:
  - fetcher.py        : 資格情報ゼロの Cloud Run Service (本番の実行主体)
  - fetch_client.py   : FETCHER_URL 未設定時のローカルフォールバック (開発・backfill)

safe_url (websiteUri 検査) はスキル共通の safe_url.py を re-export する。
"""
import requests

from safe_url import safe_url  # noqa: F401  (re-export)

IMAGE_MAX_BYTES = 10_000_000

# br を広告しない (gzip/deflate のみ)。準拠サーバの br 送出を避ける保険。
# 本命の防御は「イメージに brotli デコーダを入れない」(fetcher.Dockerfile のガード)。
_NO_BR = {"Accept-Encoding": "gzip, deflate"}


def download_image(url, timeout=30):
    """1 枚だけ取得する。画像でない / サイズ超過 / 通信失敗なら None、成功なら (bytes, mime)。
    stream=True + content-length で超過分を全量 DL せずに弾き、読み取り中も上限で打ち切る。"""
    try:
        with requests.get(url, timeout=timeout, stream=True, headers=_NO_BR) as r:
            mime = r.headers.get("content-type", "")
            clen = int(r.headers.get("content-length") or 0)
            if not (r.ok and mime.startswith("image/")) or clen > IMAGE_MAX_BYTES:
                return None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > IMAGE_MAX_BYTES:
                    return None
            return (bytes(buf), mime.split(";")[0]) if buf else None
    except requests.RequestException:
        return None


def check_embeddable(url, timeout=20):
    """X の oEmbed で埋め込み可否を判定。可=True / 不可(403,404)=False /
    判定不能(通信エラー・想定外ステータス)=None。"""
    try:
        r = requests.get("https://publish.twitter.com/oembed",
                         params={"url": url, "omit_script": 1, "dnt": "true"},
                         timeout=timeout, headers=_NO_BR)
    except requests.RequestException:
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (403, 404):
        return False
    return None
