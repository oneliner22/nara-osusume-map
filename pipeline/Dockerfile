FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# HTTP2Shell (CVE-2026-111497): git (libcurl) は br を要求して libbrotli で展開するので、修正版未満ならビルドを止める
RUN v="$(dpkg-query -W -f='${Version}' libbrotli1)" \
    && dpkg --compare-versions "$v" ge 1.1.0-2+deb13u1 \
    || { echo "libbrotli1 $v is vulnerable to CVE-2026-111497" >&2; exit 1; }
WORKDIR /app
COPY requirements.txt .
# サプライチェーン対策 (7日ルール): 公開から7日未満のパッケージ版は使わない。
# 直近に差し替えられた悪性リリース (公開直後に yank される類) を掴まないための冷却期間。
# uv の --exclude-newer に「7日前」を渡して自動適用する
RUN pip install --no-cache-dir uv \
    && uv pip install --system --break-system-packages --no-cache \
       --exclude-newer "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)" \
       -r requirements.txt
# brotli / brotlicffi が入ると requests・httpx が br レスポンスを自動展開し、
# safe_url や画像DLで第三者サイトの応答が展開経路になる。依存の推移で入ったらビルドを止める
RUN python -c "import importlib.util as u, sys; m = [n for n in ('brotli', 'brotlicffi') if u.find_spec(n)]; sys.exit(f'brotli decoder installed: {m}' if m else 0)"
# safe_url.py / fetchers_core.py は載せない: 敵対的サイトへの実取得は fetcher Service 側のみ。
# 本番は FETCHER_URL が必ず設定され fetch_client が remote 経路を使う (未設定だと fail-closed)。
COPY mcp_client.py daily_job.py pending_resolver.py refresh_hours.py models.json fetch_client.py urls_util.py ./
CMD ["python", "daily_job.py"]
