"""@butsuzolink のフォロワー数などを、1日1回 data/x_followers.csv に記録する。

X の画面ではフォロワー数の推移をさかのぼって見られないため、毎日の値を自分で残す。
毎時の実行のうち、日本時間でその日まだ記録していないときだけ X に問い合わせる（1日1回・読み取り1件）。
記録するのは、誰でも見られる自分のアカウントの数字だけ。

列: date(日本時間), followers, following, posts, listed
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import post_visit_tweet as pv

OUT = Path(__file__).parent / "data" / "x_followers.csv"
COLS = ["date", "followers", "following", "posts", "listed"]
JST = timezone(timedelta(hours=9))


def main() -> int:
    today = datetime.now(JST).date().isoformat()
    rows = []
    if OUT.exists():
        with OUT.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    if rows and rows[-1]["date"] == today:
        print("x_followers: 今日の分は記録済み")
        return 0
    session = pv.get_oauth_session()
    r = session.get("https://api.twitter.com/2/users/me", params={"user.fields": "public_metrics"}, timeout=20)
    if r.status_code != 200:
        print(f"::warning::フォロワー数の取得に失敗 HTTP {r.status_code}", file=sys.stderr)
        return 0
    m = r.json()["data"].get("public_metrics", {})
    rows.append({"date": today, "followers": m.get("followers_count", ""), "following": m.get("following_count", ""),
                 "posts": m.get("tweet_count", ""), "listed": m.get("listed_count", "")})
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"x_followers: {today} フォロワー {rows[-1]['followers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
