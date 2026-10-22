"""@butsuzolink の直近の投稿と反応数（いいね・リポスト・表示回数など）を書き出す（分析用・手動実行）。

own_posts_export.yml から手動で実行する。結果は非公開リポジトリの own_posts.json に保存する。
読み取り件数は MAX_POSTS まで（X API の読み取り費用の上限を守るため）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import post_visit_tweet as pv  # noqa: E402
from post_news import JST  # noqa: E402

MAX_POSTS = int(os.environ.get("MAX_POSTS", "200"))
OUT = Path(os.environ.get("PRIVATE_DIR", "private")) / "own_posts.json"
X_API = "https://api.twitter.com/2"


def main() -> int:
    s = pv.get_oauth_session()
    me = s.get(f"{X_API}/users/me", timeout=20).json()["data"]
    posts, token = [], None
    while len(posts) < MAX_POSTS:
        params = {
            "max_results": min(100, MAX_POSTS - len(posts)),
            "exclude": "replies",
            "tweet.fields": "created_at,public_metrics,non_public_metrics,attachments,entities,referenced_tweets",
        }
        if token:
            params["pagination_token"] = token
        r = s.get(f"{X_API}/users/{me['id']}/tweets", params=params, timeout=30)
        if r.status_code != 200:
            print(f"::warning::取得失敗 HTTP {r.status_code}: {r.text[:200]}")
            break
        body = r.json()
        posts += body.get("data", [])
        token = body.get("meta", {}).get("next_token")
        if not token:
            break
    OUT.write_text(json.dumps({"exported_at": datetime.now(JST).isoformat(timespec="seconds"),
                               "posts": posts}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"::notice::自分の投稿 {len(posts)}件を書き出し（読み取り費用 約${len(posts) * 0.005:.2f} 以下）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
