"""訪問ツイート（data/archives.json）を X API v2 の無料枠で自動投稿する。

無料枠で使えるのは v2 の POST /2/tweets（テキスト投稿）のみ。
画像は「本文に含まれる pic.twitter.com/xxx（＝既存ツイートのメディアリンク）」を
X 側が展開して表示するため、media/upload（有料）は使わない。

重複防止:
    data/post_history.json（{id: 最終投稿ISO8601}）で
    直近 DEDUP_WINDOW_DAYS 日以内に投稿したものは再投稿しない。
    候補が枯渇した場合は「最も昔に投稿したもの」を選ぶ。

認証（OAuth 1.0a User Context / 環境変数）:
    X_API_KEY               … Consumer Key (API Key)
    X_API_SECRET            … Consumer Secret (API Key Secret)
    X_ACCESS_TOKEN          … Access Token
    X_ACCESS_TOKEN_SECRET   … Access Token Secret

使い方:
    python post_visit_tweet.py --dry-run        # 投稿せず候補を表示
    python post_visit_tweet.py                  # 1件投稿
    python post_visit_tweet.py --count 2        # 2件投稿
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from requests_oauthlib import OAuth1Session
except ImportError:  # ローカルで未インストールでも --dry-run は動くように
    OAuth1Session = None  # type: ignore

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
ARCHIVES_FILE = BASE_DIR / "data" / "archives.json"
HISTORY_FILE = BASE_DIR / "data" / "post_history.json"

TWEETS_ENDPOINT = "https://api.twitter.com/2/tweets"

# 直近この日数以内に投稿した訪問記は再投稿しない
DEDUP_WINDOW_DAYS = 180

# 1ツイートの最大文字数（標準アカウント）。超過分はスキップ扱い。
MAX_TWEET_LENGTH = 280

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# データ読み書き
# ---------------------------------------------------------------------------


def load_archives() -> list[dict]:
    if not ARCHIVES_FILE.exists():
        print(f"訪問記ファイルがありません: {ARCHIVES_FILE}", file=sys.stderr)
        return []
    with ARCHIVES_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("items", [])
    return [x for x in items if isinstance(x, dict)]


def load_history() -> dict[str, str]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_history(history: dict[str, str]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 候補選定
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def is_within_window(last_iso: str, days: int) -> bool:
    """last_iso が現在から days 日以内なら True。"""
    dt = _parse_iso(last_iso)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return (datetime.now(JST) - dt) < timedelta(days=days)


def eligible_pool(archives: list[dict], history: dict[str, str]) -> list[dict]:
    """テキストが有効かつ直近ウィンドウ外の訪問記を返す。"""
    pool = []
    for a in archives:
        aid = a.get("id", "")
        text = (a.get("text") or "").strip()
        if not aid or not text:
            continue
        if len(text) > MAX_TWEET_LENGTH:
            # 長すぎるものは自動投稿では扱わない（意図しない切り詰め防止）
            continue
        if is_within_window(history.get(aid, ""), DEDUP_WINDOW_DAYS):
            continue
        pool.append(a)
    return pool


def pick_candidates(archives: list[dict], history: dict[str, str],
                    count: int) -> list[dict]:
    """count 件の投稿候補を選ぶ。

    通常は「直近ウィンドウ外」からランダム。枯渇時は
    「最後に投稿したのが最も昔（未投稿含む）」の順にフォールバック。
    """
    valid = [
        a for a in archives
        if a.get("id") and (a.get("text") or "").strip()
        and len(a["text"].strip()) <= MAX_TWEET_LENGTH
    ]
    if not valid:
        return []

    pool = eligible_pool(archives, history)
    picks: list[dict] = []

    if pool:
        random.shuffle(pool)
        picks.extend(pool[:count])

    if len(picks) < count:
        # フォールバック: 最後に投稿したのが最も昔のものから補充
        picked_ids = {p["id"] for p in picks}
        remaining = [a for a in valid if a["id"] not in picked_ids]

        def last_posted_key(a: dict):
            iso = history.get(a["id"], "")
            dt = _parse_iso(iso)
            # 未投稿は最優先（最も昔扱い）
            return dt or datetime.min.replace(tzinfo=JST)

        remaining.sort(key=last_posted_key)
        picks.extend(remaining[: count - len(picks)])

    return picks[:count]


# ---------------------------------------------------------------------------
# 投稿
# ---------------------------------------------------------------------------


def get_oauth_session() -> "OAuth1Session":
    if OAuth1Session is None:
        raise RuntimeError(
            "requests-oauthlib が未インストールです。"
            "`pip install requests-oauthlib` を実行してください。"
        )
    key = os.environ.get("X_API_KEY", "").strip()
    secret = os.environ.get("X_API_SECRET", "").strip()
    token = os.environ.get("X_ACCESS_TOKEN", "").strip()
    token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
    missing = [
        name for name, val in (
            ("X_API_KEY", key), ("X_API_SECRET", secret),
            ("X_ACCESS_TOKEN", token), ("X_ACCESS_TOKEN_SECRET", token_secret),
        ) if not val
    ]
    if missing:
        raise RuntimeError(
            "認証情報が不足しています（環境変数）: " + ", ".join(missing)
        )
    return OAuth1Session(
        client_key=key,
        client_secret=secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )


def post_tweet(session: "OAuth1Session", text: str) -> tuple[bool, str]:
    """テキストを X に投稿する。戻り値 (成功?, メッセージ)。"""
    try:
        resp = session.post(TWEETS_ENDPOINT, json={"text": text}, timeout=20)
    except Exception as e:
        return False, f"リクエスト例外: {e}"

    if resp.status_code in (200, 201):
        try:
            tid = resp.json().get("data", {}).get("id", "")
        except Exception:
            tid = ""
        return True, f"投稿成功 (tweet id: {tid})"

    # エラー時の分かりやすいメッセージ
    body = resp.text[:400]
    hint = ""
    if resp.status_code == 401:
        hint = " → 認証情報（4つのキー）が誤っている可能性。"
    elif resp.status_code == 402:
        hint = (" → 無料枠で使えない機能を叩いています。"
                "v2 の POST /2/tweets を使い、v1.1 やメディアは使わないこと。")
    elif resp.status_code == 403:
        hint = (" → アプリ権限が Read only の可能性。"
                "Developer Portal で User authentication を Read and Write に。")
    elif resp.status_code == 429:
        hint = " → レート制限（無料枠は月500件・24時間17件など）。時間を空けて再試行。"
    return False, f"投稿失敗 HTTP {resp.status_code}{hint}\n{body}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="訪問ツイート自動投稿")
    parser.add_argument("--count", type=int, default=1,
                        help="投稿件数（既定: 1）")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せず候補のみ表示")
    args = parser.parse_args()

    archives = load_archives()
    if not archives:
        print("訪問記が0件のため終了", file=sys.stderr)
        return 1

    history = load_history()
    picks = pick_candidates(archives, history, max(1, args.count))
    if not picks:
        print("投稿可能な候補がありません", file=sys.stderr)
        return 1

    eligible_n = len(eligible_pool(archives, history))
    print(f"訪問記 全{len(archives)}件 / 直近{DEDUP_WINDOW_DAYS}日外の候補 {eligible_n}件")

    if args.dry_run:
        print("=== DRY RUN（投稿しません） ===")
        for i, p in enumerate(picks, 1):
            print(f"[{i}] id={p.get('id')}")
            print(p.get("text", ""))
            print("-" * 40)
        return 0

    # 実投稿
    session = get_oauth_session()
    posted = 0
    now_iso = datetime.now(JST).isoformat()
    for i, p in enumerate(picks, 1):
        aid = p.get("id", "")
        text = (p.get("text") or "").strip()
        print(f"[{i}/{len(picks)}] 投稿中 id={aid} : {text[:40]}…")
        ok, msg = post_tweet(session, text)
        print("  " + msg)
        if ok:
            history[aid] = now_iso
            save_history(history)  # 1件ごとに保存（途中失敗でも記録を保全）
            posted += 1
        else:
            # 失敗したらそれ以上投稿しない（レート制限等の連鎖を避ける）
            print("  投稿を中断します", file=sys.stderr)
            break

    print(f"投稿完了: {posted}/{len(picks)} 件")
    return 0 if posted > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
