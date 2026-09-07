"""毎時実行の自動投稿エンジン（X API v2 / Pay Per Use）。

現在時刻(JST)に応じて、設定に基づき訪問記・告知を投稿する。

GitHub Actions の cron は高負荷時に大幅に間引かれる（実測: 1日24回のはずが6回程度）。
そのため「実行時刻が指定時刻とぴったり一致」する方式では投稿を取りこぼす。
本エンジンは「指定時刻を過ぎたか」で判定し、CATCHUP_GRACE_HOURS 以内であれば
後続の実行が取りこぼしを埋める。

- 訪問記: config/auto_post_config.json の visit_hours のスロットを過ぎていて、
  そのスロット分が今日まだ未投稿なら archives.json からランダム1件を投稿
  （直近180日は重複回避）。投稿済み判定は post_history.json の時刻から行う。
- 告知:   scheduled_posts.json の weekdays が今日に該当し、time を過ぎていれば投稿。
          weekdays は複数曜日を指定できる配列（例 ["mon","wed","fri"]）。
          ["*"] または未指定は毎日。旧形式の単一 weekday フィールドにも対応。
          同日の二重投稿は data/scheduled_log.json で防止。

設定ファイル（PWAまたはStreamlitから編集）:
  config/auto_post_config.json
    {
      "visit_enabled": true,     # 訪問記の自動投稿ON/OFF
      "visit_hours": [8, 20]     # 訪問記を投稿するJSTの「時」(0-23)。件数=個数
    }

認証は post_visit_tweet.py と同じ環境変数（X_API_KEY / X_API_SECRET /
X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET）。

使い方:
    python post_hourly.py --dry-run            # 投稿せず、今の時刻の予定を表示
    python post_hourly.py --dry-run --hour 8   # 「8時」として予定を確認
    python post_hourly.py                       # 実投稿（cronから毎時実行）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import post_visit_tweet as pv
from post_visit_tweet import JST, BASE_DIR

CONFIG_FILE = BASE_DIR / "config" / "auto_post_config.json"
SCHED_FILE = BASE_DIR / "data" / "scheduled_posts.json"
SCHED_LOG_FILE = BASE_DIR / "data" / "scheduled_log.json"

_WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_DEFAULT_CONFIG = {"visit_enabled": True, "visit_hours": [8, 20]}

# GitHub Actions の cron は高負荷時に実行が間引かれるため（実測で 1日24回 → 6回程度）、
# 「ちょうどその時」に実行されないと投稿を取りこぼす。
# そこで指定時刻を過ぎた分は、この猶予時間内であれば後続の実行が代わりに投稿する。
CATCHUP_GRACE_HOURS = 6


def load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if "visit_enabled" in d:
                    cfg["visit_enabled"] = bool(d["visit_enabled"])
                if "visit_hours" in d and isinstance(d["visit_hours"], list):
                    cfg["visit_hours"] = d["visit_hours"]
        except Exception as e:
            print(f"⚠ 設定読み込み失敗、既定値を使用: {e}", file=sys.stderr)
    # visit_hours を 0-23 の整数集合に正規化
    hrs = set()
    for h in cfg.get("visit_hours", []):
        try:
            hi = int(h)
            if 0 <= hi < 24:
                hrs.add(hi)
        except Exception:
            continue
    cfg["visit_hours"] = sorted(hrs)
    return cfg


def load_scheduled() -> list[dict]:
    if not SCHED_FILE.exists():
        return []
    try:
        d = json.loads(SCHED_FILE.read_text(encoding="utf-8"))
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def load_sched_log() -> dict[str, str]:
    if not SCHED_LOG_FILE.exists():
        return {}
    try:
        d = json.loads(SCHED_LOG_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_sched_log(log: dict[str, str]) -> None:
    SCHED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHED_LOG_FILE.write_text(
        json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _hour_of(time_str: str) -> int | None:
    try:
        return int(str(time_str).split(":")[0])
    except Exception:
        return None


def _item_weekdays(item: dict) -> list[str]:
    """告知アイテムの曜日リストを返す（'*' は毎日）。

    新形式: weekdays（配列、例 ["mon","wed"] や ["*"]）
    旧形式: weekday（単一文字列 '*' または曜日コード）にもフォールバック対応。
    """
    wds = item.get("weekdays")
    if isinstance(wds, list) and wds:
        codes = [w for w in wds if isinstance(w, str) and (w in _WEEKDAY_CODES or w == "*")]
        if codes:
            return codes
    legacy = item.get("weekday")
    if not legacy or legacy == "*":
        return ["*"]
    return [legacy]


def _weekday_matches(item: dict, cur_wd: str) -> bool:
    wds = _item_weekdays(item)
    return "*" in wds or cur_wd in wds


def _grace_for(slot_hour: int, all_hours: list[int]) -> int:
    """スロットの猶予時間。次のスロットに食い込まないよう上限を調整する。"""
    later = [h for h in sorted(all_hours) if h > slot_hour]
    gap = (later[0] - slot_hour) if later else (24 - slot_hour)
    return max(1, min(CATCHUP_GRACE_HOURS, gap))


def due_visit_hour(config: dict, now: datetime, history: dict[str, str]) -> int | None:
    """今日まだ投稿していない訪問記スロットのうち、最も早いものを返す。

    「実行時刻がぴったり一致」ではなく「指定時刻を過ぎたか」で判定するため、
    cron が間引かれても後続の実行が取りこぼしを埋められる。
    投稿済みかどうかは post_history.json のタイムスタンプから判定するので、
    追加の状態ファイル（＝ワークフローの変更）は不要。
    """
    if not config.get("visit_enabled"):
        return None
    hours = config.get("visit_hours") or []
    today = now.date()
    # 今日すでに投稿した時刻の一覧
    posted_today: list[int] = []
    for iso in history.values():
        dt = _parse_jst(iso)
        if dt is not None and dt.date() == today:
            posted_today.append(dt.hour)
    for h in sorted(hours):
        grace = _grace_for(h, hours)
        # そのスロットの猶予枠内にすでに投稿があれば消化済みとみなす
        if any(h <= ph < h + grace for ph in posted_today):
            continue
        if h <= now.hour < h + grace:
            return h
    return None


def _parse_jst(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def promo_is_due(item: dict, now: datetime, cur_wd: str) -> bool:
    """告知が「今日の指定時刻を過ぎ、猶予時間内」であれば True。"""
    if not _weekday_matches(item, cur_wd):
        return False
    h = _hour_of(item.get("time"))
    if h is None:
        return False
    return h <= now.hour < h + CATCHUP_GRACE_HOURS


def main() -> int:
    parser = argparse.ArgumentParser(description="毎時自動投稿エンジン")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せず、現在時刻の予定を表示")
    parser.add_argument("--hour", type=int, default=None,
                        help="テスト用: 現在の『時』をこの値(0-23)に上書き")
    args = parser.parse_args()

    now = datetime.now(JST)
    if args.hour is not None:
        # テスト用に「時」を上書き（取りこぼし判定も上書き後の時刻で評価する）
        now = now.replace(hour=args.hour, minute=0, second=0, microsecond=0)
    cur_hour = now.hour
    cur_wd = _WEEKDAY_CODES[now.weekday()]
    today = now.strftime("%Y-%m-%d")

    config = load_config()
    scheduled = load_scheduled()
    history = pv.load_history()

    print(f"JST {now:%Y-%m-%d %H:%M} (hour={cur_hour}, {cur_wd})")
    print(f"訪問設定: enabled={config['visit_enabled']} hours={config['visit_hours']}"
          f" / 取りこぼし補完 {CATCHUP_GRACE_HOURS}時間以内")

    visit_slot = due_visit_hour(config, now, history)
    sched_log = load_sched_log()
    due_promos = [
        s for s in scheduled
        if promo_is_due(s, now, cur_wd) and sched_log.get(s.get("id", "")) != today
    ]
    if visit_slot is None:
        print("→ 訪問投稿予定: なし（時間外、または本日分は投稿済み）")
    else:
        late = cur_hour - visit_slot
        print(f"→ 訪問投稿予定: あり（{visit_slot}時のスロット"
              f"{'・' + str(late) + '時間遅れの取りこぼし補完' if late else ''}）")
    print(f"→ 告知該当: {len(due_promos)}件")

    # ── DRY RUN ─────────────────────────────────────────────
    if args.dry_run:
        print("=== DRY RUN（投稿しません） ===")
        if visit_slot is not None:
            picks = pv.pick_candidates(pv.load_archives(), history, 1)
            if picks:
                print("[訪問]", picks[0].get("text", "")[:70])
            else:
                print("[訪問] 投稿可能な候補がありません")
        for s in due_promos:
            upload = "画像アップロードあり" if (s.get("use_media_upload") and s.get("image_url")) else "テキストのみ"
            print(f"[告知]（{upload}）", (s.get("text") or "")[:70])
        return 0

    # ── 実投稿 ──────────────────────────────────────────────
    session = None
    posted = 0
    failed = 0

    # 訪問記
    if visit_slot is not None:
        picks = pv.pick_candidates(pv.load_archives(), history, 1)
        if not picks:
            print("[訪問] 投稿可能な候補がありません")
        else:
            item = picks[0]
            text = (item.get("text") or "").strip()
            if text:
                if session is None:
                    session = pv.get_oauth_session()
                ok, msg = pv.post_tweet(session, text)
                print(f"[訪問] {msg}")
                if ok:
                    history[item["id"]] = now.isoformat()
                    pv.save_history(history)
                    posted += 1
                else:
                    failed += 1

    # 告知
    for s in due_promos:
        sid = s.get("id", "")
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if session is None:
            session = pv.get_oauth_session()

        # use_media_upload=true のときのみ image_url を実際にアップロードして添付。
        # トークン消費を抑えるため、明示的に指定されたものだけアップロードする。
        media_id = None
        image_url = (s.get("image_url") or "").strip()
        if s.get("use_media_upload") and image_url:
            media_id, upload_msg = pv.upload_media_from_url(session, image_url)
            print(f"[告知] 画像{upload_msg}")
            if not media_id:
                print(f"[告知] 画像アップロード失敗のため投稿を見送り: {sid}")
                failed += 1
                continue

        ok, msg = pv.post_tweet(session, text, media_id=media_id)
        print(f"[告知] {msg}")
        if ok:
            sched_log[sid] = today
            save_sched_log(sched_log)
            posted += 1
        else:
            failed += 1

    print(f"投稿完了: {posted}件 / 失敗: {failed}件")
    # 失敗があれば異常終了させ、GitHub Actions 上で赤く見えるようにする。
    # （以前は常に 0 を返していたため、投稿失敗が成功として見えていた）
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
