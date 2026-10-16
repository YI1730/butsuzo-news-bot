"""仏像リンク宛ての返信（メンション）を取得し、返信すべきものを Claude で選んで返信案を作る。

news_hourly.yml から毎時実行される。

1. X API で新着メンションを取得（前回の続きから。メンション取得は 1 件 $0.001）
2. Claude が「返信すべきか」「優先度」「分類」を判定し、返信すべきものには返信案を作る
3. 返信すべきものを ntfy でスマホに通知（NTFY_TOPIC 設定時のみ）

結果は data/replies.json に保存し、PWA の「💬返信」タブに表示する。
PWA で返信を確定すると data/reply_queue.json に積まれ、reply_send.py が送信する。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import post_visit_tweet as pv
from curate_news import PWA_URL, Usage, call_json, x_weighted_length
from post_news import JST

REPLIES_FILE = Path(__file__).parent / "data" / "replies.json"
X_API = "https://api.twitter.com/2"
FIRST_RUN_LOOKBACK_DAYS = 3   # 初回だけ遡って取得する日数
MAX_KEEP = 300                # replies.json に保持する件数
TRIAGE_BATCH_SIZE = 20
REPLY_MAX_CHARS = 120         # 返信案の上限（全角）

PRIORITIES = ["高", "中", "低"]
CATEGORIES = [
    "質問", "情報提供・訂正", "参加・申込の相談", "感想・共感",
    "批判・要注意", "告知・宣伝", "スパム・無関係", "その他",
]

TRIAGE_SYSTEM = """あなたは X アカウント「仏像リンク」の運営補佐です。仏像リンクは仏像好きのコミュニティで、
仏像ニュースの発信や、寺院を巡るオフ会（観仏ツアー）の運営をしています。
仏像リンク宛てに届いた返信・メンションの一覧を読み、運営者が返信すべきものを選んでください。

# 返信すべきもの（needs_reply: true）
- 質問（拝観方法、公開期間、場所、オフ会の参加方法や日程など）
- 投稿内容の誤りの指摘、追加の情報提供
- オフ会・イベントの参加や申込に関する相談、困りごと
- 体験や思いを丁寧に書いてくれた感想で、ひとこと返すと関係が深まりそうなもの

# 返信しなくてよいもの（needs_reply: false）
- 絵文字だけ、「いいですね」程度の短い反応（いいねで十分）
- 宣伝・勧誘、スパム、bot、仏像と無関係なもの
- 批判や攻撃的な内容で、返信すると揉めそうなもの（category を「批判・要注意」にし、
  reason で何に注意すべきかを書く）

# 優先度
- 高: 質問・申込・誤りの指摘など、返事を待っている人がいるもの
- 中: 返すと良い感想や情報提供
- 低: 返信不要のもの

# 返信案（needs_reply が true のときだけ。それ以外は空文字）
- 丁寧語だが堅すぎない、運営者本人の言葉。短く（全角 {max_chars} 字以内）
- 盛らない・煽らない。「ありがとうございます！！」のような過剰な感嘆は避ける
- ハッシュタグ、絵文字は使わない
- 答えが分からない質問や、確認が必要な事実は推測で答えず「確認してお返事しますね」のように書く

reason は判断理由を日本語 40 字以内で。"""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "needs_reply": {"type": "boolean"},
                    "priority": {"type": "string", "enum": PRIORITIES},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "reason": {"type": "string"},
                    "reply_draft": {"type": "string"},
                },
                "required": ["id", "needs_reply", "priority", "category", "reason", "reply_draft"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def load_state() -> dict:
    try:
        d = json.loads(REPLIES_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            return d
    except Exception:
        pass
    return {"user_id": None, "username": None, "since_id": None, "items": []}


def save_state(state: dict) -> None:
    state["items"] = state["items"][:MAX_KEEP]
    state["last_updated"] = datetime.now(JST).isoformat(timespec="seconds")
    REPLIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPLIES_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def x_get(session, path: str, params: dict | None = None) -> dict | None:
    try:
        r = session.get(f"{X_API}{path}", params=params, timeout=20)
    except Exception as e:
        print(f"::warning::X API 接続エラー: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"::warning::X API GET {path} 失敗 HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return None
    return r.json()


def fetch_mentions(session, state: dict) -> list[dict]:
    """前回以降の新着メンションを取得し、表示用の dict のリストで返す（古い順）。"""
    if not state.get("user_id"):
        me = x_get(session, "/users/me")
        if not me:
            return []
        state["user_id"] = me["data"]["id"]
        state["username"] = me["data"].get("username")
    params = {
        "max_results": 100,
        "tweet.fields": "created_at,conversation_id,author_id,referenced_tweets",
        "expansions": "author_id,referenced_tweets.id",
        "user.fields": "username,name",
    }
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    else:
        start = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
        params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    body = x_get(session, f"/users/{state['user_id']}/mentions", params)
    if body is None:
        return []
    meta = body.get("meta", {})
    if meta.get("newest_id"):
        state["since_id"] = meta["newest_id"]
    users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
    parents = {t["id"]: t for t in body.get("includes", {}).get("tweets", [])}

    out = []
    for t in body.get("data", []):
        if t.get("author_id") == state["user_id"]:
            continue  # 自分の投稿は除く
        parent = None
        for ref in t.get("referenced_tweets", []):
            if ref.get("type") == "replied_to":
                parent = parents.get(ref["id"])
        author = users.get(t.get("author_id"), {})
        out.append({
            "id": t["id"],
            "text": t.get("text", ""),
            "created_at": t.get("created_at", ""),
            "conversation_id": t.get("conversation_id", ""),
            "author_username": author.get("username", ""),
            "author_name": author.get("name", ""),
            "parent_text": (parent or {}).get("text", ""),
            "parent_is_ours": bool(parent) and parent.get("author_id") == state["user_id"],
            "status": "open",
            "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        })
    out.sort(key=lambda x: x["created_at"])
    return out


def triage(client, usage: Usage, items: list[dict]) -> None:
    system = TRIAGE_SYSTEM.replace("{max_chars}", str(REPLY_MAX_CHARS))
    for i in range(0, len(items), TRIAGE_BATCH_SIZE):
        batch = items[i:i + TRIAGE_BATCH_SIZE]
        payload = [
            {
                "id": it["id"],
                "author": f"@{it['author_username']}（{it['author_name']}）",
                "text": it["text"],
                "replying_to": (("仏像リンクの投稿: " if it["parent_is_ours"] else "他の投稿: ")
                                + it["parent_text"][:300]) if it["parent_text"] else "（直接のメンション）",
            }
            for it in batch
        ]
        data = call_json(client, usage, system,
                         json.dumps(payload, ensure_ascii=False, indent=1),
                         TRIAGE_SCHEMA, effort="medium")
        if not data:
            continue
        by_id = {it["id"]: it for it in batch}
        for r in data.get("results", []):
            it = by_id.get(r.get("id"))
            if not it:
                continue
            draft = r["reply_draft"].strip() if r["needs_reply"] else ""
            if draft and x_weighted_length(draft) > 280:
                draft = ""  # 長すぎる案は捨てる（PWA で手書き）
            it["ai"] = {
                "needs_reply": bool(r["needs_reply"]),
                "priority": r["priority"],
                "category": r["category"],
                "reason": r["reason"],
                "reply_draft": draft,
            }


def notify(topic: str, it: dict) -> None:
    ai = it["ai"]
    msg = f"@{it['author_username']}: {it['text']}"
    if ai.get("reply_draft"):
        msg += f"\n\n返信案: {ai['reply_draft']}"
    try:
        requests.post(
            "https://ntfy.sh/",
            json={
                "topic": topic,
                "title": f"💬 返信すべき返信（{ai['priority']}・{ai['category']}）",
                "message": msg,
                "click": PWA_URL + "#replies",
                "tags": ["speech_balloon"],
                "priority": 4 if ai["priority"] == "高" else 3,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"::warning::通知の送信に失敗: {e}", file=sys.stderr)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("::warning::ANTHROPIC_API_KEY が未設定のため返信の仕分けをスキップ", file=sys.stderr)
        return 0
    import anthropic

    state = load_state()
    session = pv.get_oauth_session()
    new = fetch_mentions(session, state)
    known = {it["id"] for it in state["items"]}
    new = [it for it in new if it["id"] not in known]
    print(f"新着の返信・メンション: {len(new)}件")

    # 新着を先頭に積み、AI 未判定のもの（前回 API が失敗した分を含む）をまとめて判定する
    state["items"] = sorted(new, key=lambda x: x["created_at"], reverse=True) + state["items"]
    pending = [it for it in state["items"] if "ai" not in it and it.get("status") == "open"][:60]
    usage = Usage()
    if pending:
        triage(anthropic.Anthropic(), usage, pending)
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        for it in pending:
            ai = it.get("ai", {})
            mark = "💬要返信" if ai.get("needs_reply") else "　不要　"
            print(f"{mark} [{ai.get('priority', '-')}/{ai.get('category', '-')}] "
                  f"@{it['author_username']}: {it['text'][:50]}")
            if topic and ai.get("needs_reply"):
                notify(topic, it)

    save_state(state)
    need = sum(1 for it in pending if it.get("ai", {}).get("needs_reply"))
    print(f"::notice::返信の仕分け 新着{len(new)}件（要返信{need}件）・AI費用 約${usage.cost():.4f}"
          f"・X API 約${len(new) * 0.001:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
