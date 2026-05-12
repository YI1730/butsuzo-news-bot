"""auto_post_runner.py
1 時間ごとに GitHub Actions から呼び出される自動投稿エンジン。

処理優先順位（要件4）:
  1. config/auto_post_config.json + data/scheduled_posts.json を読み、
     現在の曜日・時（"時"単位で評価）に一致する予約投稿があれば最優先で投稿し、終了。
  2. 予約投稿がなければ「最終ランダム投稿時刻 + 設定ペース」を確認。
     経過していれば data/archives.json + data/post_history.json を用い、
     直近 180 日に投稿した記事を除外してランダムに 1 件投稿。
     成功時は post_history.json と config.last_random_post_at を更新。
  3. 経過していなければ何もせず終了。

実投稿には tweepy（OAuth 1.0a の永久トークン）を使用する。
"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE   = ROOT / "config" / "auto_post_config.json"
ARCHIVES_FILE = ROOT / "data" / "archives.json"
SCHED_FILE    = ROOT / "data" / "scheduled_posts.json"
HISTORY_FILE  = ROOT / "data" / "post_history.json"

DEDUP_WINDOW_DAYS = 180
JST = timezone(timedelta(hours=9))

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------------------------------------------------------------------------
# I/O ヘルパ
# ---------------------------------------------------------------------------


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠ JSON 読み込み失敗 {path}: {e}", file=sys.stderr)
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# X 投稿
# ---------------------------------------------------------------------------


def _get_x_client():
    """tweepy.Client を生成。環境変数から永久トークンを読む。"""
    import tweepy

    keys = {
        "consumer_key":        os.environ.get("X_CONSUMER_KEY", ""),
        "consumer_secret":     os.environ.get("X_CONSUMER_SECRET", ""),
        "access_token":        os.environ.get("X_ACCESS_TOKEN", ""),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    }
    missing = [k for k, v in keys.items() if not v]
    if missing:
        raise RuntimeError(
            "X API キーが環境変数に未設定です: " + ", ".join(missing)
        )
    return tweepy.Client(**keys)


def _post_tweet(text: str) -> dict:
    client = _get_x_client()
    resp = client.create_tweet(text=text)
    tweet_id = resp.data.get("id") if hasattr(resp, "data") and resp.data else None
    return {
        "id": str(tweet_id) if tweet_id else "",
        "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
    }


# ---------------------------------------------------------------------------
# 予約投稿判定
# ---------------------------------------------------------------------------


def _find_due_scheduled(
    scheduled: list[dict], now: datetime
) -> dict | None:
    """現在の「曜日 + 時」に一致する予約投稿があれば最初の1件を返す。

    時刻の比較は **時単位** で行う（GitHub Actions の最小実行間隔が1時間のため）。
    weekday は ``"mon"`` … ``"sun"`` または ``"*"``（毎日）。
    """
    if not scheduled:
        return None
    now_wd = WEEKDAY_KEYS[now.weekday()]
    now_hour = now.hour
    for item in scheduled:
        wd = item.get("weekday", "*")
        tm = item.get("time", "")
        if wd != "*" and wd != now_wd:
            continue
        try:
            sched_hour = int(tm.split(":")[0])
        except Exception:
            continue
        if sched_hour == now_hour:
            return item
    return None


# ---------------------------------------------------------------------------
# ランダム投稿（180日重複防止）
# ---------------------------------------------------------------------------


def _pick_random_eligible(
    archives: list[dict], history: dict[str, str], now: datetime,
) -> dict | None:
    cutoff = now - timedelta(days=DEDUP_WINDOW_DAYS)
    eligible: list[dict] = []
    for a in archives:
        aid = a.get("id")
        if not aid:
            continue
        last_iso = history.get(aid, "")
        if not last_iso:
            eligible.append(a)
            continue
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=JST)
        except Exception:
            eligible.append(a)
            continue
        if last_dt < cutoff:
            eligible.append(a)
    if not eligible:
        return None
    return random.choice(eligible)


def _due_for_random_post(
    config: dict, now: datetime
) -> bool:
    """設定されたペース以上の時間が経過していれば True。"""
    interval_h = int(config.get("interval_hours", 4))
    last_iso = config.get("last_random_post_at", "")
    if not last_iso:
        return True  # 一度も投稿したことが無い → 投稿対象
    try:
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=JST)
    except Exception:
        return True
    return (now - last_dt) >= timedelta(hours=interval_h)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    now = datetime.now(JST)
    print(f"=== auto_post_runner 起動: {now.isoformat()} ===")

    archives: list[dict] = _read_json(ARCHIVES_FILE, [])
    scheduled: list[dict] = _read_json(SCHED_FILE, [])
    history: dict[str, str] = _read_json(HISTORY_FILE, {})
    config: dict = _read_json(CONFIG_FILE, {"interval_hours": 4, "last_random_post_at": ""})

    print(
        f"アーカイブ: {len(archives)} 件 / 予約: {len(scheduled)} 件 / "
        f"配信ペース: {config.get('interval_hours', 4)} 時間"
    )

    # ── ① 予約投稿チェック ─────────────────────────────────
    due = _find_due_scheduled(scheduled, now)
    if due is not None:
        print(f"予約投稿に該当: weekday={due.get('weekday')} time={due.get('time')}")
        try:
            result = _post_tweet(due["text"])
        except Exception as e:
            print(f"❌ 予約投稿失敗: {e}", file=sys.stderr)
            return 1
        print(f"✅ 予約投稿成功: {result.get('url')}")
        # 予約投稿は履歴に「scheduled:<id>:<iso>」を残す（重複防止カウントには影響させない）
        history[f"scheduled:{due.get('id', '')}"] = now.isoformat()
        _write_json(HISTORY_FILE, history)
        return 0

    # ── ② ランダム投稿時刻判定 ─────────────────────────────
    if not _due_for_random_post(config, now):
        last = config.get("last_random_post_at", "—")
        interval = config.get("interval_hours", 4)
        print(f"⏳ まだランダム投稿時刻ではありません（前回={last} / ペース={interval}h）")
        return 0

    if not archives:
        print("⚠ アーカイブが空のためランダム投稿スキップ")
        return 0

    chosen = _pick_random_eligible(archives, history, now)
    if chosen is None:
        print(f"⚠ 抽選できる候補なし（全件が直近 {DEDUP_WINDOW_DAYS} 日内に投稿済み）")
        return 0

    print(f"🎲 抽選: id={chosen.get('id')} / text={chosen.get('text', '')[:30]}...")
    try:
        result = _post_tweet(chosen["text"])
    except Exception as e:
        print(f"❌ ランダム投稿失敗: {e}", file=sys.stderr)
        return 1

    print(f"✅ ランダム投稿成功: {result.get('url')}")
    history[chosen["id"]] = now.isoformat()
    _write_json(HISTORY_FILE, history)

    config["last_random_post_at"] = now.isoformat()
    _write_json(CONFIG_FILE, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
