"""「反応したい投稿」への返信を、仏像リンクのアカウントから送る。

PWA で「仏像リンクで返信」を押すと、非公開リポジトリ（PRIVATE_DIR）の reply_queue.json に
送信予約が積まれ、公開リポジトリの data/reaction_kick.json（中身は時刻だけ）が更新される。
その push をきっかけに reaction_reply.yml がこのスクリプトを動かす。

このリポジトリと Actions のログは公開されているので、ログには件数だけを出し、
返信先のアカウント名・本文・投稿 ID は出さない。結果は reactions.json の各提案に記録する。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import post_visit_tweet as pv
from post_news import JST
from reply_send import send_reply

PRIVATE_DIR = Path(os.environ.get("PRIVATE_DIR", "private"))
QUEUE_FILE = PRIVATE_DIR / "reply_queue.json"
REACTIONS_FILE = PRIVATE_DIR / "reactions.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    queue = load_json(QUEUE_FILE, [])
    if not isinstance(queue, list) or not queue:
        print("返信の予約はありません")
        return 0
    state = load_json(REACTIONS_FILE, {"items": []})
    by_id = {it["id"]: it for it in state.get("items", [])}
    session = pv.get_oauth_session()
    now = datetime.now(JST).isoformat(timespec="seconds")
    remaining, sent, failed, skipped = [], 0, 0, 0

    for op in queue:
        it = by_id.get(str(op.get("id", "")))
        text = (op.get("text") or "").strip()
        if not it or not text:
            skipped += 1
            continue
        if it.get("reply_status") == "replied":
            skipped += 1  # 二重送信を防ぐ
            continue
        tid, msg = send_reply(session, it["id"], text)
        if tid:
            it.update(reply_status="replied", reply_text=text, reply_tweet_id=tid, replied_at=now)
            it.pop("reply_error", None)
            sent += 1
        else:
            # 本文や相手は出さず、HTTP の状態だけを記録する（PWA に表示）
            it["reply_error"] = msg.split(":")[0][:40]
            it["reply_status"] = "failed"
            failed += 1

    QUEUE_FILE.write_text(json.dumps(remaining, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    REACTIONS_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"::notice::反応したい投稿への返信 送信{sent}件・失敗{failed}件・スキップ{skipped}件"
          f"（約${sent * 0.015:.3f}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
