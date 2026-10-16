"""御開帳カレンダー: 記事から抜き出した公開期間を集め、節目でリマインドの配信候補を作る。

news_hourly.yml から curate_news.py の後に毎時実行される。

1. AI 採点（curate_news.py）が記事から抜き出した公開期間（ai.event）を集め、
   data/kaicho_calendar.json に出来事として蓄える（同じ場所・期間のものは1つにまとめる）
2. 節目に来た出来事について、リマインドの投稿文を作って配信候補にする
   - 公開の前日: 【あすから公開】 / 当日（前日に出せなかった場合）: 【きょうから公開】
   - 終了の3日前〜前日（4日以上の公開のみ）: 【公開はあと◯日】
   リマインドは news.json に source "reminder" として追加され、⭐配信候補欄と通知に出る
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from news_store import CANDIDATE_THRESHOLD
from post_news import JST, load_news_data, save_news_data

BASE_DIR = Path(__file__).parent
CALENDAR_FILE = BASE_DIR / "data" / "kaicho_calendar.json"
SOURCE = "reminder"

EVENT_SOURCES = {"google_news", "official_site"}
EVENT_CATEGORIES = {"秘仏・御開帳・特別公開", "展覧会", "寺院の行事", "発見・研究・新指定", "修理・保存"}
MIN_RELEVANCE = 30         # これ未満（無関係・ノイズ）の記事の期間は使わない
REMIND_FROM_HOUR = 7       # この時刻（JST）以降の実行でリマインドを出す
ENDING_DAYS = 3            # 終了の何日前からリマインドするか
MIN_DAYS_FOR_ENDING = 4    # 公開期間がこれより短い出来事には「あと◯日」を出さない
KEEP_DAYS_AFTER_END = 30   # 終了後もこの日数はカレンダーに残す
REMINDER_SCORE = 90        # リマインドは配信候補として扱う


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (AttributeError, ValueError):
        return None


def place_key(place: str) -> str:
    """場所の表記ゆれを吸収した比較用キー（括弧内・空白を除き先頭8文字）。"""
    p = re.sub(r"[（(].*?[）)]", "", place or "")
    return re.sub(r"\s", "", p)[:8]


def load_calendar() -> dict:
    try:
        d = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("events", [])
            return d
    except Exception:
        pass
    return {"events": []}


def event_key(pkey: str, start: str, end: str) -> tuple[str, str]:
    """同じ出来事を判定するキー。記事によって終了日の有無が分かれるので、
    開始日が分かれば（場所, 開始日）、分からなければ（場所, 終了日）で比べる。"""
    return (pkey, "s" + start) if start else (pkey, "e" + end)


def merge_into(e: dict, start: str, end: str) -> None:
    """既存の出来事に、別の記事で分かった開始日・終了日を補う。"""
    if start and not e.get("start"):
        e["start"] = start
    if end and not e.get("end"):
        e["end"] = end


def dedupe_events(cal: dict) -> int:
    """キーが同じ出来事を1つにまとめる（旧仕様で別扱いになっていた分の整理）。"""
    merged, out = 0, {}
    for e in cal["events"]:
        k = event_key(e["place_key"], e.get("start", ""), e.get("end", ""))
        if k in out:
            keep = out[k]
            merge_into(keep, e.get("start", ""), e.get("end", ""))
            keep["reminded"] = sorted(set(keep.get("reminded", [])) | set(e.get("reminded", [])))
            merged += 1
        else:
            out[k] = e
    cal["events"] = list(out.values())
    return merged


def collect_events(cal: dict, items: list[dict]) -> int:
    """採点済みの記事から出来事を集めてカレンダーに足す。新しく足した件数を返す。"""
    by_key = {event_key(e["place_key"], e.get("start", ""), e.get("end", "")): e for e in cal["events"]}
    added = 0
    for it in items:
        ai = it.get("ai") or {}
        ev = ai.get("event") or {}
        if it.get("source") not in EVENT_SOURCES or ai.get("category") not in EVENT_CATEGORIES:
            continue
        if ai.get("score", 0) < MIN_RELEVANCE or not ev.get("place"):
            continue
        start, end = parse_date(ev.get("start_date", "")), parse_date(ev.get("end_date", ""))
        if not start and not end:
            continue
        if start and end and end < start:
            start, end = end, start
        s_iso, e_iso = (start.isoformat() if start else ""), (end.isoformat() if end else "")
        pkey = place_key(ev["place"])
        key = event_key(pkey, s_iso, e_iso)
        if key in by_key:
            e = by_key[key]
            merge_into(e, s_iso, e_iso)
            # より点数の高い記事を情報源にする（投稿のリンク先・本文の材料になる）
            if ai.get("score", 0) > e.get("source_score", 0):
                e.update(source_url=it["url"], source_title=it["title"], source_score=ai["score"])
            continue
        e = {
            "id": hashlib.md5("|".join(key).encode()).hexdigest()[:12],
            "event_name": ev.get("event_name", ""),
            "place": ev["place"],
            "place_key": pkey,
            "start": s_iso,
            "end": e_iso,
            "category": ai.get("category", ""),
            "source_url": it["url"],
            "source_title": it["title"],
            "source_score": ai.get("score", 0),
            "first_seen": datetime.now(JST).isoformat(timespec="seconds"),
            "reminded": [],
        }
        cal["events"].append(e)
        by_key[key] = e
        added += 1
    return added


def due_reminders(cal: dict, today: date) -> list[tuple[dict, str, str]]:
    """今日出すべきリマインド (出来事, 種類, 見出し) の一覧。"""
    out = []
    for e in cal["events"]:
        start, end = parse_date(e.get("start", "")), parse_date(e.get("end", ""))
        done = set(e.get("reminded", []))
        if start and "start" not in done:
            if start == today + timedelta(days=1):
                out.append((e, "start", "【あすから公開】"))
                continue
            if start == today:
                out.append((e, "start", "【きょうから公開】"))
                continue
        if end and "ending" not in done and (not start or start <= today):
            left = (end - today).days
            long_enough = (not start) or (end - start).days + 1 >= MIN_DAYS_FOR_ENDING
            if 1 <= left <= ENDING_DAYS and long_enough:
                out.append((e, "ending", f"【公開はあと{left}日】"))
    return out


def reminder_extra(e: dict, kind: str, today: date) -> str:
    period = f"{e.get('start') or '?'}〜{e.get('end') or '?'}"
    when = {"start": "公開がまもなく始まる", "ending": "公開がまもなく終わる"}[kind]
    return (
        f"これは記事の紹介ではなく、{when}ことを知らせるリマインドの投稿です（今日は {today:%Y-%m-%d}）。\n"
        f"出来事: {e.get('event_name', '')} / 場所: {e.get('place', '')} / 公開期間: {period}\n"
        "公開期間・場所を必ず入れ、「行くならいま」が伝わるように。記事の詳細は補足程度でよい。"
    )


def main() -> int:
    now = datetime.now(JST)
    today = now.date()
    data = load_news_data()
    cal = load_calendar()

    dedupe_events(cal)
    added = collect_events(cal, data["items"])
    # 終わってしばらくたった出来事は捨てる
    cutoff = today - timedelta(days=KEEP_DAYS_AFTER_END)
    cal["events"] = [
        e for e in cal["events"]
        if (parse_date(e.get("end", "")) or parse_date(e.get("start", "")) or today) >= cutoff
    ]
    cal["events"].sort(key=lambda e: e.get("start") or e.get("end") or "")

    reminders = due_reminders(cal, today) if now.hour >= REMIND_FROM_HOUR else []
    made = 0
    if reminders and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        from curate_news import POST_FORMAT, Usage, notify, write_post

        client, usage = anthropic.Anthropic(), Usage()
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        existing = {it["id"] for it in data["items"]}
        for e, kind, header in reminders:
            rid = f"rem-{e['id']}-{kind}"
            e.setdefault("reminded", []).append(kind)  # 失敗しても同じ節目では作り直さない
            if rid in existing:
                continue
            item = {"url": e["source_url"], "title": e["source_title"]}
            text = write_post(client, usage, item, header=header, extra=reminder_extra(e, kind, today))
            if not text:
                continue
            rem = {
                "id": rid,
                "title": e["source_title"],
                "url": e["source_url"],
                "source": SOURCE,
                "header": header,
                "hashtags": "",
                "fetched_at": now.isoformat(timespec="seconds"),
                "published_at": now.isoformat(timespec="seconds"),
                "image_url": next((it.get("image_url", "") for it in data["items"]
                                   if it.get("url") == e["source_url"]), ""),
                "ai": {
                    "score": REMINDER_SCORE,
                    "category": e.get("category", ""),
                    "duplicate": False,
                    "reason": f"{header.strip('【】')}（御開帳カレンダー）",
                    "post_text": text,
                    "post_header": header,
                    "post_format": POST_FORMAT,
                },
            }
            data["items"].insert(0, rem)
            made += 1
            print(f"⏰ {header} {e.get('event_name')}（{e.get('place')}）\n   {text}")
            if topic:
                notify(topic, rem)
                rem["ai"]["notified"] = True
        print(f"リマインド費用 約${usage.cost():.4f}")
    elif reminders:
        print("::warning::ANTHROPIC_API_KEY が無いためリマインドを作れません", file=sys.stderr)

    save_news_data(data)
    cal["last_updated"] = now.isoformat(timespec="seconds")
    CALENDAR_FILE.write_text(json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    upcoming = sum(1 for e in cal["events"] if (parse_date(e.get("end", "")) or today) >= today)
    print(f"::notice::御開帳カレンダー 掲載 {upcoming}件（新規 {added}件）/ リマインド {made}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
