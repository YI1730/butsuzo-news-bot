"""御開帳カレンダー: 記事から抜き出した公開期間を集め、節目でリマインドの配信候補を作る。

news_hourly.yml から curate_news.py の後に毎時実行される。

1. AI 採点（curate_news.py）が記事から抜き出した公開期間（ai.event）を集め、
   data/kaicho_calendar.json に出来事として蓄える（同じ場所・期間のものは1つにまとめる）
2. 節目に来た出来事について、リマインドの投稿文を作って配信候補にする
   - 公開の前日: 【あすから公開】 / 当日（前日に出せなかった場合）: 【きょうから公開】
   - 終了の3日前〜前日（4日以上の公開のみ）: 【公開はあと◯日】
   リマインドは news.json に source "reminder" として追加され、⭐配信候補欄と通知に出る
3. 展覧会（"remind": false の項目。exhibition_calendar.py と特別展の取得 scrape_exhibition.py 由来）は、
   カレンダーに入った直後にニュースと同じ基準（curate_news.SCORE_SYSTEM。開幕が先でも減点しない）で採点し、
   開幕前で 70 点以上なら配信候補にする（【仏像速報】）。ほかに開幕の前日【あすから開幕】を 1 回だけ出す。
   同じ展覧会のニュース記事がすでに配信候補になっていれば、取得時の候補は出さない（前日の 1 回だけ）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from news_store import CANDIDATE_THRESHOLD, load_skipped
from post_news import JST, load_news_data, save_news_data

BASE_DIR = Path(__file__).parent
CALENDAR_FILE = BASE_DIR / "data" / "kaicho_calendar.json"
SOURCE = "reminder"

EVENT_SOURCES = {"google_news", "official_site", "exhibition"}
NEWS_SOURCES = {"google_news", "official_site"}
EVENT_CATEGORIES = {"秘仏・御開帳・特別公開", "展覧会", "寺院の行事", "発見・研究・新指定", "修理・保存"}
MIN_RELEVANCE = 30         # これ未満（無関係・ノイズ）の記事の期間は使わない
REMIND_FROM_HOUR = 7       # この時刻（JST）以降の実行でリマインドを出す
ENDING_DAYS = 3            # 終了の何日前からリマインドするか
MIN_DAYS_FOR_ENDING = 4    # 公開期間がこれより短い出来事には「あと◯日」を出さない
KEEP_DAYS_AFTER_END = 30   # 終了後もこの日数はカレンダーに残す
REMINDER_SCORE = 90        # リマインドは配信候補として扱う
EXHIBIT_HEADERS = {"found": "【仏像速報】", "start": "【あすから開幕】"}
# 開幕前の展覧会が追加情報を出したときの続報（exhibit_watch.py）。重要度は 出品作品 > ビジュアル > ページ公開
WATCH_HEADERS = {"page": "【展覧会続報】", "visual": "【展覧会続報・ビジュアル公開】", "lineup": "【出品作品公開】"}
EXHIBIT_HEADERS_ALL = (set(EXHIBIT_HEADERS.values()) | {"【まもなく開幕】"}
                       | set(WATCH_HEADERS.values()))  # 重複判定用（旧仕様の見出しと続報を含む）
MAX_EXHIBIT_SCORE_PER_RUN = 30


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
        keep = out.get(k)
        if keep is None and not e.get("start") and e.get("end"):
            # 開始日が分からない項目は、同じ会場で同じ終了日の項目（開始日あり）にまとめる
            keep = next((o for o in out.values() if o["place_key"] == e["place_key"]
                         and o.get("end") == e["end"]), None)
        if keep is not None:
            merge_into(keep, e.get("start", ""), e.get("end", ""))
            keep["reminded"] = sorted(set(keep.get("reminded", [])) | set(e.get("reminded", [])))
            if e.get("news_candidate"):
                keep["news_candidate"] = True
            if e.get("remind") is not False:
                keep.pop("remind", None)  # ニュース由来の項目と同じなら、リマインドの対象にする
            merged += 1
        else:
            out[k] = e
    cal["events"] = list(out.values())
    return merged


def collect_events(cal: dict, items: list[dict], posted_ids: set[str] | None = None) -> int:
    """採点済みの記事から出来事を集めてカレンダーに足す。新しく足した件数を返す。"""
    by_key = {event_key(e["place_key"], e.get("start", ""), e.get("end", "")): e for e in cal["events"]}
    skipped_ids, skipped_urls = load_skipped()
    added = 0
    for it in items:
        ai = it.get("ai") or {}
        ev = ai.get("event") or {}
        if it.get("id") in skipped_ids or it.get("url") in skipped_urls:
            continue  # PWA でスキップした記事からは出来事を作らない（リマインドを出さない）
        posted = it.get("id") in (posted_ids or set())  # @butsuzolink が投稿した記事は、点数が低くても入れる
        news_candidate = (it.get("source") in NEWS_SOURCES and not ai.get("duplicate")
                          and (ai.get("score", 0) >= CANDIDATE_THRESHOLD or posted))
        if ai.get("stale"):
            continue  # 古い記事の再配信は、公開期間の年が違う（curate_news.check_stale）
        if it.get("source") not in EVENT_SOURCES or (ai.get("category") not in EVENT_CATEGORIES and not posted):
            continue
        if (ai.get("score", 0) < MIN_RELEVANCE and not posted) or not ev.get("place"):
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
            if news_candidate:
                e["news_candidate"] = True  # 展覧会の【まもなく開幕】は出さない（ニュースとして流れるため）
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
        if it.get("source") not in NEWS_SOURCES:
            e.update(origin=it["source"], remind=False)  # 特別展の取得由来は展覧会の扱い
        elif news_candidate:
            e["news_candidate"] = True
        cal["events"].append(e)
        by_key[key] = e
        added += 1
    return added


def due_reminders(cal: dict, today: date) -> list[tuple[dict, str, str]]:
    """今日出すべきリマインド (出来事, 種類, 見出し) の一覧。"""
    out = []
    _, skipped_urls = load_skipped()
    for e in cal["events"]:
        if e.get("source_url") in skipped_urls:
            continue  # PWA でスキップした記事・配信候補の出来事は、以後のリマインドも出さない
        start, end = parse_date(e.get("start", "")), parse_date(e.get("end", ""))
        done = set(e.get("reminded", []))
        if e.get("remind") is False:
            # 展覧会: 開幕前だけ、採点が 70 点以上のとき。取得時に 1 回と前日に 1 回
            if not start or start <= today or e.get("exhibit_score", 0) < CANDIDATE_THRESHOLD:
                continue
            if (start - today).days == 1:
                if "start" not in done:
                    out.append((e, "start", EXHIBIT_HEADERS["start"]))
            elif not done and not e.get("news_candidate"):
                out.append((e, "found", EXHIBIT_HEADERS["found"]))
            continue
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


def exhibits_to_score(cal: dict, today: date) -> list[dict]:
    """カレンダーに入った、まだ採点していない開幕前の展覧会（開幕日が分からないものは除く）。"""
    out = []
    for e in cal["events"]:
        start = parse_date(e.get("start", ""))
        if e.get("remind") is False and "exhibit_score" not in e and start and start > today:
            out.append(e)
    return out[:MAX_EXHIBIT_SCORE_PER_RUN]


def score_exhibits(client, usage, events: list[dict], data: dict, today: date) -> None:
    """展覧会を、ニュースと同じ基準で採点する（会期の情報を 1 件の記事として渡す）。"""
    from curate_news import score_items

    now = datetime.now(JST)
    week_ago = (now - timedelta(days=7)).isoformat()
    # 重複判定の材料: 直近の配信候補のニュースと、展覧会の配信候補
    known = [it for it in data["items"] if it.get("fetched_at", "") >= week_ago
             and (it.get("ai") or {}).get("score", 0) >= CANDIDATE_THRESHOLD and not it["ai"].get("duplicate")
             and (it.get("source") in NEWS_SOURCES
                  or (it.get("source") == SOURCE and it["ai"].get("post_header") in EXHIBIT_HEADERS_ALL))]
    items = []
    for e in events:
        days = (parse_date(e["start"]) - today).days
        items.append({
            "id": e["id"],
            "title": f"{e.get('event_name', '')}（{e.get('place', '')}、{e.get('start')}〜{e.get('end') or '?'}）",
            "description": f"展覧会の会期情報（開幕まであと{days}日）。情報源: {e.get('source_title', '')}",
            "url": e.get("source_url", ""),
            "source": "exhibit_calendar",
            "published_at": now.isoformat(timespec="seconds"),
        })
    score_items(client, usage, items, known)
    for e, it in zip(events, items):
        ai = it.get("ai")
        if not ai:
            continue  # 採点に失敗したものは次の実行でやり直す
        e["exhibit_score"] = ai["score"] if not ai["duplicate"] else min(ai["score"], CANDIDATE_THRESHOLD - 1)
        e["exhibit_reason"] = ai["reason"] + ("・重複" if ai["duplicate"] else "")
        print(f"🏛 展覧会の採点 {e['exhibit_score']}点 {e.get('event_name')}（{e.get('place')}）{ai['reason']}")


def reminder_extra(e: dict, kind: str, today: date) -> str:
    period = f"{e.get('start') or '?'}〜{e.get('end') or '?'}"
    start = parse_date(e.get("start", ""))
    left = (start - today).days if start else 0
    if kind == "found":
        return (
            f"これは展覧会の開催情報を紹介する投稿です（今日は {today:%Y-%m-%d}、開幕まであと{left}日）。\n"
            f"展覧会: {e.get('event_name', '')} / 会場: {e.get('place', '')} / 会期: {period}\n"
            "会期・会場を必ず入れる。何が来るのか（仏像の名前・寺院など、情報源に書かれた範囲）と、"
            "楽しみな気持ちで書く。来訪を促す決まり文句は使わない。"
        )
    when = {"start": "公開がまもなく始まる", "ending": "公開がまもなく終わる",
            "preview": f"展覧会の開幕まであと{left}日になった"}[kind]
    if e.get("remind") is False and kind == "start":
        when = "展覧会があす開幕する"
    return (
        f"これは記事の紹介ではなく、{when}ことを知らせるリマインドの投稿です（今日は {today:%Y-%m-%d}）。\n"
        f"出来事: {e.get('event_name', '')} / 場所: {e.get('place', '')} / 公開期間: {period}\n"
        "公開期間・場所を必ず入れる。「行くならいま」「会えるうちに」のような決まり文句に頼らず、"
        "像の見どころや公開の事情で行きたくなるように書く。記事の詳細は補足程度でよい。"
    )


def main() -> int:
    now = datetime.now(JST)
    today = now.date()
    data = load_news_data()
    cal = load_calendar()

    # 古い記事の再配信だったと分かった記事から作った出来事・リマインドは取り除く
    stale_urls = {it["url"] for it in data["items"] if (it.get("ai") or {}).get("stale")}
    if stale_urls:
        cal["events"] = [e for e in cal["events"] if e.get("source_url") not in stale_urls]
        data["items"] = [it for it in data["items"]
                         if not (it.get("source") == SOURCE and it.get("url") in stale_urls)]

    dedupe_events(cal)
    try:
        posted_ids = {p["news_id"] for p in json.loads(
            (BASE_DIR / "data" / "x_posts.json").read_text(encoding="utf-8")).get("posts", []) if p.get("news_id")}
    except Exception:
        posted_ids = set()
    added = collect_events(cal, data["items"], posted_ids)
    # 終わってしばらくたった出来事は捨てる
    cutoff = today - timedelta(days=KEEP_DAYS_AFTER_END)
    cal["events"] = [
        e for e in cal["events"]
        if (parse_date(e.get("end", "")) or parse_date(e.get("start", "")) or today) >= cutoff
    ]
    cal["events"].sort(key=lambda e: e.get("start") or e.get("end") or "")

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    client = usage = None
    if has_key:
        import anthropic

        from curate_news import Usage

        client, usage = anthropic.Anthropic(), Usage()
    to_score = exhibits_to_score(cal, today)
    if to_score and client:
        score_exhibits(client, usage, to_score, data, today)

    reminders = due_reminders(cal, today) if now.hour >= REMIND_FROM_HOUR else []
    made = 0
    if reminders and client:
        from curate_news import POST_FORMAT, notify, write_post

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
                    "score": e.get("exhibit_score", REMINDER_SCORE) if e.get("remind") is False else REMINDER_SCORE,
                    "category": e.get("category", ""),
                    "duplicate": False,
                    "reason": ("展覧会の情報" if kind == "found" else f"{header.strip('【】')}（御開帳カレンダー）")
                              + (f"・{e.get('exhibit_reason', '')}" if e.get("remind") is False else ""),
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
    elif reminders:
        print("::warning::ANTHROPIC_API_KEY が無いためリマインドを作れません", file=sys.stderr)

    if usage:
        print(f"リマインド・展覧会の採点の費用 約${usage.cost():.4f}")
    save_news_data(data)
    cal["last_updated"] = now.isoformat(timespec="seconds")
    CALENDAR_FILE.write_text(json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    upcoming = sum(1 for e in cal["events"] if (parse_date(e.get("end", "")) or today) >= today)
    print(f"::notice::御開帳カレンダー 掲載 {upcoming}件（新規 {added}件）/ リマインド {made}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
