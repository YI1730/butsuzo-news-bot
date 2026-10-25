"""@butsuzolink の X リストの新着から、運営者が「いいね・リポストしたい投稿」を Claude で選ぶ。

news_hourly.yml から実行されるが、X API の読み取り費用を抑えるため、実際に読むのは
1 日 2 回（前回から MIN_INTERVAL_HOURS 以上たち、かつ RUN_HOURS の時間帯）だけ。

いいね・リポストは自動では行わない（X の自動化ルールで自動いいねは禁止）。PWA の返信タブに
カードとして出し、タップすると X が開いて運営者本人が押す。

対象のリストは GitHub Secret の X_LIST_STARTERS（本命）と X_LIST_FRIENDS（優先度低め）。
このリポジトリと Actions のログは公開されているので、
  - 取得した投稿と状態は非公開リポジトリ（PRIVATE_DIR に checkout 済み）にだけ保存する
  - ログには件数だけを出し、アカウント名・本文・投稿 ID は出さない
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import post_visit_tweet as pv
from curate_news import PWA_URL, Usage, call_json
from post_news import JST

PRIVATE_DIR = Path(os.environ.get("PRIVATE_DIR", "private"))
OUT_FILE = PRIVATE_DIR / "reactions.json"
X_API = "https://api.twitter.com/2"

MIN_INTERVAL_HOURS = 11        # 前回の読み取りからこの時間以上あける（＝1日2回）
RUN_HOURS = range(7, 22)       # この時間帯（JST）だけ読む
MAX_AGE_HOURS = 24             # これより古い投稿は出さない
# 1 回・1 日あたりの読み取り件数の上限（1 件 約$0.005）
GROUPS = {
    # key: (Secret 名, 1 回の件数, 1 日の上限)
    "A": ("X_LIST_STARTERS", 10, 20),
    "B": ("X_LIST_FRIENDS", 3, 6),
}
MAX_PICKS_PER_DAY = 3          # 1 日に提案する件数
MAX_PICKS_B_PER_DAY = 1        # そのうち B（優先度低め）のリストからの件数
KEEP_DAYS = 14
SEEN_MAX = 2000

SYSTEM = """あなたは仏像好きのコミュニティ「仏像リンク」（X: @butsuzolink）の運営補佐です。
仏像リンクは寺院を巡るツアー・オフ会（寺ベルなど）を開いています。
運営者が知り合いの投稿に、いいね・リポストで反応したいと考えています。ただし手当たり次第に反応すると
下心があるように見えるので、本当に反応する価値のある投稿だけを選びます。

各投稿は group が A（ツアーの常連さん。本命）か B（仏像つながりの知り合い。優先度は低め）です。
各投稿を次の rank に分けてください。

- rank 1: A の人が、仏像リンクのツアー・オフ会の感想や思い出を書いた投稿（最優先）
- rank 2: A の人が、自分で訪ねた寺・仏像・展覧会の写真や感想、考察など、中身のあるオリジナル投稿
- rank 3: B の人の、rank 2 と同じような中身のある仏像関連の投稿
- rank 0（出さない）: 日常の投稿、宣伝・告知だけのもの、ニュースや他人の投稿の転載、愚痴・批判・政治、
  中身の薄い短文、仏像と関係ないもの

action は rank 1〜3 のときだけ意味を持ちます。
- "repost": 仏像リンクのフォロワーにも見てほしい投稿（ツアーの感想、写真が良く情報として価値がある投稿）
- "like": それ以外（いいねで十分なもの）

reason は、運営者がカードを見て内容が分かる一言（日本語 30 字以内。例「室生寺の十一面観音の写真と感想」）。
人の属性（常連など）には触れないでください。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "rank": {"type": "integer", "enum": [0, 1, 2, 3]},
                    "action": {"type": "string", "enum": ["like", "repost"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rank", "action", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


# 返信案の書き方の版。REPLY_SYSTEM を変えたら上げると、未返信の提案の返信案を作り直す
#   2: 投稿の言い換え（オウム返し）をやめ、自分の視点を 1 つ足す
#   3: 反応の型を分け（共感・感想・ツッコミ・質問・お礼）、質問は 2 割まで、お礼はオフ会の話と分かるときだけ
REPLY_DRAFT_VERSION = 3

# 仏像リンクのオフ会（ツアー）についての投稿と判断してよい手がかり。これが無い投稿にはお礼を書かない
TOUR_KEYWORDS = ("寺ベル", "ブラ参り", "ぶら参り", "仏像リンク", "リンクツアー", "リンクオフ会", "butsuzolink", "オフ会")
THANKS_RE = re.compile(r"(参加|ご参加|ツアー|オフ会).{0,12}(ありがとう|感謝)|(ありがとう|感謝).{0,6}(参加|ツアー)")
BANNED_WORDS = ("仏様", "癒や", "癒さ", "感動", "圧倒", "最高")
REPLY_TYPES = ["共感", "感想", "ツッコミ", "質問", "お礼"]
MAX_QUESTION_RATIO = 0.2

REPLY_SYSTEM = """あなたは仏像好きのコミュニティ「仏像リンク」（X: @butsuzolink）の運営者です。
平安・鎌倉期の彫刻や地方仏に詳しく、各地の寺を実際に巡っています。
知り合いの X の投稿に、仏像リンクのアカウントから短い返信を書きます。

# 反応の型（投稿に合うものを 1 つ選び、type に入れる）
- 共感: 相手の気持ちや体験に「それ、ですよね！」と頷く。例:「あの石段、上りきったあとの本堂は格別ですよね！」
- 感想: 像・場所・写真の具体を 1 つ拾って、ひと言添える。細部（表情・衣文・光背・安置の仕方）に目を留める。
  運営者が確実に知っていることを一言足してよい。不確かな知識は書かない
- ツッコミ: 像や状況への親しみのある軽いツッコミ。像への愛が伝わるもの。ご住職や寺、信仰を
  からかう表現、像を貶める表現は使わない
- 質問: 本当に知りたいことがあるときだけ。短く 1 つ
- お礼: 下の「お礼のルール」に当てはまるときだけ

# 型の割合
- 質問は、同じ回に渡す投稿のうち 2 割まで（渡された件数が 5 件未満なら 1 件まで）。
  迷ったら質問ではなく共感か感想にする。「直近の反応の型」が渡されたら、それと同じ型が続かないようにする
- 同じ書き出し・同じ締めを続けない。冒頭に定型の相づち（「いいですね」「素敵ですね」）を置かない

# お礼のルール（重要）
- 投稿の本文に「寺ベル」「ブラ参り」「仏像リンク」「リンクツアー」「オフ会」などの言葉があり、
  仏像リンクのオフ会（ツアー）の話だと確信できるときだけ、参加へのお礼を短く添える
- 一般の旅行・個人の寺めぐり・展覧会・他の団体のツアーなど、仏像リンクのオフ会と確信できない投稿には、
  「ツアーへのご参加ありがとう」「参加ありがとうございました」などを絶対に書かない。迷ったら書かない

# 文体
- 丁寧語だが堅すぎない、運営者本人の言葉。全角 80 字以内。1〜2 文。短いほどよい
- 盛らない・煽らない。うれしい・驚いた気持ちは「！」で表してよい（1〜2 個まで）
- ハッシュタグ・絵文字は使わない
- 感情を「感動」「圧倒」「最高」「癒やし」の語で言わず、具体で伝える。「仏様」とは書かず「仏さま」か像の名前にする
- 投稿の内容を言い換えて返すだけ（オウム返し）にしない
- 投稿に書かれていない像の名前・寺・事実を断定して書かない（運営者が確実に知っていることだけ足してよい）
- 宣伝（ツアーの案内など）はしない"""

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": REPLY_TYPES},
                    "reply_draft": {"type": "string"},
                },
                "required": ["id", "type", "reply_draft"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def is_tour_post(text: str) -> bool:
    return any(k in text for k in TOUR_KEYWORDS)


def reply_problems(it: dict, draft: str) -> list[str]:
    """返信案がルールに反していないか。反していれば理由の一覧。"""
    out = []
    if THANKS_RE.search(draft) and not is_tour_post(it.get("text", "")):
        out.append("仏像リンクのオフ会と確信できない投稿に、ツアー参加のお礼を書いている")
    out += [f"「{w}」は使わない" for w in BANNED_WORDS if w in draft]
    return out


def write_reply_drafts(client, usage: Usage, items: list[dict], recent_types: list[str] | None = None) -> None:
    """提案した投稿に返信案を付ける（PWA で直して X の返信画面に入れる）。"""
    max_q = max(1, int(len(items) * MAX_QUESTION_RATIO)) if len(items) >= 5 else 1
    payload = {
        "質問にしてよい件数の上限": max_q,
        "直近の反応の型（新しい順）": (recent_types or [])[:8],
        "投稿": [{"id": it["id"], "text": it["text"][:600], "reason": it.get("reason", "")} for it in items],
    }
    res = call_json(client, usage, REPLY_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1),
                    REPLY_SCHEMA, effort="medium")
    by_id = {it["id"]: it for it in items}
    retry = []
    for r in (res or {}).get("results", []):
        it = by_id.get(r.get("id"))
        if not it:
            continue
        draft = r["reply_draft"].strip()
        probs = reply_problems(it, draft)
        if probs:
            retry.append((it, draft, probs))
            continue
        it["reply_draft"], it["reply_type"] = draft, r["type"]
        it["reply_draft_version"] = REPLY_DRAFT_VERSION
    # ルールに反した案は、理由を伝えて 1 回だけ書き直す。それでも反するなら、反した部分を落とす
    for it, bad, probs in retry:
        res2 = call_json(
            client, usage, REPLY_SYSTEM,
            json.dumps({"質問にしてよい件数の上限": 0, "直近の反応の型（新しい順）": [],
                        "投稿": [{"id": it["id"], "text": it["text"][:600], "reason": it.get("reason", "")}],
                        "前回の案": bad, "直す点": probs}, ensure_ascii=False, indent=1),
            REPLY_SCHEMA, effort="medium")
        new = ((res2 or {}).get("results") or [{}])[0]
        draft = (new.get("reply_draft") or "").strip()
        if not draft or reply_problems(it, draft):
            draft = "".join(sn for sn in re.split(r"(?<=[。！？])", draft or bad)
                            if sn and not reply_problems(it, sn)).strip()
        if draft:
            it["reply_draft"], it["reply_type"] = draft, new.get("type", "感想")
            it["reply_draft_version"] = REPLY_DRAFT_VERSION


def fill_reply_drafts(state: dict, usage: Usage) -> int:
    """返信案が無い提案（今回の分、または返信案の機能を入れる前の分）に返信案を付ける。"""
    need = [
        it for it in state["items"]
        if it.get("reply_draft_version", 0) < REPLY_DRAFT_VERSION and it.get("reply_status") != "replied"
    ][:MAX_PICKS_PER_DAY * 3]
    if not need or not os.environ.get("ANTHROPIC_API_KEY"):
        return 0
    import anthropic

    recent = [it.get("reply_type", "") for it in state["items"] if it.get("reply_type")]
    write_reply_drafts(anthropic.Anthropic(), usage, need, recent)
    return len(need)


def load_state() -> dict:
    try:
        d = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            d.setdefault("seen", [])
            d.setdefault("daily", {})
            d.setdefault("skipped", [])
            return d
    except Exception:
        pass
    return {"items": [], "seen": [], "daily": {}, "skipped": []}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(JST) - timedelta(days=KEEP_DAYS)).isoformat()
    state["items"] = [it for it in state["items"] if it.get("picked_at", "") >= cutoff]
    # AI が見送った投稿（PWA で確認できるように、直近 3 日・最大 30 件だけ残す）
    skip_cutoff = (datetime.now(JST) - timedelta(days=3)).isoformat()
    state["skipped"] = [it for it in state["skipped"] if it.get("judged_at", "") >= skip_cutoff][:30]
    state["seen"] = state["seen"][-SEEN_MAX:]
    today = datetime.now(JST).strftime("%Y-%m-%d")
    state["daily"] = {k: v for k, v in state["daily"].items() if k >= today}  # 今日の分だけ残す
    state["last_updated"] = datetime.now(JST).isoformat(timespec="seconds")
    OUT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def should_run(state: dict, now: datetime) -> bool:
    if os.environ.get("FORCE_REACTIONS") == "1":
        return True
    if now.hour not in RUN_HOURS:
        return False
    try:
        last = datetime.fromisoformat(state.get("last_read_at", ""))
    except ValueError:
        return True
    return now - last >= timedelta(hours=MIN_INTERVAL_HOURS)


def fetch_list(session, list_id: str, n: int) -> list[dict]:
    """リストの新着 n 件。失敗時は空（ログには内容を出さない）。"""
    params = {
        "max_results": n,
        "tweet.fields": "created_at,author_id,referenced_tweets,attachments",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "username,name,profile_image_url",
        "media.fields": "url,preview_image_url,type",
    }
    try:
        r = session.get(f"{X_API}/lists/{list_id}/tweets", params=params, timeout=20)
    except Exception as e:
        print(f"::warning::リストの取得に失敗（接続: {type(e).__name__}）", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"::warning::リストの取得に失敗（HTTP {r.status_code}）", file=sys.stderr)
        return []
    body = r.json()
    users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
    media = {m["media_key"]: m for m in body.get("includes", {}).get("media", [])}
    out = []
    for t in body.get("data", []):
        refs = {ref.get("type") for ref in t.get("referenced_tweets", [])}
        u = users.get(t.get("author_id"), {})
        keys = t.get("attachments", {}).get("media_keys", [])
        image = next((media[k].get("url") or media[k].get("preview_image_url", "")
                      for k in keys if k in media), "")
        out.append({
            "id": t["id"],
            "text": t.get("text", ""),
            "created_at": t.get("created_at", ""),
            "author_id": t.get("author_id", ""),
            "username": u.get("username", ""),
            "name": u.get("name", ""),
            "icon": u.get("profile_image_url", ""),
            "image": image or "",
            "is_repost": "retweeted" in refs,
            "is_reply": "replied_to" in refs,
        })
    return out


def age_hours(created_at: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (now - dt).total_seconds() / 3600


def main() -> int:
    if not PRIVATE_DIR.is_dir():
        print("反応したい投稿: 非公開リポジトリが無いためスキップ")
        return 0
    if not any(os.environ.get(secret) for secret, _, _ in GROUPS.values()):
        print("反応したい投稿: リストが未設定のためスキップ")
        return 0
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if not should_run(state, now):
        # 読み取りはしないが、返信案の無い提案があれば作る（X API は使わない）
        if fill_reply_drafts(state, Usage()):
            save_state(state)
        print("反応したい投稿: 読み取りの時間ではないためスキップ")
        return 0

    daily = state["daily"].setdefault(today, {"reads": {}, "picks": 0, "picks_b": 0, "authors": []})
    session = pv.get_oauth_session()
    seen = set(state["seen"])
    fresh: list[dict] = []
    reads = 0
    for group, (secret, per_run, per_day) in GROUPS.items():
        list_id = os.environ.get(secret, "").strip()
        n = min(per_run, per_day - daily["reads"].get(group, 0))
        if not list_id or n < 1:
            continue
        posts = fetch_list(session, list_id, n)
        daily["reads"][group] = daily["reads"].get(group, 0) + len(posts)
        reads += len(posts)
        for p in posts:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            state["seen"].append(p["id"])
            if p["is_repost"] or p["is_reply"] or age_hours(p["created_at"], now) > MAX_AGE_HOURS:
                continue
            p["group"] = group
            fresh.append(p)
    state["last_read_at"] = now.isoformat(timespec="seconds")

    usage = Usage()
    picked = 0
    room = MAX_PICKS_PER_DAY - daily["picks"]
    if fresh and room > 0 and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        payload = [{"id": p["id"], "group": p["group"], "text": p["text"][:600],
                    "has_image": bool(p["image"])} for p in fresh]
        res = call_json(anthropic.Anthropic(), usage, SYSTEM,
                        json.dumps(payload, ensure_ascii=False, indent=1), SCHEMA, effort="low")
        by_id = {p["id"]: p for p in fresh}
        ranked = sorted(
            (r for r in (res or {}).get("results", []) if r.get("rank", 0) > 0 and r.get("id") in by_id),
            key=lambda r: (r["rank"], age_hours(by_id[r["id"]]["created_at"], now)),  # rank 順、同じなら新しい順
        )
        for r in (res or {}).get("results", []):
            p = by_id.get(r.get("id"))
            if p and r.get("rank", 0) == 0:
                state["skipped"].insert(0, {
                    "id": p["id"], "url": f"https://x.com/{p['username']}/status/{p['id']}",
                    "username": p["username"], "name": p["name"], "text": p["text"][:200],
                    "created_at": p["created_at"], "reason": r.get("reason", ""),
                    "judged_at": now.isoformat(timespec="seconds"),
                })
        for r in ranked:
            p = by_id[r["id"]]
            if picked >= room:
                break
            if p["author_id"] in daily["authors"]:
                continue  # 同じ人は 1 日 1 件まで
            if p["group"] == "B" and daily["picks_b"] >= MAX_PICKS_B_PER_DAY:
                continue
            state["items"].insert(0, {
                "id": p["id"],
                "url": f"https://x.com/{p['username']}/status/{p['id']}",
                "username": p["username"],
                "name": p["name"],
                "icon": p["icon"],
                "image": p["image"],
                "text": p["text"],
                "created_at": p["created_at"],
                "rank": r["rank"],
                "action": r["action"],
                "reason": r["reason"],
                "picked_at": now.isoformat(timespec="seconds"),
            })
            daily["authors"].append(p["author_id"])
            daily["picks"] += 1
            if p["group"] == "B":
                daily["picks_b"] += 1
            picked += 1

    fill_reply_drafts(state, usage)

    capped = all(daily["reads"].get(g, 0) >= per_day for g, (_, _, per_day) in GROUPS.items())
    state["last_run"] = {"at": now.isoformat(timespec="seconds"), "reads": reads,
                         "fresh": len(fresh), "picked": picked, "capped": capped and reads == 0}
    save_state(state)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and picked:
        try:
            requests.post("https://ntfy.sh/", json={
                "topic": topic,
                "title": f"👍 反応したい投稿 {picked}件",
                "message": "返信タブで確認できます",
                "click": PWA_URL + "#replies",
                "tags": ["thumbsup"],
            }, timeout=10)
        except Exception:
            pass
    print(f"::notice::反応したい投稿 読み取り{reads}件（約${reads * 0.005:.3f}）・"
          f"候補{len(fresh)}件・提案{picked}件・AI費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
