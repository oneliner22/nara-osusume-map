# -*- coding: utf-8 -*-
"""日次入稿ジョブ (Cloud Run Jobs)。

X検索(xdev) → 画像取得 → Gemini抽出 → bot判定 → Places裏取り →
受入ゲート → data/*.json 更新 → commit/push。詳細は README の
「自動収集パイプライン」参照。人手レビューなしで運用するため、
掲載可否は必ずコード側のゲート (gate 関数) で強制する。

必要な環境変数:
  GITHUB_REPO   owner/name (例 oneliner22/nara-osusume-map)
  GITHUB_TOKEN  push / Issue 起票用
  XDEV_MCP_URL  xdev MCP エンドポイント (認証キー込み)
  PLACES_API_KEY
  GCP_PROJECT / VERTEX_LOCATION (既定 global)
  MODEL_EXTRACT / MODEL_JUDGE (既定は pipeline/models.json)
  MODEL_JUDGE_FALLBACK  MODEL_JUDGE が 429 のとき代わりに使う GA モデル (既定は pipeline/models.json)
  GEMINI_CONCURRENCY    Gemini 同時呼び出し数の上限 (既定 3。WORKERS とは別枠)
  INGEST_WINDOW_HOURS  x_ingest の取得窓 (既定 25。日次実行の再取得課金を抑える)
  INGEST_SINCE  RFC3339。初回バックフィル用: この時刻までフルアーカイブ検索で遡る
                (BACKFILL_WINDOW_DAYS 刻み・各500件上限。日次運用では未設定にする)
  DAILY_CAP     サーキットブレーカー閾値の上書き (初回バックフィル用)
  FORCE_RUN=1   pipeline.json の until (更新終了日) を過ぎていても実行する
  DRY_RUN=1 で push しない (差分はログに出す)
"""
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
from google import genai
from mcp_client import McpClient
import safe_url as safe_url_mod

REPO = os.environ["GITHUB_REPO"]
TOKEN = os.environ["GITHUB_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
TODAY = date.today().isoformat()
INGEST_SINCE = os.environ.get("INGEST_SINCE")  # 初回バックフィル専用

CATS = ["グルメ", "温泉", "カフェ・喫茶", "観光", "レトロ", "雑貨・土産",
        "自然", "動物", "体験", "宿"]
AREA_PALETTE = ["#5c8f3f", "#c2543f", "#3f6fa8", "#a8623f", "#6f3fa8", "#3fa89b"]

# Vertex の 429 (RESOURCE_EXHAUSTED) は共有枠の混雑で出ることが多く、SDK 既定のリトライ
# (十数秒で諦める) では足りずジョブごと落ちていた。指数バックオフで最大 60s 間隔・8 回まで粘る
client = genai.Client(
    vertexai=True, project=os.environ.get("GCP_PROJECT"),
    location=os.environ.get("VERTEX_LOCATION", "global"),
    # timeout (ms): 応答が返らず無期限に固まることがある (2026-10 奈良版バックフィルで発生) ので打ち切って再試行させる
    http_options=genai.types.HttpOptions(timeout=180_000, retry_options=genai.types.HttpRetryOptions(
        attempts=8, initial_delay=2, max_delay=60, exp_base=2, jitter=1,
        http_status_codes=[429, 500, 502, 503, 504])))
# モデルIDの正本は pipeline/models.json (版上げはそこだけ書き換えてイメージを再ビルド)
with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json"),
             encoding="utf-8") as _f:
    _MODELS = json.load(_f)
MODEL_EXTRACT = os.environ.get("MODEL_EXTRACT", _MODELS["extract"])
MODEL_JUDGE = os.environ.get("MODEL_JUDGE", _MODELS["judge"])
# preview モデルは枠が狭い。リトライしても 429 なら GA モデルで判定を続ける
MODEL_JUDGE_FALLBACK = os.environ.get("MODEL_JUDGE_FALLBACK", _MODELS["judge_fallback"])
_gemini_slots = threading.BoundedSemaphore(int(os.environ.get("GEMINI_CONCURRENCY", "3")))


def _git_auth_header():
    """GitHub の HTTPS 認証を http.extraHeader で渡すための値。トークンを URL やコマンド引数に
    載せない (CalledProcessError の repr にはコマンドがそのまま入るため)。"""
    import base64
    return "AUTHORIZATION: basic " + base64.b64encode(
        f"x-access-token:{TOKEN}".encode()).decode()


def git_env():
    """git に認証ヘッダを環境変数経由で渡す (GitHub Actions の checkout と同じ方式)。
    clone 後の remote URL にもトークンが残らない。"""
    env = dict(os.environ)
    env.update({"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": _git_auth_header()})
    return env


def _secret_values():
    """伏せ字にする文字列。認証キー込みの URL は全文だけでなく「パス+クエリ」と各クエリ値も
    対象にする (requests の接続エラーは 'Max retries exceeded with url: /path?key=...' のように
    ホストを除いた部分だけを含むので、全文一致だけではすり抜ける)。"""
    from urllib.parse import urlparse, parse_qsl
    vals = [TOKEN, os.environ.get("PLACES_API_KEY"), _git_auth_header()]
    u = os.environ.get("XDEV_MCP_URL")
    if u:
        vals.append(u)
        pu = urlparse(u)
        tail = pu.path + ("?" + pu.query if pu.query else "")
        if len(tail) > 8:
            vals.append(tail)
        vals += [v for _, v in parse_qsl(pu.query) if len(v) >= 8]
    # 長いものから置換する (短い値を先に伏せると、それを含む長い値が一致しなくなる)
    return sorted({v for v in vals if v and v != "-"}, key=len, reverse=True)


SECRET_VALUES = _secret_values()


def redact(s):
    """出力文字列から秘密情報を伏せる。障害時の例外 repr には認証キー込み XDEV_MCP_URL 等が
    含まれ得るため、ログ・Issue に出る全経路で通すこと
    (Issue は公開リポジトリなので漏れると即公開になる)。"""
    s = str(s)
    for v in SECRET_VALUES:
        s = s.replace(v, "***")
    return s


def fail(tag, e):
    """ジョブ失敗時の共通処理: 伏せ字にしたトレースバックをログに、例外の要約を Issue に書いて終了。
    素の raise だと Python の未処理トレースバックが redact を通らずにログへ出るので使わない。"""
    import traceback
    log("FATAL:", repr(e))
    log(traceback.format_exc())
    try:
        github_issue(f"[{tag}] ジョブ失敗 {TODAY}", f"```\n{type(e).__name__}: {e}\n```")
    except Exception:
        pass
    sys.exit(1)


def log(*a):
    print(*(redact(x) for x in a), flush=True)


def _generate_with_transport_retry(model, contents, config, attempts=3):
    """SDK のリトライは HTTP ステータスのみが対象で、接続断 (RemoteProtocolError 等) や
    タイムアウトでは即例外になる (2026-10 奈良版バックフィルがこれで落ちた)。数回だけ再試行する。"""
    import time
    import httpx
    for i in range(attempts):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except httpx.TransportError as e:
            if i == attempts - 1:
                raise
            log(f"WARN: Gemini 通信エラー {type(e).__name__} -> 再試行 {i + 1}/{attempts - 1}")
            time.sleep(5 * (i + 1))


def gen_json(model, schema, parts):
    """構造化出力つき Gemini 呼び出し。parts は str / (bytes, mime) の混在リスト。"""
    contents = []
    for p in parts:
        if isinstance(p, str):
            contents.append(p)
        else:
            contents.append(genai.types.Part.from_bytes(data=p[0], mime_type=p[1]))
    config = {"response_mime_type": "application/json", "response_schema": schema,
              "temperature": 0}
    with _gemini_slots:
        try:
            res = _generate_with_transport_retry(model, contents, config)
        except genai.errors.ClientError as e:
            if e.code != 429 or model != MODEL_JUDGE or not MODEL_JUDGE_FALLBACK:
                raise
            log(f"WARN: {model} 429 -> fallback {MODEL_JUDGE_FALLBACK}")
            res = client.models.generate_content(
                model=MODEL_JUDGE_FALLBACK, contents=contents, config=config)
    return json.loads(res.text)


# ---------------- リポジトリ入出力 ----------------

def clone_repo(workdir):
    subprocess.run(["git", "clone", "--depth", "1", f"https://github.com/{REPO}.git", workdir],
                   check=True, capture_output=True, env=git_env())


def load(workdir, name):
    return json.load(io.open(os.path.join(workdir, "data", name), encoding="utf-8"))


# 第三者のディレクトリ系サイト。Places の websiteUri に入っていても公式サイトではないので
# spot["url"] に採用しない (pipeline.json の url_blocked_domains で追加できる)
URL_BLOCKED_DOMAINS = {"japoncompany.business"}


def load_url_blocklist(pipeline_cfg):
    URL_BLOCKED_DOMAINS.update(d.lower() for d in pipeline_cfg.get("url_blocked_domains", []))


def official_url(place):
    """Places の websiteUri を返す。除外ドメイン (サブドメイン含む) なら None。"""
    from urllib.parse import urlparse
    u = ((place or {}).get("websiteUri") or "").strip()
    pu = urlparse(u)
    host = (pu.hostname or "").lower()
    if pu.scheme not in ("http", "https") or not host:
        return None   # javascript: 等のスキームは href に入れない
    if any(host == d or host.endswith("." + d) for d in URL_BLOCKED_DOMAINS):
        return None
    # 実取得して確かめる (失効・別サイト・404・パスワード欄等は載せない)。実装はスキル共通の safe_url.py
    return safe_url_mod.safe_url(u, URL_BLOCKED_DOMAINS, log=log)


def clean_area_name(name):
    """Gemini が提案する新エリア名の無害化。HTML 特殊文字・制御文字を落とし 20 字まで。
    エリア名は index.html のチップにそのまま出るので、モデル出力を信用しない。"""
    s = re.sub(r"[<>&\"'`\\\x00-\x1f]", "", str(name or "")).strip()[:20]
    return s or "その他エリア"


def save(workdir, name, obj):
    json.dump(obj, io.open(os.path.join(workdir, "data", name), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def commit_if_changed(workdir, msg):
    subprocess.run(["git", "-C", workdir, "config", "user.name", "oneliner22"], check=True)
    subprocess.run(["git", "-C", workdir, "config", "user.email",
                    "oneliner22@users.noreply.github.com"], check=True)
    subprocess.run(["git", "-C", workdir, "add", "data"], check=True)
    diff = subprocess.run(["git", "-C", workdir, "diff", "--cached", "--stat"],
                          capture_output=True, encoding="utf-8",
                          errors="replace").stdout.strip()
    if not diff:
        log("no changes")
        return
    log(diff)
    if DRY_RUN:
        log("DRY_RUN: skip commit/push")
        return
    subprocess.run(["git", "-C", workdir, "commit", "-m", msg], check=True)
    subprocess.run(["git", "-C", workdir, "push"], check=True, env=git_env())
    log("pushed")


def github_issue(title, body):
    r = requests.post(f"https://api.github.com/repos/{REPO}/issues",
                      headers={"Authorization": f"Bearer {TOKEN}"},
                      json={"title": redact(title), "body": redact(body)}, timeout=30)
    log("issue:", r.status_code, title)


# ---------------- 収集 ----------------

INGEST_WINDOW_HOURS = float(os.environ.get("INGEST_WINDOW_HOURS", "25"))
WORKERS = int(os.environ.get("WORKERS", "8"))  # 画像DL/Gemini/Places の並列度


BACKFILL_WINDOW_DAYS = int(os.environ.get("BACKFILL_WINDOW_DAYS", "10"))


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ingest(mcp, args):
    ing = mcp.call("x_ingest", args)
    dataset = ing.get("dataset") if isinstance(ing, dict) else None
    if not dataset:
        raise RuntimeError(f"x_ingest から dataset を特定できない: {ing}")
    n = ing.get("fetched_posts") if isinstance(ing, dict) else None
    if n is not None and n >= args.get("max_posts", 0):
        log(f"warn: x_ingest が上限 {n} 件に達した (取りこぼしの可能性): "
            f"{args.get('start_time')}〜{args.get('end_time', 'now')}")
    return dataset


SQL_PAGE = 100  # run_sql は出力の文字数上限があり、本文の長い投稿が多い区画を一括で引くと
                # JSON ではなく切り詰めた文字列が返る (2026-10 奈良版バックフィルで発生) ためページングする


def _dataset_rows(mcp, dataset, ref_cond):
    rows_all, offset, page = [], 0, SQL_PAGE
    while True:
        # media_urls は ingest 時期によって NULL の区画があるため、同一IDの別区画から補完する
        rows = mcp.call("run_sql", {"max_rows": page, "query":
            "SELECT t.id, t.created_at, t.conversation_id, t.author_id, t.author_name, t.author_followers, "
            "t.text, COALESCE(t.media_urls, mm.mu) media_urls, t.urls, t.like_count, t.reply_count "
            "FROM tweets t LEFT JOIN (SELECT id, arg_max(media_urls, ingested_at) mu "
            "FROM tweets WHERE media_urls IS NOT NULL GROUP BY id) mm ON mm.id = t.id "
            f"WHERE t.dataset='{dataset}' {ref_cond}"
            f"ORDER BY t.created_at DESC, t.id LIMIT {page} OFFSET {offset}"})
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except ValueError:
                rows = None
        if not isinstance(rows, (dict, list)) or (isinstance(rows, dict) and rows.get("error")):
            if page > 10:   # 出力上限に当たったとみなしてページを細かくして引き直す
                page //= 2
                continue
            raise RuntimeError(f"run_sql の結果を解釈できない ({dataset} offset {offset}): {str(rows)[:200]}")
        got = rows.get("rows", []) if isinstance(rows, dict) else rows
        if got and isinstance(got[0], list):  # 列配列形式への保険
            cols = rows.get("columns", [])
            got = [dict(zip(cols, r)) for r in got]
        rows_all += got
        if len(got) < page:
            return rows_all
        offset += page


def fetch_posts(mcp, ledger, query, max_posts=200, include_refs=False, since=None):
    """query を ingest し、台帳未処理の投稿を返す。include_refs=True で reply/引用も含む。

    日次実行のため、既定では直近 INGEST_WINDOW_HOURS (25h) に絞って取得する
    (X API の post read 課金の抑制。無指定だと recent 検索が7日分を毎日引き直す)。
    since (aware datetime) 指定でさらに遡れるが、recent 検索の7日上限にクランプする。

    INGEST_SINCE (初回バックフィル) が設定されているときはフルアーカイブ検索に切り替える:
      - since 無指定 (メイン検索・公式監視) は INGEST_SINCE〜現在を BACKFILL_WINDOW_DAYS
        刻みの窓に分けて取得する (x_ingest は1回500件上限のため)
      - since 指定 (シード収穫・続き投稿) は since〜現在を1回で取得する (件数が少ないため)
    """
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    datasets = []
    if INGEST_SINCE:
        if since is None:
            cur = datetime.fromisoformat(INGEST_SINCE.replace("Z", "+00:00"))
            while cur < now:
                end = min(cur + timedelta(days=BACKFILL_WINDOW_DAYS), now)
                datasets.append(_ingest(mcp, {
                    "query": query, "max_posts": 500, "archive": True,
                    "start_time": _ts(cur), "end_time": _ts(end)}))
                cur = end
        else:
            datasets.append(_ingest(mcp, {
                "query": query, "max_posts": 500, "archive": True,
                "start_time": _ts(min(since, now - timedelta(minutes=1)))}))
    else:
        if since is None:
            since = now - timedelta(hours=INGEST_WINDOW_HOURS)
        oldest = now - timedelta(days=7) + timedelta(minutes=10)
        datasets.append(_ingest(mcp, {"query": query, "max_posts": max_posts,
                                      "start_time": _ts(max(since, oldest))}))
    ref_cond = "" if include_refs else "AND t.ref_type IS NULL "
    posts, seen = [], set()
    for dataset in datasets:
        for p in _dataset_rows(mcp, dataset, ref_cond):
            if str(p["id"]) not in seen:
                seen.add(str(p["id"]))
                posts.append(p)
    return [p for p in posts if str(p["id"]) not in ledger["processed_posts"]]


ASK_SCHEMA = {"type": "object", "properties": {
    "is_seed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["is_seed"]}


def add_seed(ledger, pid, kind, author, base_date, ttl_days):
    if pid in ledger["seeds"]:
        return
    until = (date.fromisoformat(base_date) + timedelta(days=ttl_days)).isoformat()
    ledger["seeds"][pid] = {"kind": kind, "author": author,
                            "date": base_date, "until": until}
    log(f"seed added: {pid} ({kind}, {author}, until {until})")


def watch_official(mcp, ledger, rh):
    """公式アカウントの投稿を監視し、おすすめ募集らしき投稿をシード化。投稿自体も処理対象として返す。"""
    accounts = rh.get("official_accounts", [])
    if not accounts:
        return []
    q = " OR ".join(f"from:{a}" for a in accounts)
    posts = fetch_posts(mcp, ledger, f"({q})" if len(accounts) > 1 else q,
                        max_posts=50, include_refs=True)
    done = [0]

    def judge(p):
        try:
            return _judge(p)
        except Exception as e:   # 1件の失敗でジョブを落とさない (シードではない扱い)
            log("official judge skip:", p.get("id"), repr(e))
            return {}
        finally:
            done[0] += 1
            if done[0] % 50 == 0:
                log(f"official judged {done[0]}/{len(posts)}")

    def _judge(p):
        return gen_json(MODEL_EXTRACT, ASK_SCHEMA, [
            "以下はぽこピー(ぽんぽこ・ピーナッツくん)公式アカウントのX投稿です。"
            "奈良公演「ぽこピーの回覧板」(2026年12月27日・なら100年会館)に関連して、ファンにおすすめスポット・グルメ等の"
            "情報提供を呼びかけたり質問したりする投稿(そのreplyや引用RTにおすすめ情報が"
            "集まりそうな投稿)かを判定してください。\n"
            f"投稿: {p.get('text', '')}"])

    # バックフィルでは公式の半年分 (数百件) を判定するため並列化 (Gemini 同時数は gen_json 側で制限)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        verdicts = list(pool.map(judge, posts))
    for p, j in zip(posts, verdicts):
        if j.get("is_seed"):
            add_seed(ledger, str(p["id"]), "official_ask", p.get("author_name", ""),
                     str(p.get("created_at", ""))[:10] or TODAY, rh.get("seed_ttl_days", 35))
    log(f"official posts judged: {len(posts)}")
    return posts


def harvest_seeds(mcp, ledger, rh):
    """有効なシードの reply・引用RTを収穫する。"""
    # バックフィル時は期限切れシード (告知から35日超) も今回だけ収穫対象にする
    active = [(pid, s) for pid, s in ledger["seeds"].items()
              if s["until"] >= TODAY or INGEST_SINCE]
    active.sort(key=lambda kv: kv[1]["date"], reverse=True)
    active = active[:rh.get("max_active_seeds", 25)]
    harvested = []
    for pid, s in active:
        try:
            # 初回収穫のみシード投稿日まで遡る (シード化は投稿処理の翌日以降のため、
            # 25h窓だと投稿直後〜シード化前の reply を取りこぼす)。以降は既定の25h窓。
            since = None
            if not s.get("harvested"):
                since = datetime.fromisoformat(s["date"]).replace(tzinfo=timezone.utc)
            harvested += fetch_posts(
                mcp, ledger, f"(conversation_id:{pid} OR url:{pid}) -is:retweet",
                max_posts=50, include_refs=True, since=since)
            s["harvested"] = True
        except Exception as e:
            log("harvest skip:", pid, repr(e))
    ledger["seeds"] = {pid: s for pid, s in ledger["seeds"].items()
                       if s["until"] >= TODAY or INGEST_SINCE}
    return harvested, len(active)


# 続き投稿 (本人リプ・自己引用RT) の抽出に添える取得文脈。スポット名を含まない注記
# なので ground_candidates の実記載チェック (捏造防止) には影響しない
CONTINUATION_NOTE = ("(注: この投稿は、#ぽこピーの回覧板 向け奈良おすすめ紹介"
                     "スレッドに投稿者本人がぶら下げた続きの投稿"
                     " (リプライまたは自己引用RT))\n")


def as_url_list(v):
    """run_sql の VARCHAR[] は list でも文字列表現でも返り得るため正規化する。"""
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


# ---------------- X 出典URL ----------------

HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")


def post_id_of(url):
    m = re.search(r"/status(?:es)?/(\d+)", url or "")
    return m.group(1) if m else None


def x_post_url(pid, author):
    """X出典のURL。/i/web/status/<id> は X 側のリダイレクト頼みで、投稿者が
    年齢制限付きアカウントだと未ログイン閲覧者にはリダイレクトが解決されず
    X の404が出る。著者ハンドルが分かる限り正規形で持つこと。"""
    author = (author or "").lstrip("@")
    if HANDLE_RE.match(author):
        return f"https://x.com/{author}/status/{pid}"
    return f"https://x.com/i/web/status/{pid}"


def has_source(sources, url):
    """出典の重複判定。URL文字列ではなくポストIDで比較する
    (同じポストを指す別形式のURLを二重登録しないため)。"""
    pid = post_id_of(url)
    if not pid:
        return any(src.get("url") == url for src in sources)
    return any(post_id_of(src.get("url")) == pid for src in sources)


def check_embeddable(url):
    """X の oEmbed で埋め込み可否を判定する。年齢制限付きアカウントのポストは
    403 を返し、埋め込みも未ログイン閲覧もできない。可=True / 不可=False /
    判定できなかった場合 (通信エラー・想定外ステータス) は None。"""
    try:
        r = requests.get("https://publish.twitter.com/oembed",
                         params={"url": url, "omit_script": 1, "dnt": "true"},
                         timeout=20)
    except requests.RequestException as e:
        log("oembed check skip:", repr(e))
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (403, 404):
        return False
    log(f"oembed check: 想定外のステータス {r.status_code} ({url})")
    return None


def set_embed_flag(src):
    """出典に embed フラグを立てる。既定は埋め込み可なので、不可のときだけ
    embed:false を持たせる。判定できなかったときは既存の値を維持する。"""
    ok = check_embeddable(src["url"])
    if ok is True:
        src.pop("embed", None)
    elif ok is False:
        src["embed"] = False


def refresh_embed_flags(spots):
    """全X出典の埋め込み可否を洗い直す。投稿者がアカウント設定を変えれば可否も
    変わるため、新規分だけでなく毎日引き直す。"""
    srcs = [src for s in spots for src in s.get("sources", [])
            if src.get("type") == "x" and src.get("url")]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(set_embed_flag, srcs))
    n = sum(1 for src in srcs if src.get("embed") is False)
    log(f"embed check: {len(srcs)} sources ({n} 件は未ログイン閲覧不可)")


IMAGE_MAX_BYTES = 10_000_000


def download_images(media_urls, limit=8):
    images = []
    for u in as_url_list(media_urls)[:limit]:
        try:
            # stream=True + content-length で、サイズ超過を全量DLせずに弾く
            with requests.get(u, timeout=30, stream=True) as r:
                mime = r.headers.get("content-type", "")
                clen = int(r.headers.get("content-length") or 0)
                if not (r.ok and mime.startswith("image/")) or clen > IMAGE_MAX_BYTES:
                    continue
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > IMAGE_MAX_BYTES:
                        buf = None
                        break
                if buf:
                    images.append((bytes(buf), mime.split(";")[0]))
        except requests.RequestException as e:
            log("image skip:", u, e)
    return images


# ---------------- 抽出・判定 ----------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_recommendation": {"type": "boolean"},
        "candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "hint": {"type": "string"},
                "quote": {"type": "string"},
            }, "required": ["name"]}},
    }, "required": ["is_recommendation", "candidates"]}


def extract_spots(post, images):
    parts = [
        "以下はX(旧Twitter)の投稿です。「#ぽこピーの回覧板」奈良公演(奈良市・2026年12月27日)に向けた"
        "奈良県のおすすめスポット紹介かどうかを判定し、紹介されている具体的な"
        "(注意: 既に終わった愛媛公演(松山)・石川公演(金沢)等の感想・他県のおすすめは対象外。奈良向けのみ)"
        "スポット(店・施設・観光地)を漏れなく抽出してください。\n"
        "- おすすめリストは本文ではなく添付画像内に書かれていることが多い。画像を必ず読むこと。\n"
        "- 【最重要】本文または画像に実際に名前が記載されているスポットのみを抽出すること。"
        "「奈良のおすすめ」という文脈から定番観光地を推測・補完することは厳禁。"
        "名前の記載がなければ candidates は空にする。\n"
        "- name はスポットの正式名称に最も近い表記。hint は住所・エリア・ジャンルなど"
        "特定の手がかり。quote は投稿者の実際の記述の抜粋(30字程度)。実際の記述が"
        "なければ空文字にする。創作・脚色は厳禁。\n"
        "- 一般名詞(「海鮮丼」「温泉」等)や県外が明らかなスポットは含めない。\n"
        "- おすすめスポットをまとめた地図サイト・マップ自体の告知や宣伝(サイト画面の"
        "スクリーンショットに既存スポット名が並んでいるもの等)は投稿者自身のおすすめではない。"
        "is_recommendation=false・candidates は空にする。\n"
        f"本文: {post.get('text', '')}"]
    parts += images
    return gen_json(MODEL_EXTRACT, EXTRACT_SCHEMA, parts)


GROUND_SCHEMA = {"type": "object", "properties": {
    "grounded_names": {"type": "array", "items": {"type": "string"}}},
    "required": ["grounded_names"]}


def ground_candidates(post, images, candidates):
    """抽出候補が投稿(本文+画像)に実際に記載されているか検証し、記載のあるものだけ残す。
    文脈からの推測で定番スポットが捏造されるのを防ぐ二重チェック。"""
    if not candidates:
        return candidates
    names = [c["name"] for c in candidates]
    parts = [
        "以下のX投稿(本文+添付画像)に、候補リストの各スポット名が【実際に文字として記載"
        "されているか】を確認してください。表記ゆれ・略称は同一とみなしてよいが、"
        "記載がないものを文脈や一般常識から「ありそう」と補完することは厳禁。\n"
        f"候補: {json.dumps(names, ensure_ascii=False)}\n"
        f"本文: {post.get('text', '')}\n"
        "実際に記載が確認できた候補名のみを grounded_names にそのまま列挙すること。"]
    parts += images
    j = gen_json(MODEL_EXTRACT, GROUND_SCHEMA, parts)
    ok = set(j.get("grounded_names", []))
    kept = [c for c in candidates if c["name"] in ok]
    if len(kept) < len(candidates):
        dropped = [c["name"] for c in candidates if c["name"] not in ok]
        log("grounding dropped:", dropped)
    return kept


AUTHOR_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["fan", "bot", "uncertain"]},
    "reason": {"type": "string"}}, "required": ["verdict"]}


def author_gate(mcp, ledger, post):
    aid = str(post["author_id"])
    cached = ledger["authors"].get(aid)
    if cached:
        return cached["verdict"]
    base = (f"著者名: {post.get('author_name')} / フォロワー: {post.get('author_followers')}\n"
            f"投稿: {post.get('text', '')}")
    j = gen_json(MODEL_EXTRACT, AUTHOR_SCHEMA, [
        "X投稿の著者が「実在のぽこピー(ぽんぽこ・ピーナッツくん)ファンによる善意のおすすめ投稿」か、"
        "「bot・宣伝アカウント・無関係な便乗」かを判定してください。判断できなければ uncertain。\n" + base])
    verdict = j["verdict"]
    if verdict == "uncertain":
        rows = mcp.call("run_sql", {"max_rows": 20, "query":
            f"SELECT text FROM tweets WHERE author_id='{aid}' ORDER BY created_at DESC"})
        others = rows.get("rows", rows) if isinstance(rows, dict) else rows
        history = "\n".join((r.get("text", "") if isinstance(r, dict) else str(r))[:100]
                            for r in (others or [])[:20])
        j = gen_json(MODEL_JUDGE, AUTHOR_SCHEMA, [
            "X投稿の著者がぽこピーファンの実在ユーザーか、bot・宣伝アカウントかを判定してください。"
            "同一著者の過去投稿も参考にすること。判断できなければ uncertain。\n" + base +
            "\n過去投稿:\n" + history])
        verdict = j["verdict"]
    ledger["authors"][aid] = {"verdict": verdict, "name": post.get("author_name"),
                              "date": TODAY}
    return verdict


# ---------------- 裏取り (Places) ----------------

def places_search(name, hint, bbox):
    r = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={"X-Goog-Api-Key": os.environ["PLACES_API_KEY"],
                 "X-Goog-FieldMask":
                     "places.id,places.displayName,places.formattedAddress,"
                     "places.location,places.businessStatus,places.websiteUri,"
                     "places.regularOpeningHours.weekdayDescriptions"},
        json={"textQuery": f"{name} {hint or ''}".strip(), "languageCode": "ja",
              "locationBias": {"rectangle": {
                  "low": {"latitude": bbox["lat_min"], "longitude": bbox["lng_min"]},
                  "high": {"latitude": bbox["lat_max"], "longitude": bbox["lng_max"]}}}},
        timeout=30)
    r.raise_for_status()
    return r.json().get("places", [])[:3]


DESC_SCHEMA = {"type": "object", "properties": {"desc": {"type": "string"}},
               "required": ["desc"]}


def refresh_desc(spot):
    """X言及が増えたスポットの紹介文を、言及内容を織り込んで更新する。"""
    quotes = [f"- {x.get('author', '')}: {x.get('quote', '')}"
              for x in spot["sources"] if x.get("type") == "x" and x.get("quote")]
    if not quotes:
        return
    j = gen_json(MODEL_EXTRACT, DESC_SCHEMA, [
        "スポット紹介文を更新してください。既存の紹介文の情報(動画由来)を核に残しつつ、"
        "Xでのファン言及から加わった観点や推しポイントを自然に織り込むこと。"
        "文体は既存文に合わせ常体(だ・である調/体言止め中心、です・ます調禁止)、"
        "60〜110字、絵文字なし、事実の捏造なし。\n"
        f"スポット名: {spot['name']}\n既存の紹介文: {spot['desc']}\n"
        "Xでの言及:\n" + "\n".join(quotes)])
    if j.get("desc"):
        spot["desc"] = j["desc"]


JUDGE_SCHEMA = {"type": "object", "properties": {
    "match": {"type": "boolean"},
    "place_index": {"type": "integer"},
    "confidence": {"type": "string", "enum": ["high", "low"]},
    "category": {"type": "string", "enum": CATS},
    "area_id": {"type": "string"},
    "new_area_name": {"type": "string"},
    "slug": {"type": "string"},
    "desc": {"type": "string"}}, "required": ["match", "confidence"]}


def judge_candidate(cand, places, areas, style_examples):
    area_desc = "\n".join(f"- {a['id']}: {a['name']}" for a in areas)
    places_desc = "\n".join(
        f"[{i}] {p.get('displayName', {}).get('text')} / {p.get('formattedAddress')} / "
        f"status={p.get('businessStatus')} / ({p['location']['latitude']:.4f},{p['location']['longitude']:.4f})"
        for i, p in enumerate(places))
    return gen_json(MODEL_JUDGE, JUDGE_SCHEMA, [
        "Xで紹介されたスポット候補と、Google Places の検索結果を照合してください。\n"
        f"候補: {cand['name']} / 手がかり: {cand.get('hint', 'なし')} / 言及: {cand.get('quote', '')}\n"
        f"Places結果:\n{places_desc or '(ヒットなし)'}\n\n"
        "1. 候補と同一の施設が結果にあれば match=true, その place_index, confidence "
        "(同名別店・別支店の可能性が残るなら low)。\n"
        f"2. match の場合: category は次から選ぶ: {', '.join(CATS)}。\n"
        f"3. area_id は既存エリアから選ぶ:\n{area_desc}\n"
        "   どれにも地理的に属さない場合のみ area_id='new' とし new_area_name に"
        "「吉野・奈良南部」のような簡潔なエリア名を提案。\n"
        "4. slug は小文字英数の短い識別子。\n"
        "5. desc はこの地図の紹介文。以下の文体(常体・体言止め中心、です・ます調禁止)に合わせ、"
        "言及内容を踏まえ50-90字:\n" +
        "\n".join(f"例: {e}" for e in style_examples)])


# ---------------- 受入ゲート ----------------

def gate(judged, place, bbox):
    """掲載可否はここで強制する。戻り値 None=合格 / str=保留理由。"""
    if not judged.get("match"):
        return "places_no_match"
    if judged.get("confidence") != "high":
        return "low_confidence"
    if place.get("businessStatus") != "OPERATIONAL":
        return f"business_status={place.get('businessStatus')}"
    lat = place["location"]["latitude"]
    lng = place["location"]["longitude"]
    if not (bbox["lat_min"] <= lat <= bbox["lat_max"]
            and bbox["lng_min"] <= lng <= bbox["lng_max"]):
        return "out_of_bbox"
    return None


# 奈良県本体のおよその範囲 (外れたら out_of_pref。マージン付き bbox は pipeline.json)
PREF_STRICT = {"lat_min": 33.85, "lat_max": 34.79, "lng_min": 135.53, "lng_max": 136.24}


def normalize(name):
    return re.sub(r"[\s　]", "", name).lower()


def match_existing(cand_name, aliases, spots):
    name = aliases.get(cand_name.strip(), cand_name.strip())
    norm = normalize(name)
    for s in spots:
        if normalize(s["name"]) == norm:
            return s
    hits = difflib.get_close_matches(norm, [normalize(s["name"]) for s in spots],
                                     n=1, cutoff=0.85)
    if hits:
        return next(s for s in spots if normalize(s["name"]) == hits[0])
    return None


# ---------------- メイン ----------------

def main():
    workdir = tempfile.mkdtemp(prefix="spots-")
    clone_repo(workdir)
    spots_doc = load(workdir, "spots.json")
    pipeline_cfg = load(workdir, "pipeline.json")
    load_url_blocklist(pipeline_cfg)
    ledger = load(workdir, "ledger.json")
    aliases = load(workdir, "aliases.json")
    pending = load(workdir, "pending.json")
    until = pipeline_cfg.get("until")
    if until and TODAY > until and os.environ.get("FORCE_RUN") != "1":
        log(f"更新期間終了 (until {until}): 何もしない。Cloud Scheduler を削除してよい")
        return
    bbox = pipeline_cfg["bbox"]
    spots = spots_doc["spots"]
    areas = spots_doc["areas"]
    style_examples = [s["desc"] for s in spots[:3]]

    mcp = McpClient(os.environ["XDEV_MCP_URL"])
    ledger.setdefault("seeds", {})
    rh = pipeline_cfg.get("reply_harvest", {})

    # シード化は公式(ponpokoka)の呼びかけ検知のみ。処理済みおすすめ投稿の自動昇格は
    # 廃止済み (シードごとに x_ingest が走るため、収穫対象は呼びかけ投稿に限定して
    # API 課金を抑える)
    official = watch_official(mcp, ledger, rh)
    # キーワード入りならリプ・引用も処理対象 (ingest は元々ヒットしており追加課金なし)
    main_posts = fetch_posts(mcp, ledger, pipeline_cfg["x_query"], max_posts=200,
                             include_refs=True)
    harvested, n_seeds = harvest_seeds(mcp, ledger, rh)
    seen_ids, posts = set(), []
    for p in official + main_posts + harvested:
        if str(p["id"]) not in seen_ids:
            seen_ids.add(str(p["id"]))
            posts.append(p)
    log(f"new posts: {len(posts)} (official {len(official)} / main {len(main_posts)} "
        f"/ harvested {len(harvested)} from {n_seeds} seeds)")
    if not posts:
        refresh_embed_flags(spots)  # 新規ポストがない日も埋め込み可否は変わり得る
        save(workdir, "spots.json", spots_doc)
        save(workdir, "ledger.json", ledger)  # シード整理だけでも保存対象
        commit_if_changed(workdir, f"auto-ingest {TODAY}: no new posts (seeds {n_seeds})")
        return

    # 前処理 (画像DL→抽出→実記載チェック) と著者判定はポスト間で独立なので並列に流し、
    # ledger・spots への反映は順序を保って逐次で行う
    # このマップ自体へのリンクを含む投稿 (サイトの周知・宣伝) はおすすめではないので抽出しない
    self_urls = pipeline_cfg.get("self_site_urls", [])

    def links_self_site(post):
        return any(su in u for u in as_url_list(post.get("urls")) for su in self_urls)

    def prep(post):
        if links_self_site(post):
            return []
        images = download_images(post.get("media_urls"))
        p = post
        if post.get("_continuation"):  # 二段階目: 取得文脈を1行添えて抽出する
            p = dict(post)
            p["text"] = CONTINUATION_NOTE + (post.get("text") or "")
        ex = extract_spots(p, images)
        return ground_candidates(p, images, ex["candidates"]) \
            if ex["is_recommendation"] else []

    def prep_batch(batch):
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            prepped = list(pool.map(prep, batch))
            # 著者判定は候補ありポストの著者だけを著者単位で1回ずつ
            # (結果は ledger["authors"] にキャッシュされ、後段ループはキャッシュを引くだけ)
            gate_posts = {}
            for post, cands in zip(batch, prepped):
                if cands:
                    gate_posts.setdefault(str(post["author_id"]), post)
            list(pool.map(lambda p: author_gate(mcp, ledger, p), gate_posts.values()))
        return prepped

    prepped = prep_batch(posts)

    # ---- 二段階目: 適格投稿の「本人の続き投稿」を同run内で収穫する ----
    # おすすめを自分のリプや自己引用RTで連ねる投稿者が多く、続き投稿は単体では
    # 検索キーワードに合致しないことが多い。一段階目で適格 (候補あり・著者fan) と
    # なった投稿を起点に、本人リプは conversation_id 検索で一括、自己引用RTは
    # url: 検索で1ホップずつ拾う (引用の引用は見つかった続き投稿を新起点に反復)。
    # シードのような日をまたぐ監視はしない。このrunで適格になった投稿の連鎖のみ。
    def cont_roots(pairs):
        roots = []
        for post, cands in pairs:
            if not cands:
                continue
            verdict = ledger["authors"].get(str(post["author_id"]), {}).get("verdict")
            if verdict == "fan":  # uncertain は pending 直行のため連鎖も追わない
                roots.append(post)
        return roots

    sc = pipeline_cfg.get("self_continuation", {})
    continuations, queried = [], set()
    frontier = cont_roots(zip(posts, prepped))
    for _ in range(sc.get("max_rounds", 3)):
        if not frontier:
            break
        found = []
        for root in frontier[:sc.get("max_roots_per_run", 15)]:
            author = (root.get("author_name") or "").lstrip("@")
            rid = str(root["id"])
            conv = str(root.get("conversation_id") or "") or rid
            q = f"(conversation_id:{conv} OR url:{rid}) from:{author} -is:retweet"
            if not HANDLE_RE.match(author) or q in queried:
                continue
            queried.add(q)
            try:
                since = datetime.fromisoformat(
                    str(root.get("created_at")).replace("Z", "").replace("T", " ")
                    .split(".")[0]).replace(tzinfo=timezone.utc)
            except ValueError:
                since = None
            try:
                found += fetch_posts(mcp, ledger, q, max_posts=50,
                                     include_refs=True, since=since)
            except Exception as e:
                log("continuation skip:", rid, repr(e))
        fresh = []
        for p in found:
            pid = str(p["id"])
            if pid not in seen_ids:
                seen_ids.add(pid)
                p["_continuation"] = True
                fresh.append(p)
        if not fresh:
            break
        fresh_prepped = prep_batch(fresh)
        continuations += fresh
        posts += fresh
        prepped += fresh_prepped
        frontier = cont_roots(zip(fresh, fresh_prepped))
    if continuations:
        log(f"continuations harvested: {len(continuations)} posts "
            f"({len(queried)} queries)")

    stats = {"posts": len(posts), "continuations": len(continuations),
             "added": [], "source_added": [], "pending": 0,
             "skipped_bot": 0, "skipped_offtopic": 0}
    new_candidates = []  # (cand, post)
    desc_refresh = {}    # slug -> spot: 今回X言及が増え紹介文を更新すべきスポット

    for post, cands in zip(posts, prepped):
        pid = str(post["id"])
        post_url = x_post_url(pid, post.get("author_name"))
        if not cands:
            ledger["processed_posts"][pid] = {"date": TODAY, "result": "not_recommendation"}
            stats["skipped_offtopic"] += 1
            continue
        verdict = author_gate(mcp, ledger, post)
        if verdict == "bot":
            ledger["processed_posts"][pid] = {"date": TODAY, "result": "author_bot"}
            stats["skipped_bot"] += 1
            continue
        post_date = str(post.get("created_at", ""))[:10] or TODAY
        for cand in cands:
            cand["_post"] = {"id": pid, "url": post_url, "date": post_date,
                             "author": post.get("author_name", ""),
                             "author_uncertain": verdict == "uncertain"}
            existing = match_existing(cand["name"], aliases, spots)
            if existing:
                if has_source(existing["sources"], post_url):
                    continue
                if verdict == "uncertain":
                    # 著者の素性が判断できない投稿は、既存スポットへの出典追記・紹介文の
                    # 書き換え材料にもしない。pending に積み、人が ledger.authors の verdict を
                    # fan にすれば pending 整理ジョブが回収する (bot なら item を消す)
                    stats["pending"] += 1
                    pending["items"].append({
                        "name": cand["name"], "reason": "author_uncertain",
                        "hint": f"既存スポット「{existing['name']}」への出典追記",
                        "post": post_url, "author": cand["_post"]["author"], "date": TODAY})
                    continue
                existing["sources"].append({
                    "type": "x", "url": post_url, "author": cand["_post"]["author"],
                    "date": post_date, "quote": cand.get("quote", "")})
                stats["source_added"].append(existing["name"])
                desc_refresh[existing["slug"]] = existing
            else:
                new_candidates.append(cand)
        ledger["processed_posts"][pid] = {"date": TODAY, "result": "processed",
                                          "author": post.get("author_name", "")}

    cap = int(os.environ.get("DAILY_CAP") or pipeline_cfg["gates"]["daily_new_spot_cap"])
    if len(new_candidates) > cap:
        github_issue(
            f"[auto-ingest] サーキットブレーカー発動: 新規候補 {len(new_candidates)} 件 > {cap}",
            "検索クエリが他のムーブメントと衝突している可能性があります。"
            f"pipeline.json の x_query を確認してください。\n\n候補例:\n" +
            "\n".join(f"- {c['name']}" for c in new_candidates[:30]))
        log("circuit breaker: abort without commit")
        return

    known_place_ids = {s.get("place_id"): s for s in spots if s.get("place_id")}
    slugs = {s["slug"] for s in spots}

    # Places検索と同一性判定 (外部API) は候補間で独立なので並列に先行実行し、
    # ゲート・重複排除・登録は順序依存 (place_id の先勝ち等) のため逐次で適用する。
    # 同名候補 (複数ポストが同じ店を紹介) は正規化名で代表1件だけ照会し結果を共有する
    def lookup(cand):
        try:
            places = places_search(cand["name"], cand.get("hint"), bbox)
        except requests.RequestException as e:
            return ("places_error", str(e))
        return ("ok", places, judge_candidate(cand, places, areas, style_examples))

    reps = {}
    for cand in new_candidates:
        if not cand["_post"]["author_uncertain"]:  # pending 直行分は検索不要
            reps.setdefault(normalize(cand["name"]), cand)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rep_results = dict(zip(reps, pool.map(lookup, reps.values())))

    for cand in new_candidates:
        res = rep_results.get(normalize(cand["name"]))
        meta = cand["_post"]
        def hold(reason):
            stats["pending"] += 1
            pending["items"].append({"name": cand["name"], "reason": reason,
                                     "hint": cand.get("hint", ""), "post": meta["url"],
                                     "author": meta["author"], "date": TODAY})
        if meta["author_uncertain"]:
            hold("author_uncertain")
            continue
        if res[0] == "places_error":
            hold(f"places_error: {res[1]}")
            continue
        _, places, judged = res
        idx = judged.get("place_index", 0)
        place = places[idx] if places and 0 <= idx < len(places) else {}
        reason = gate(judged, place, bbox) if place else "places_no_match"
        if reason:
            hold(reason)
            continue
        place_id = place["id"]
        if place_id in known_place_ids:  # 名寄せ漏れ: 同一施設に出典追記
            ex_spot = known_place_ids[place_id]
            if not has_source(ex_spot["sources"], meta["url"]):
                ex_spot["sources"].append({"type": "x", "url": meta["url"],
                                           "author": meta["author"], "date": meta["date"],
                                           "quote": cand.get("quote", "")})
                stats["source_added"].append(ex_spot["name"])
                desc_refresh[ex_spot["slug"]] = ex_spot
            continue
        area_id = judged.get("area_id", "new")
        if area_id == "new" or area_id not in {a["id"] for a in areas}:
            new_name = clean_area_name(judged.get("new_area_name"))
            # 判定は並列実行のため直前の候補が新設したエリアを知らない。同名なら再利用する
            hit = next((a for a in areas if a["name"] == new_name), None)
            if hit:
                area_id = hit["id"]
            else:
                area_id = re.sub(r"[^a-z0-9]", "", (judged.get("slug") or "area")) + "-area"
                areas.append({"id": area_id, "name": new_name,
                              "color": AREA_PALETTE[len(areas) % len(AREA_PALETTE)]})
        slug = re.sub(r"[^a-z0-9-]", "", (judged.get("slug") or "spot").lower()) or "spot"
        while slug in slugs:
            slug += "2"
        slugs.add(slug)
        lat, lng = place["location"]["latitude"], place["location"]["longitude"]
        spot = {
            "slug": slug, "name": place["displayName"]["text"], "area": area_id,
            "cat": judged.get("category", "観光"), "lat": round(lat, 5),
            "lng": round(lng, 5), "approx": False,
            "desc": judged.get("desc", cand.get("quote", "")),
            "address": place.get("formattedAddress", ""),
            "place_id": place_id, "added": TODAY,
            "sources": [{"type": "x", "url": meta["url"], "author": meta["author"],
                         "date": meta["date"], "quote": cand.get("quote", "")}]}
        hours = place.get("regularOpeningHours", {}).get("weekdayDescriptions")
        if hours:
            # 週7日ぶんをそのまま持つ。先頭2日だけ残すと Places の並び (月始まり) の
            # せいで「月火だけの店」に見え、土日の予定を立てる用途で使えない
            spot["hours"] = list(hours)
        if official_url(place):
            spot["url"] = official_url(place)
        if not (PREF_STRICT["lat_min"] <= lat <= PREF_STRICT["lat_max"]
                and PREF_STRICT["lng_min"] <= lng <= PREF_STRICT["lng_max"]):
            spot["out_of_pref"] = True
        spots.append(spot)
        known_place_ids[place_id] = spot
        stats["added"].append(spot["name"])

    def _refresh(spot):
        try:
            refresh_desc(spot)
        except Exception as e:
            log("desc refresh skip:", spot["slug"], repr(e))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(_refresh, desc_refresh.values()))
    refresh_embed_flags(spots)

    save(workdir, "spots.json", spots_doc)
    save(workdir, "ledger.json", ledger)
    save(workdir, "pending.json", pending)

    v = subprocess.run([sys.executable, "validate.py"], cwd=workdir,
                       capture_output=True, encoding="utf-8", errors="replace")
    log(v.stdout.strip())
    if v.returncode != 0:
        github_issue("[auto-ingest] validate.py 失敗のため入稿中止", v.stdout + v.stderr)
        return

    msg = (f"auto-ingest {TODAY}: +{len(stats['added'])} spots"
           f"{' (' + ', '.join(stats['added'][:5]) + ')' if stats['added'] else ''}, "
           f"src+{len(stats['source_added'])}, pending {stats['pending']}, "
           f"posts {stats['posts']} (bot {stats['skipped_bot']} / offtopic {stats['skipped_offtopic']}, "
           f"cont {stats['continuations']}, seeds {n_seeds})")
    log("summary:", msg)
    commit_if_changed(workdir, msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 失敗を Issue で可視化してから落とす (トレースバックは伏せ字にして自前で出す)
        fail("auto-ingest", e)
