"""「参加者の声」（PWA の 🗣 声タブ）のデータを非公開リポジトリの voices.json に集める。

参加者の声は、ツアー・オフ会の参加者の公開ポストを、代表が手でリツイートして紹介するための
ストック。参加者のハンドルを一覧にしたものなので、公開リポジトリ（GitHub Pages）には置かず、
非公開リポジトリ butsuzo-private の voices.json にだけ保存する（PWA は保存済みのトークンで読む）。
Actions のログも公開なので、ハンドル名や本文はログに出さない（件数だけ出す）。

供給元:
  - muse: 事業本部の週次収集（voices.json に追記する）
  - rt_history: @butsuzolink がリツイートした参加者のポスト。record_x_posts.py が自分の投稿を
    読むときにリツイートも一緒に読み（読み取りの共有）、寺ベル・オフ会などの語を含むものだけを
    RT_OUT_FILE に書く。それをこのスクリプトの merge が取り込む。PWA では「要確認」を付ける
    ※ 2026-11-07 時点で見送り（既定でオフ）。news_hourly.yml の手順はリポジトリ変数
      VOICES_RT_ENABLED=true のときだけ動き、record_x_posts.py もリツイートを読まない設定のまま。
      動かすときは、record_x_posts.py の読み取りで exclude からリツイートを外し、
      rt_candidates() / append_rt_out() を呼ぶようにする

使い方（PRIVATE_DIR に butsuzo-private を checkout 済みのとき）:
  python voices.py merge       record_x_posts.py が書いたリツイートの候補を取り込む
  python voices.py backfill    直近 1 年のリツイートを一度だけさかのぼって取り込む（VOICES_BACKFILL=1 のとき）

voices.json: {"updated", "backfilled_at", "items": [新しい順]}
  各 item: id, handle, name, date, tour, kind(高評価|初参加|当日の様子|仏像の感想), summary,
           status_id, search, source(muse|rt_history), needs_check
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRIVATE_DIR = Path(os.environ.get("PRIVATE_DIR", "private"))
OUT_FILE = PRIVATE_DIR / "voices.json"
# record_x_posts.py → merge の受け渡し（公開リポジトリの外。Actions では RUNNER_TEMP）
RT_OUT_FILE = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "rt_voices.json"

X_API = "https://api.twitter.com/2"
# 元ポストにこれらの語があるリツイートだけを、参加者の声の候補にする
VOICE_KEYWORDS = ("寺ベル", "オフ会", "仏像リンク", "ブラ参り", "butsuzolink", "仏像オフ")
KINDS = ("高評価", "初参加", "当日の様子", "仏像の感想")
BACKFILL_DAYS = 365
MAX_BACKFILL_READ = 1500       # 過去分の読み取り上限（費用の上限: 約 $7.5 ＋ 付属情報）
READ_COST = 0.005              # X API 読み取り 1 件（11/22 からは付属情報も 1 件ごと）

# --- X の投稿の読み取り（record_x_posts.py と共有） ----------------------------------

# リツイートの元ポスト（本文・投稿者）を付属情報として受け取る。付属情報も課金されるので、
# 元ポストの本文と投稿者名に必要な分だけにする
RT_EXPANSIONS = "referenced_tweets.id,referenced_tweets.id.author_id"
RT_TWEET_FIELDS = "created_at,entities,referenced_tweets,author_id"
RT_USER_FIELDS = "username,name"


def is_retweet(tweet: dict) -> bool:
    return any(r.get("type") == "retweeted" for r in tweet.get("referenced_tweets") or [])


def rt_candidates(tweets: list[dict], includes: dict) -> list[dict]:
    """自分の投稿のうちリツイートだけを取り出し、元ポストに寺ベル・オフ会などの語が
    あるものを候補（元ポストの id・本文・投稿者・日付）にする。"""
    by_id = {t["id"]: t for t in includes.get("tweets", [])}
    users = {u["id"]: u for u in includes.get("users", [])}
    out = []
    for t in tweets:
        if not is_retweet(t):
            continue
        ref = next(r["id"] for r in t["referenced_tweets"] if r.get("type") == "retweeted")
        orig = by_id.get(ref)
        if not orig or not any(k in orig.get("text", "") for k in VOICE_KEYWORDS):
            continue
        u = users.get(orig.get("author_id"), {})
        if u.get("username", "").lower() == "butsuzolink":
            continue
        out.append({"status_id": ref, "text": orig.get("text", ""),
                    "handle": u.get("username", ""), "name": u.get("name", ""),
                    "date": (orig.get("created_at") or t.get("created_at") or "")[:10]})
    return out


def append_rt_out(cands: list[dict]) -> None:
    """record_x_posts.py から呼ぶ。同じ実行の merge が読む。"""
    if not cands:
        return
    try:
        old = json.loads(RT_OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        old = []
    RT_OUT_FILE.write_text(json.dumps(old + cands, ensure_ascii=False), encoding="utf-8")


# --- voices.json ----------------------------------------------------------------------

def load_voices() -> dict:
    try:
        d = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            return d
    except Exception:
        pass
    return {"updated": "", "items": []}


def save_voices(d: dict) -> None:
    d["updated"] = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    d["items"].sort(key=lambda v: v.get("date", ""), reverse=True)
    OUT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


SUMMARY_SYSTEM = """仏像好きのコミュニティ「仏像リンク」が、ツアー・オフ会の参加者のポストを紹介するための
「要旨」を作ります。渡されたポストそれぞれについて、次を返してください。
- summary: 何について、どう感じたかを 30 字以内の要旨で。本人の文を長く引用しない（5 字を超える引用はしない）。
  「仏様」「癒やし」「最高」「パワースポット」「ご利益」などの語は使わず、具体的な言い方に言い換える
- kind: 高評価（ツアー・会そのものへの評価）／初参加（初めての参加の声）／当日の様子（行程・出来事の報告）／
  仏像の感想（特定の像への感想）のどれか 1 つ
- tour: 本文から分かるツアー・オフ会の名前（例: 寺ベルin茨城）。分からなければ空文字"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"status_id": {"type": "string"}, "summary": {"type": "string"},
                       "kind": {"type": "string", "enum": list(KINDS)}, "tour": {"type": "string"}},
        "required": ["status_id", "summary", "kind", "tour"], "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False,
}


def summarize(cands: list[dict]) -> dict[str, dict]:
    """候補の要旨・分類・ツアー名を AI で作る（軽いモデル）。失敗したら空の dict。"""
    if not cands or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    import anthropic

    from curate_news import SEARCH_MODEL, Usage, call_json
    client, usage = anthropic.Anthropic(), Usage()
    payload = [{"status_id": c["status_id"], "date": c["date"], "text": c["text"]} for c in cands]
    res = call_json(client, usage, SUMMARY_SYSTEM, json.dumps(payload, ensure_ascii=False),
                    SUMMARY_SCHEMA, effort="low", model=SEARCH_MODEL)
    print(f"参加者の声の要旨 {len(cands)}件・AI費用 約${usage.cost():.4f}")
    return {r["status_id"]: r for r in (res or {}).get("results", [])}


def merge(cands: list[dict]) -> int:
    d = load_voices()
    known = {v.get("status_id") for v in d["items"] if v.get("status_id")}
    new = []
    for c in cands:
        if c["status_id"] in known:
            continue
        known.add(c["status_id"])
        new.append(c)
    if not new:
        return 0
    ai = summarize(new)
    for c in new:
        a = ai.get(c["status_id"], {})
        d["items"].append({
            "id": "rt" + c["status_id"], "handle": c["handle"], "name": c["name"], "date": c["date"],
            "tour": a.get("tour", ""), "kind": a.get("kind", "当日の様子"),
            "summary": a.get("summary", ""),   # 空なら PWA で「要旨は未作成」と出る
            "status_id": c["status_id"], "search": "", "source": "rt_history", "needs_check": True,
        })
    save_voices(d)
    return len(new)


def backfill() -> int:
    """直近 1 年のリツイートを一度だけさかのぼる。済んだら backfilled_at を記録し、以後は何もしない。"""
    d = load_voices()
    if d.get("backfilled_at"):
        print("::notice::参加者の声: 過去分は取り込み済み（backfilled_at）")
        return 0
    import post_visit_tweet as pv
    session = pv.get_oauth_session()
    r = session.get(f"{X_API}/users/me", timeout=20)
    if r.status_code != 200:
        print(f"::warning::ユーザー情報の取得に失敗 HTTP {r.status_code}", file=sys.stderr)
        return 0
    uid = r.json()["data"]["id"]
    start = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"max_results": 100, "exclude": "replies", "start_time": start,
              "tweet.fields": RT_TWEET_FIELDS, "expansions": RT_EXPANSIONS, "user.fields": RT_USER_FIELDS}
    tweets, inc, token, n_inc = [], {"tweets": [], "users": []}, None, 0
    while len(tweets) < MAX_BACKFILL_READ:
        if token:
            params["pagination_token"] = token
        r = session.get(f"{X_API}/users/{uid}/tweets", params=params, timeout=30)
        if r.status_code != 200:
            print(f"::warning::過去分の取得に失敗 HTTP {r.status_code}", file=sys.stderr)
            break
        body = r.json()
        tweets += body.get("data", [])
        for k in ("tweets", "users"):
            got = body.get("includes", {}).get(k, [])
            inc[k] += got
            n_inc += len(got)
        token = body.get("meta", {}).get("next_token")
        if not token:
            break
    n_rt = sum(1 for t in tweets if is_retweet(t))
    added = merge(rt_candidates(tweets, inc))
    d = load_voices()
    d["backfilled_at"] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    save_voices(d)
    print(f"::notice::参加者の声の過去分（直近{BACKFILL_DAYS}日）: 投稿{len(tweets)}件を読み、うちリツイート{n_rt}件・"
          f"候補として追加{added}件。X API 約${len(tweets) * READ_COST:.2f}"
          f"（付属情報{n_inc}件は11/22以降なら別に約${n_inc * READ_COST:.2f}）")
    return added


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "merge"
    if not PRIVATE_DIR.is_dir():
        print("::warning::非公開リポジトリが無いため参加者の声を更新しません", file=sys.stderr)
        return 0
    if cmd == "backfill":
        if os.environ.get("VOICES_BACKFILL") == "1":
            backfill()
        return 0
    try:
        cands = json.loads(RT_OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        cands = []
    added = merge(cands)
    print(f"::notice::参加者の声: リツイートの候補 {len(cands)}件・新規 {added}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
