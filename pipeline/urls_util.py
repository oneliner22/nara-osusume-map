# -*- coding: utf-8 -*-
"""run_sql の VARCHAR[] (list でも文字列表現でも返り得る) を URL リストに正規化する小物。
daily_job (自サイト判定) / fetch_client (画像DL前の件数制限) の双方が使う。URL 文字列の
パースだけで実取得はしないため、資格情報を持つ orchestrator 側で実行してよい。"""
import json
import re


def as_url_list(v):
    if isinstance(v, list):
        return [u for u in v if isinstance(u, str) and u.startswith("http")]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [u for u in parsed if isinstance(u, str) and u.startswith("http")]
        except ValueError:
            pass
        return re.findall(r"https?://[^\s'\"\],]+", v)
    return []
