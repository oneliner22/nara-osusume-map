FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# HTTP2Shell (CVE-2026-111497): libbrotli1 が入っている場合のみ、修正版未満ならビルドを止める
# (この Service は git/libcurl を使わないので libbrotli1 が無いこともある。無ければ無害)
RUN v="$(dpkg-query -W -f='${Version}' libbrotli1 2>/dev/null || true)"; \
    if [ -n "$v" ]; then dpkg --compare-versions "$v" ge 1.1.0-2+deb13u1 \
    || { echo "libbrotli1 $v is vulnerable to CVE-2026-111497" >&2; exit 1; }; fi
WORKDIR /app
COPY fetcher.requirements.txt .
# サプライチェーン対策 (7日ルール): 公開から7日未満のパッケージ版は使わない。
# 冷却期間を置くことで、公開直後に差し替えられた悪性リリースを掴まない
RUN pip install --no-cache-dir uv \
    && uv pip install --system --break-system-packages --no-cache \
       --exclude-newer "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)" \
       -r fetcher.requirements.txt
# 第三者応答の br 自動展開経路を塞ぐ最重要ガード: brotli/brotlicffi が入ったらビルド停止。
# 敵対的応答に実際に触れるのはこの Service なので、ここでこそ効く
RUN python -c "import importlib.util as u, sys; m=[n for n in ('brotli','brotlicffi') if u.find_spec(n)]; sys.exit(f'brotli decoder installed: {m}' if m else 0)"
COPY fetcher.py fetchers_core.py safe_url.py ./
# Cloud Run は $PORT を注入する。shell 形式で展開させる
CMD gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 16 --timeout 120 fetcher:app
