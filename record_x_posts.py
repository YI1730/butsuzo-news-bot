"""@butsuzolink の投稿（ID・URL・日時・元記事）を data/x_posts.json に記録する。

ニュースの投稿は PWA の「にポスト」から運営者が X で送るため、送った時点ではこちらに投稿 ID が
渡らない。そこで毎時、前回より新しい自分の投稿だけを X から読み（新しい投稿の件数ぶんだけ
読み取り費用がかかる: 1 件 約 $0.005）、news.json の記事と結び付けて記録する。

記録するのは X で誰でも見られる自分の投稿の情報だけなので、公開リポジトリに置いてよい。
ブログ側（事業本部の web-editor）が、記事に投稿を埋め込むために読む。

data/x_posts.json: {"user", "since_id", "updated_at", "posts": [新しい順]}
  各 post: id, url, posted_at, kind(news|visit|other), article_url, news_id, title, text
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import post_visit_tweet as pv
from news_store import clean_news_title
from post_news import JST, load_news_data

OUT_FILE = Path(__file__).parent / "data" / "x_posts.json"
X_API = "https://api.twitter.com/2"
FIRST_RUN_DAYS = 60            # 初回は、この日数前までさかのぼる
MAX_READ_PER_RUN = 150         # 1 回の実行で読む投稿数の上限（費用の上限: 約 $0.75）
SKIP_HOSTS = ("x.com", "twitter.com", "pic.twitter.com")
POST_URL = "https://x.com/butsuzolink/status/{id}"


def load_state() -> dict:
    try:
        d = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("posts", [])
            return d
    except Exception:
        pass
    return {"user": "butsuzolink", "since_id": None, "posts": []}


def norm_url(url: str) -> str:
    """比較用に URL を正規化する（クエリ・末尾の / ・ www. を無視）。"""
    p = urlsplit(url or "")
    host = (p.hostname or "").removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}"


def article_url_of(tweet: dict) -> str:
    for u in tweet.get("entities", {}).get("urls", []):
        exp = u.get("unwound_url") or u.get("expanded_url") or ""
        host = (urlsplit(exp).hostname or "").removeprefix("www.")
        if exp and not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            return exp
    return ""


def build_index(items: list[dict]) -> tuple[dict, list[tuple[str, dict]]]:
    by_url, titles = {}, []
    for it in items:
        for u in (it.get("url"), (it.get("ai") or {}).get("official_url")):
            if u:
                by_url[norm_url(u)] = it
        t = clean_news_title(it.get("title", ""))
        if len(t) >= 12:
            titles.append((t, it))
    return by_url, titles


def classify(tweet: dict, by_url: dict, titles: list) -> dict:
    text = tweet.get("text", "")
    art = article_url_of(tweet)
    item = by_url.get(norm_url(art)) if art else None
    if item is None:
        flat = re.sub(r"\s+", "", text)
        for t, it in titles:
            if re.sub(r"\s+", "", t)[:40] in flat:
                item = it
                break
    if text.startswith("【仏ログ】"):
        kind = "visit"
    elif item is not None or "【仏像速報】" in text:
        kind = "news"
    else:
        kind = "other"
    return {
        "id": tweet["id"],
        "url": POST_URL.format(id=tweet["id"]),
        "posted_at": (datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
                      .astimezone(JST).isoformat(timespec="seconds")),
        "kind": kind,
        "article_url": (item or {}).get("url") if item and not art else art,
        "news_id": (item or {}).get("id", ""),
        "title": (item or {}).get("title", ""),
        "text": text,
    }


def fetch_new(session, user_id: str, since_id: str | None, end_time: str | None = None) -> list[dict]:
    params = {
        "max_results": 100,
        "exclude": "replies,retweets",
        "tweet.fields": "created_at,entities",
    }
    if end_time:  # 過去分の取得（一度だけ）: この時刻より前を、FIRST_RUN_DAYS 日前までさかのぼる
        params["end_time"] = end_time
        start = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
        params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif since_id:
        params["since_id"] = since_id
    else:
        start = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
        params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    out, token = [], None
    while len(out) < MAX_READ_PER_RUN:
        if token:
            params["pagination_token"] = token
        r = session.get(f"{X_API}/users/{user_id}/tweets", params=params, timeout=30)
        if r.status_code != 200:
            print(f"::warning::自分の投稿の取得に失敗 HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break
        body = r.json()
        out += body.get("data", [])
        token = body.get("meta", {}).get("next_token")
        if not token:
            break
    return out[:MAX_READ_PER_RUN]


def main() -> int:
    state = load_state()
    session = pv.get_oauth_session()
    if not state.get("user_id"):
        r = session.get(f"{X_API}/users/me", timeout=20)
        if r.status_code != 200:
            print(f"::warning::ユーザー情報の取得に失敗 HTTP {r.status_code}", file=sys.stderr)
            return 0
        state["user_id"] = r.json()["data"]["id"]
        state["user"] = r.json()["data"].get("username", "butsuzolink")
    backfill = os.environ.get("BACKFILL_OLDER") == "1" and state["posts"]
    if backfill:
        oldest = min(state["posts"], key=lambda p: p["posted_at"])["posted_at"]
        end = datetime.fromisoformat(oldest).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tweets = fetch_new(session, state["user_id"], None, end_time=end)
    else:
        tweets = fetch_new(session, state["user_id"], state.get("since_id"))
    if not tweets:
        print("::notice::自分の投稿の記録: 新しい投稿なし")
        return 0
    by_url, titles = build_index(load_news_data()["items"])
    known = {p["id"] for p in state["posts"]}
    new = [classify(t, by_url, titles) for t in tweets if t["id"] not in known]
    state["posts"] = sorted(new + state["posts"], key=lambda p: p["id"], reverse=True)
    if not backfill:
        state["since_id"] = max(tweets, key=lambda t: int(t["id"]))["id"]
    state["updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    OUT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    matched = sum(1 for p in new if p["news_id"])
    print(f"::notice::自分の投稿の記録 新規{len(new)}件（記事と結び付いた{matched}件）"
          f"・X API 約${len(tweets) * 0.005:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
