# -*- coding: utf-8 -*-
"""取得専用 Cloud Run Service。敵対的サイトへの実 HTTP はすべてここで行い、
orchestrator は本サービス (信頼済み TLS ピア) としか喋らない。

デプロイ前提 (deploy.sh):
  - SA は IAM ロール 0 件・シークレット 0 件。万一 RCE されてもメタデータで出る
    トークンは無権限で、env に秘密も無いので盗める物が無い。
  - --no-allow-unauthenticated。呼び出し認証は Cloud Run の IAM (run.invoker) が
    ID トークンで強制するため、アプリ側ではトークン検証しない。
"""
import base64
import os

from flask import Flask, jsonify, request

import fetchers_core as core
from safe_url import inspect as safe_url_inspect

app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/safe-url")
def safe_url_ep():
    d = request.get_json(force=True, silent=True) or {}
    url = d.get("url")
    blocked = set(d.get("blocked_domains") or [])
    if not url:
        return jsonify({"url": None, "reason": "no url"})
    final, why = safe_url_inspect(url, blocked)
    return jsonify({"url": final, "reason": why})


@app.post("/image")
def image_ep():
    d = request.get_json(force=True, silent=True) or {}
    url = d.get("url")
    img = core.download_image(url) if url else None
    if not img:
        return jsonify({"image": None})
    data, mime = img
    return jsonify({"image": {"b64": base64.b64encode(data).decode(), "mime": mime}})


@app.post("/embeddable")
def embeddable_ep():
    d = request.get_json(force=True, silent=True) or {}
    url = d.get("url")
    return jsonify({"ok": core.check_embeddable(url) if url else None})


if __name__ == "__main__":  # ローカル確認用。本番は gunicorn (Dockerfile) 経由
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
