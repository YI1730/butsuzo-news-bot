"""PWA の「💬返信」タブで確定した操作（data/reply_queue.json）を処理する。

- action "reply": 仏像リンクのアカウントから X API で返信を送る（URL なしで 1 件 $0.015）
- action "dismiss": 返信不要として一覧から外す

reply_send.yml から、reply_queue.json が push されたときに実行される。
処理した操作はキューから消し、結果を data/replies.json の各項目の status に記録する。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import post_visit_tweet as pv
from post_news import JST
from triage_replies import load_state, save_state

QUEUE_FILE = Path(__file__).parent / "data" / "reply_queue.json"
TWEETS_ENDPOINT = "https://api.twitter.com/2/tweets"


def load_queue() -> list[dict]:
    try:
        d = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def send_reply(session, in_reply_to: str, text: str) -> tuple[str | None, str]:
    try:
        r = session.post(TWEETS_ENDPOINT, json={
            "text": text, "reply": {"in_reply_to_tweet_id": in_reply_to},
        }, timeout=20)
    except Exception as e:
        return None, f"リクエスト例外: {e}"
    if r.status_code in (200, 201):
        return r.json().get("data", {}).get("id", ""), "送信成功"
    return None, f"HTTP {r.status_code}: {r.text[:300]}"


def main() -> int:
    queue = load_queue()
    if not queue:
        print("キューは空です")
        return 0
    state = load_state()
    by_id = {it["id"]: it for it in state["items"]}
    session = None
    remaining, failed = [], 0
    now = datetime.now(JST).isoformat(timespec="seconds")

    for op in queue:
        it = by_id.get(op.get("id", ""))
        if it is None:
            print(f"対象が見つからないためスキップ: {op}")
            continue
        if it.get("status") != "open":
            print(f"処理済みのためスキップ: {op.get('id')} ({it.get('status')})")
            continue
        if op.get("action") == "dismiss":
            it["status"] = "dismissed"
            it["handled_at"] = now
            print(f"返信不要: @{it['author_username']}")
            continue
        text = (op.get("text") or "").strip()
        if op.get("action") != "reply" or not text:
            print(f"不正な操作のためスキップ: {op}")
            continue
        if session is None:
            session = pv.get_oauth_session()
        tid, msg = send_reply(session, it["id"], text)
        print(f"返信 @{it['author_username']}: {msg}")
        if tid:
            it.update(status="replied", reply_text=text, reply_tweet_id=tid, handled_at=now)
        else:
            failed += 1
            it["send_error"] = msg
            remaining.append(op)  # 失敗した操作は次回に再試行

    save_state(state)
    QUEUE_FILE.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failed:
        print(f"::warning::返信の送信に {failed} 件失敗（キューに残して再試行）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
