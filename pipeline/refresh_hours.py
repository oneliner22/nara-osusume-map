# -*- coding: utf-8 -*-
"""既存スポットの営業時間を週7日ぶんに入れ直す一回きりのジョブ。

以前は Places の weekdayDescriptions を先頭2日だけ残していたため、月曜始まりの
並びのせいで全店が「月火だけ営業」に見えていた。捨てた5日ぶんはデータに無いので
引き直すしかないが、place_id は保存済みなので Place Details を1件ずつ引くだけで
足りる（テキスト検索も判定モデルも通さない）。

日次ジョブと同じイメージで動かす:
  gcloud run jobs execute nara-spots-pending --region asia-northeast1 \
    --args refresh_hours.py --wait
DRY_RUN=1 で push しない（差分はログに出す）。
必要な環境変数: GITHUB_REPO, GITHUB_TOKEN, PLACES_API_KEY
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import requests  # noqa: E402
from daily_job import (clone_repo, commit_if_changed, github_issue,  # noqa: E402
                       load, log, redact, save)

API_KEY = os.environ["PLACES_API_KEY"]


def weekday_descriptions(place_id):
    r = requests.get(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={"X-Goog-Api-Key": API_KEY,
                 "X-Goog-FieldMask": "regularOpeningHours.weekdayDescriptions"},
        params={"languageCode": "ja"}, timeout=30)
    r.raise_for_status()
    return r.json().get("regularOpeningHours", {}).get("weekdayDescriptions")


def main():
    workdir = tempfile.mkdtemp(prefix="spots-hours-")
    clone_repo(workdir)
    doc = load(workdir, "spots.json")

    changed = skipped = failed = dropped = 0
    for s in doc["spots"]:
        if isinstance(s.get("hours"), list):   # すでに週7日ぶん
            skipped += 1
            continue
        if not s.get("place_id"):
            log(f"{s['name']}: place_id なし skip")
            skipped += 1
            continue
        try:
            hours = weekday_descriptions(s["place_id"])
        except Exception as e:
            log(f"{s['name']}: 取得失敗 {redact(repr(e))[:120]}")
            failed += 1
            continue
        if not hours:
            # 24時間営業でも weekdayDescriptions は返るので、
            # 空なら Places 側に営業時間の登録自体がない
            if s.pop("hours", None) is not None:
                log(f"{s['name']}: 営業時間の登録なし -> hours を落とす")
                dropped += 1
            else:
                log(f"{s['name']}: 営業時間の登録なし (元から hours なし)")
                skipped += 1
            continue
        log(f"{s['name']}: {len(hours)}日ぶん ({hours[0]} … {hours[-1]})")
        s["hours"] = list(hours)
        changed += 1

    log(f"summary: 更新 {changed} / skip {skipped} / 削除 {dropped} / 失敗 {failed}")
    if not changed and not dropped:
        log("変更なしのため終了")
        return

    save(workdir, "spots.json", doc)
    v = subprocess.run([sys.executable, "validate.py"], cwd=workdir,
                       capture_output=True, encoding="utf-8", errors="replace")
    log(v.stdout.strip())
    if v.returncode != 0:
        github_issue("[refresh-hours] validate.py 失敗のため中止", v.stdout + v.stderr)
        return

    commit_if_changed(
        workdir,
        f"営業時間を週7日ぶんに入れ直し: {changed}件更新"
        f"{f' / {dropped}件は登録なしで削除' if dropped else ''}"
        f"{f' / {failed}件は取得失敗' if failed else ''}")


if __name__ == "__main__":
    main()
