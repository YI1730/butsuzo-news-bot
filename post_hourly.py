"""毎時実行の自動投稿エンジン（X API v2 / Pay Per Use）。

現在時刻(JST)に応じて、設定に基づき訪問記・告知を投稿する。

- 訪問記: config/auto_post_config.json の visit_hours に現在「時」が含まれれば、
  archives.json からランダム1件（直近180日は重複回避）を投稿。
- 告知:   scheduled_posts.json の weekdays/time が現在の曜日・時に一致すれば投稿。
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


def main() -> int:
    parser = argparse.ArgumentParser(description="毎時自動投稿エンジン")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せず、現在時刻の予定を表示")
    parser.add_argument("--hour", type=int, default=None,
                        help="テスト用: 現在の『時』をこの値(0-23)に上書き")
    args = parser.parse_args()

    now = datetime.now(JST)
    cur_hour = args.hour if args.hour is not None else now.hour
    cur_wd = _WEEKDAY_CODES[now.weekday()]
    today = now.strftime("%Y-%m-%d")

    config = load_config()
    scheduled = load_scheduled()

    print(f"JST {now:%Y-%m-%d %H:%M} (hour={cur_hour}, {cur_wd})")
    print(f"訪問設定: enabled={config['visit_enabled']} hours={config['visit_hours']}")

    plan_visit = bool(config["visit_enabled"]) and (cur_hour in config["visit_hours"])
    due_promos = [
        s for s in scheduled
        if _weekday_matches(s, cur_wd) and (_hour_of(s.get("time")) == cur_hour)
    ]
    print(f"→ 訪問投稿予定: {plan_visit} / 告知該当: {len(due_promos)}件")

    # ── DRY RUN ─────────────────────────────────────────────
    if args.dry_run:
        print("=== DRY RUN（投稿しません） ===")
        if plan_visit:
            archives = pv.load_archives()
            history = pv.load_history()
            picks = pv.pick_candidates(archives, history, 1)
            if picks:
                print("[訪問]", picks[0].get("text", "")[:70])
        log = load_sched_log()
        for s in due_promos:
            dup = log.get(s.get("id", "")) == today
            print(f"[告知]{'（本日投稿済）' if dup else ''}", (s.get("text") or "")[:70])
        return 0

    # ── 実投稿 ──────────────────────────────────────────────
    session = None
    posted = 0

    # 訪問記
    if plan_visit:
        archives = pv.load_archives()
        history = pv.load_history()
        picks = pv.pick_candidates(archives, history, 1)
        if not picks:
            print("[訪問] 投稿可能な候補がありません")
        else:
            p = picks[0]
            text = (p.get("text") or "").strip()
            if text:
                if session is None:
                    session = pv.get_oauth_session()
                ok, msg = pv.post_tweet(session, text)
                print(f"[訪問] {msg}")
                if ok:
                    history[p["id"]] = now.isoformat()
                    pv.save_history(history)
                    posted += 1

    # 告知
    if due_promos:
        log = load_sched_log()
        for s in due_promos:
            sid = s.get("id", "")
            if log.get(sid) == today:
                print(f"[告知] 本日投稿済みのためスキップ: {sid}")
                continue
            text = (s.get("text") or "").strip()
            if not text:
                continue
            if session is None:
                session = pv.get_oauth_session()
            ok, msg = pv.post_tweet(session, text)
            print(f"[告知] {msg}")
            if ok:
                log[sid] = today
                save_sched_log(log)
                posted += 1

    print(f"投稿完了: {posted}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
