#!/usr/bin/env bash
# salmon-chan (central-bulwark-427114-j7) への初回デプロイ。冪等に書いてあるので再実行可。
# 前提: gcloud 認証済み / シークレット値は環境変数で渡す:
#   GITHUB_TOKEN_VALUE, XDEV_MCP_URL_VALUE (PLACES はスクリプト内で API キーを作成)
# 奈良版: Secret (github-token / xdev-mcp-url / places-api-key) は ishikawa-spots / ehime-spots と共用。
#   既に存在すれば SKIP_SECRETS=1 で upsert を飛ばせる
set -euo pipefail

PROJECT=central-bulwark-427114-j7
REGION=asia-northeast1
JOB=nara-spots-daily
SA_NAME=nara-spots-crj
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/nara-spots/daily:latest"

gcloud config set project "$PROJECT"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com \
  aiplatform.googleapis.com artifactregistry.googleapis.com \
  places.googleapis.com apikeys.googleapis.com

gcloud artifacts repositories describe nara-spots --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create nara-spots --repository-format=docker --location="$REGION"

gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="nara-spots daily ingest"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
  --role=roles/aiplatform.user --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
  --role=roles/secretmanager.secretAccessor --condition=None >/dev/null

upsert_secret() {  # upsert_secret NAME VALUE
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=-
  fi
}
if [ "${SKIP_SECRETS:-0}" != "1" ]; then
  upsert_secret github-token "$GITHUB_TOKEN_VALUE"
  upsert_secret xdev-mcp-url "$XDEV_MCP_URL_VALUE"
fi

if ! gcloud secrets describe places-api-key >/dev/null 2>&1; then
  KEY=$(gcloud services api-keys create --display-name="nara-spots-places" \
    --api-target=service=places.googleapis.com --format="value(response.keyString)")
  upsert_secret places-api-key "$KEY"
fi

gcloud builds submit pipeline --tag "$IMAGE"

gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --max-retries 1 --task-timeout 30m \
  --set-env-vars "GCP_PROJECT=${PROJECT},VERTEX_LOCATION=global,GITHUB_REPO=oneliner22/nara-osusume-map" \
  --set-secrets "GITHUB_TOKEN=github-token:latest,XDEV_MCP_URL=xdev-mcp-url:latest,PLACES_API_KEY=places-api-key:latest"

gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
  --member="serviceAccount:$SA" --role=roles/run.invoker >/dev/null

SCHED=nara-spots-daily-trigger
SCHED_URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "$SCHED" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHED" --location "$REGION" \
    --schedule "13 7 * * *" --time-zone "Asia/Tokyo" --uri "$SCHED_URI" \
    --http-method POST --oauth-service-account-email "$SA"
else
  gcloud scheduler jobs create http "$SCHED" --location "$REGION" \
    --schedule "13 7 * * *" --time-zone "Asia/Tokyo" --uri "$SCHED_URI" \
    --http-method POST --oauth-service-account-email "$SA"
fi

# ---- 日次 pending 整理ジョブ (毎日 8:23 JST、日次ジョブ完了後に走る。
# 日次ジョブは 7:13 開始 + task-timeout 30m x (1 + max-retries 1) なので 8:13 までに必ず終了している。
# 正時 (:00) は Vertex の共有枠が混み 429 が出やすいので、どちらも分をずらしている) ----
PJOB=nara-spots-pending
gcloud run jobs deploy "$PJOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --max-retries 1 --task-timeout 30m \
  --command python --args pending_resolver.py \
  --set-env-vars "GCP_PROJECT=${PROJECT},VERTEX_LOCATION=global,GITHUB_REPO=oneliner22/nara-osusume-map" \
  --set-secrets "GITHUB_TOKEN=github-token:latest,XDEV_MCP_URL=xdev-mcp-url:latest,PLACES_API_KEY=places-api-key:latest"

# Scheduler は SA の OAuth トークンでジョブを起動するので、日次ジョブと同じく
# invoker 権限が要る (これが無いと Scheduler は毎朝 403 PERMISSION_DENIED で空振りし、
# Cloud Run 側には実行が1件も残らない)
gcloud run jobs add-iam-policy-binding "$PJOB" --region "$REGION" \
  --member="serviceAccount:$SA" --role=roles/run.invoker >/dev/null

PSCHED=nara-spots-pending-trigger
PSCHED_URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${PJOB}:run"
if gcloud scheduler jobs describe "$PSCHED" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$PSCHED" --location "$REGION" \
    --schedule "23 8 * * *" --time-zone "Asia/Tokyo" --uri "$PSCHED_URI" \
    --http-method POST --oauth-service-account-email "$SA"
else
  gcloud scheduler jobs create http "$PSCHED" --location "$REGION" \
    --schedule "23 8 * * *" --time-zone "Asia/Tokyo" --uri "$PSCHED_URI" \
    --http-method POST --oauth-service-account-email "$SA"
fi

echo "done. manual run: gcloud run jobs execute $JOB --region $REGION --wait"
