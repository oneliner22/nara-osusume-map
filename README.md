# 奈良おすすめMAP 〜YouTube旅Vlog & Xのファン投稿から〜

奈良県（奈良公園・ならまち・西ノ京・斑鳩・天理・明日香・吉野）のおすすめスポットを、エリア別に色分けした地図＋一覧にまとめた静的サイト。
スポットをクリックすると出典（YouTube紹介動画の埋め込み / Xポストへのリンク）がモーダルで見られる。

対象イベント: **ぽこぴーの回覧板 in 奈良**（2026年12月27日・なら100年会館 大ホール）。
地図には会場マーカー（🎤）を置いてある。

出典は2系統:

1. **YouTube旅Vlog**（初期データ、Gemini 動画解析 → 名寄せ → Places 裏取り）。
   うち **ぽんぽこちゃんねるの奈良回**（鹿せんべい・リアル桃鉄 京都奈良編・バンジー など）で登場したスポットは
   🍃 マーク付き（`spot.mark` / `videos[].mark`）
2. **Xの「#ぽこピーの回覧板」奈良公演 関連のおすすめ投稿**（日次自動収集、下記パイプライン。**2026-12-27 まで**）。
   2026-03-11（奈良公演の告知）以降の投稿は初回バックフィルで取り込み済み

公開URL: https://oneliner22.github.io/nara-osusume-map/

愛媛版 https://github.com/oneliner22/ehime-osusume-map をそのまま奈良向けに差し替えたもの。
サイト本体・パイプラインの構造は愛媛版（＝石川版）と同一。

## 構成

- `index.html` — アプリ本体。`data/*.json` を実行時に fetch して描画（ビルド工程なし）
- `data/spots.json` — **スポットデータの正本**（エリア/カテゴリ/座標/説明/出典）
- `data/videos.json` — YouTube出典のメタ情報（動画ID → タイトル/チャンネル/mark）
- `data/config.json` — タイトル・リード文・地図中心/ズーム・🍃 マークの注記
- `data/pipeline.json` — 自動収集パイプラインの設定（クエリ/bbox/受入ゲート/モデル/**更新終了日 until**）
- `data/ledger.json` — 処理済みXポストID・著者判定のキャッシュ（二重入稿防止。著者判定は愛媛版から引き継ぎ）
- `data/aliases.json` — 略称→正規スポット名
- `data/pending.json` — 受入ゲート不合格で保留になった候補（地図には出ない）
- `validate.py` — データ整合性チェック（`python validate.py`）
- `tools/build_yt_data.py` — 初期データ（YouTube由来）を組み立てた一回きりのスクリプト（エリア判定・🍃対象動画もここ）
- `tools/polish_yt_spots.py` — slug のローマ字化・紹介文の常体化

`spots.json` のスポットは `sources` 配列で出典を持つ:

```json
{"type": "youtube", "id": "PLKr7oD-Td8"}
{"type": "x", "url": "https://x.com/<handle>/status/<id>", "author": "<handle>", "date": "2026-09-01", "quote": "..."}
```

X出典のURLは必ず **正規形**（`https://x.com/<handle>/status/<id>`）で持つ。
`"embed": false` が付いた出典は、投稿者のアカウント設定（年齢制限）により埋め込みも未ログイン閲覧もできないポスト。

## エリア

| id | 名前 | 判定（tools/build_yt_data.py の area_of） |
|---|---|---|
| park | 奈良公園・東大寺・春日大社 | 奈良市中心部の東側（東経135.829以東） |
| naramachi | ならまち・猿沢池 | 北緯34.6803以南・東経135.822〜135.845 |
| city | 奈良駅周辺・きたまち | 奈良市中心部の西側（会場のなら100年会館もここ） |
| west | 西ノ京・斑鳩・生駒 | 東経135.79以西（北緯34.56以北） |
| east | 天理・桜井・宇陀・柳生 | 天理・桜井以東、奈良市東部山間 |
| asuka | 明日香・橿原・葛城 | 北緯34.42〜34.56・東経135.835以西 |
| yoshino | 吉野・奈良南部 | 北緯34.42以南 |
| other | 県外（大阪・京都・三重寄り） | 県外だがマージン付きbbox内 |

日次ジョブは Gemini が既存エリアから選び、どれにも属さなければ新エリアを提案する。

## 閲覧側の機能

愛媛版と同一（ブックマーク・🧭動線・出典/投稿者/近い順の絞り込み・新着スポット/ランダムおすすめのカルーセル・
「これはなに？」折りたたみ）。すべて `index.html` 内で完結し、`localStorage` 以外に状態を持たない。

## ローカルプレビュー

fetch を使うため file:// では動かない。リポジトリ直下で:

```
python -m http.server
# → http://localhost:8000/
```

編集後は `python validate.py` で整合性チェック。

## 自動収集パイプライン（Cloud Run Jobs、2026-12-27 まで）

日次で以下を実行し、合格スポットを `spots.json` に直接コミットする（人手レビューなし）:

```
Cloud Scheduler → Cloud Run Job (GCP: salmon-chan)
 0. pipeline.json の until (2026-12-27) を過ぎていたら何もせず終了
 1. xdev で X を検索 (回覧板 (奈良 OR 奈良公園 OR ならまち OR 奈良駅 OR 橿原 OR 吉野 OR 明日香 OR 天理 OR 生駒) -is:retweet)
 2. 添付画像をダウンロード（おすすめリストは画像内記載が主流）
 3. Gemini (flash lite) が本文+全画像からスポット候補を抽出（実記載チェックで捏造防止）
 3b. 適格投稿の「本人の続き投稿」(自分のリプ・自己引用RT) を同run内で収穫
 4. aliases + 既存スポットと照合。既存なら sources に言及を追記
 5. Google Places API で裏取り（実在/正規名称/住所/座標/営業時間/営業状況）
 6. 受入ゲート（コード強制）:
    - Places 一致（Gemini pro が同一性を判定）
    - business_status = OPERATIONAL
    - マージン付きbbox内 (lat 33.75-34.88 / lng 135.5-136.35)。奈良県外は out_of_pref
    - 著者がbotでない（fan / bot / uncertain。uncertain は pending 行き）
    - 日次300件のサーキットブレーカー（超過時は全停止+Issue起票）
 7. 不合格は pending.json へ。合格分を commit & push（GitHub Pages が自動配信）
```

公式サイトURL（Places の websiteUri）は `pipeline/safe_url.py`（スキル共通、`tools/safe_url.py` と同一）で実際に取得して確かめてから付ける:
取得失敗・4xx/5xx（失効ドメインは第三者に再取得されフィッシングに転用され得る）、別サイトへのリダイレクト、
パスワード入力欄、パーキング/ギャンブル/ログイン系の title のいずれかに当たれば URL を付けない（スポット自体は掲載）。
除外ドメインは `pipeline.json` の `url_blocked_domains`。初期データ分は 2026-10-17 に `python tools/url_check.py` + 目視で確認済み。

公式 (@ponpokoka) の「おすすめ教えて」投稿を検知するとシード登録し、そのリプ・引用RTも収穫する。

### 日次 pending 整理（毎日 8:23 JST、日次ジョブ完了後）

保留候補を Gemini のツールループ（Places再検索・出典ポスト再読）で精査して回収する。
掲載可否の最終判定（place_id の実在根拠・営業状況・bbox）はコード側で強制する。

実装は `pipeline/` 配下（愛媛版と同じファイル構成）。Secret (github-token / xdev-mcp-url / places-api-key) は
石川版・愛媛版と共用なので `SKIP_SECRETS=1 ./pipeline/deploy.sh` で足りる。

初回バックフィル（告知日 2026-03-11 〜 デプロイ日の投稿を一括処理）:

```
INGEST_SINCE=2026-03-11T00:00:00Z DAILY_CAP=2000 python pipeline/daily_job.py
```

運用コマンド:

```
# 手動実行
gcloud run jobs execute nara-spots-daily --region asia-northeast1 --project central-bulwark-427114-j7 --wait
# ログ確認
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=nara-spots-daily' --project central-bulwark-427114-j7 --limit 50 --format 'value(textPayload)'
# 更新期間終了後の後始末 (until 以降ジョブは即終了するので急がなくてよい)
gcloud scheduler jobs delete nara-spots-daily-trigger --location asia-northeast1 --project central-bulwark-427114-j7
gcloud scheduler jobs delete nara-spots-pending-trigger --location asia-northeast1 --project central-bulwark-427114-j7
```

失敗・サーキットブレーカー・validate 不合格時は GitHub Issue が自動起票される。

## 出典・クレジット

スポット情報は以下の旅Vlogおよび X のファン投稿に基づく（著作権は各投稿者に帰属）:
ぽんぽこちゃんねる / 中尾明慶のきつねさーん / おとらべる! ~旅行&グルメ~ / しやごちゃんねる＠グルメ旅 / 奈良グルメちゃんねる飛鳥 / Maibaru Travel / 東海オンエア / みっきーCAFE&LIFE / 西園寺 / ゆめたび全国版【観るガイドブック】 / ayuco フッ軽ソロ活レポーター / おとな旅あるき旅【公式】 / 奈良とびっきり観光ガイド by　 佳山隆生【奈良観光】

座標は Google Places / 公式サイト等で裏取り。番地が確定できないものは「およその位置」と明記。
