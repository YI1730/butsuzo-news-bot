"""御開帳カレンダー（data/kaicho_calendar.json）に、仏像が出る展覧会を加える。週 2 回の実行。

ニュース記事になっていない展覧会（会期が記事に書かれていないもの、記事にならないもの）の取りこぼしを減らす。

A. 観仏三昧の展覧会情報（news.json の source "kanbutsu"。会場・展覧会名・会期がタイトルに入っている）を、
   タイトルから読み取ってカレンダーに加える。仏像が出る展覧会かどうかだけ、軽いモデルで判定する
B. config/exhibition_sources.json の主な博物館・美術館の展覧会ページを見に行き、軽いモデルで
   「仏像が出る展覧会」の名前・会期・公式ページを抜き出す。ページの内容が前回と同じなら AI は呼ばない

追加した項目は "remind": false（「あすから公開」のリマインド配信候補は作らない）。
すでにニュース由来の同じ展覧会（同じ会場・同じ開始日、または終了日）があれば、新しい項目は作らず
足りない開始日・終了日だけを補う。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from bs4 import BeautifulSoup

import kaicho_calendar as kc
from post_news import JST, RESOLVE_USER_AGENT, load_news_data

BASE_DIR = Path(__file__).parent
SOURCES_FILE = BASE_DIR / "config" / "exhibition_sources.json"
STATE_FILE = BASE_DIR / "data" / "exhibition_state.json"
LIGHT_MODEL = "claude-haiku-5-5"
CATEGORY = "展覧会"
HEADERS = {"User-Agent": RESOLVE_USER_AGENT, "Accept-Language": "ja,en;q=0.5"}
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_AHEAD_DAYS = 240        # これより先に始まる展覧会は、まだ加えない
PAGE_CHARS = 3500           # 1 ページから AI に渡す文字数
MAX_SUBPAGES = 3            # 各館で、トップ（または指定ページ）のほかにたどるリンク数
LINK_RE = re.compile(r"開催中|開催予定|これから|次回|今後|展覧会|企画展|特別展|スケジュール|現在の展示|exhibition", re.I)

JUDGE_SYSTEM = """博物館・美術館・寺院の展覧会の一覧から、「仏像（仏像・神像・仏教彫刻・密教彫刻）が展示の主役
または重要な展示になる展覧会」を選んでください。
- true: 仏像展、仏教美術展、寺宝展（仏像を含む）、地域の仏像を集めた展覧会、特定の寺院・仏師の展覧会
- false: 絵画・工芸・書跡・考古・民俗・歴史資料・現代美術・石造物などだけの展覧会、仏像と無関係なもの
判断できないときは false。"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "has_buddha": {"type": "boolean"}},
        "required": ["id", "has_buddha"], "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False,
}

EXTRACT_SYSTEM = """博物館・美術館の展覧会情報のページ内容から、「仏像（仏像・神像・仏教彫刻）が展示の主役または
重要な展示になる展覧会」だけを抜き出してください。開催中・これから開催されるものが対象で、終了したものは除きます。
- event_name: 展覧会名（「特別展」「企画展」などの種別を含めてよい）
- start_date / end_date: YYYY-MM-DD。年が書かれていなければ今日の日付から判断する。分からなければ空文字（推測で埋めない）
- url: 渡されたリンク一覧にある、その展覧会の個別ページの URL。無ければ空文字
- 絵画・工芸・書跡・考古・民俗・現代美術などだけの展覧会、仏像と無関係なものは含めない
該当が無ければ空の配列を返す。"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": {
        "type": "object",
        "properties": {"event_name": {"type": "string"}, "start_date": {"type": "string"},
                       "end_date": {"type": "string"}, "url": {"type": "string"}},
        "required": ["event_name", "start_date", "end_date", "url"], "additionalProperties": False}}},
    "required": ["events"], "additionalProperties": False,
}

KANBUTSU_RE = re.compile(
    r"^(?P<venue>\S+)\s+(?P<name>.+?)（(?P<m1>\d+)月(?P<d1>\d+)日\s*[~〜～-]\s*(?:(?P<m2>\d+)月)?(?P<d2>\d+)日）\s*$"
)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_kanbutsu(title: str, today: date) -> dict | None:
    """「会場 展覧会名（10月24日~12月20日）」から会場・展覧会名・会期を読み取る。"""
    if "\n" in title:
        return None
    m = KANBUTSU_RE.match(title.strip())
    if not m:
        return None
    try:
        y = today.year
        start = date(y, int(m["m1"]), int(m["d1"]))
        end = date(y, int(m["m2"] or m["m1"]), int(m["d2"]))
        if end < start:
            end = end.replace(year=y + 1)
    except ValueError:
        return None
    if end < today or (start - today).days > MAX_AHEAD_DAYS:
        return None  # 終わった、またはまだ先すぎる（去年・来年の同じ月日と区別できないため）
    return {"venue": m["venue"], "name": m["name"].strip(), "start": start.isoformat(), "end": end.isoformat()}


def find_match(cal: dict, pkey: str, start: str, end: str) -> dict | None:
    """同じ展覧会とみなせる既存の項目（同じ会場で、開始日が同じ、または終了日が同じで片方に開始日がない）。"""
    for e in cal["events"]:
        if e.get("place_key") != pkey:
            continue
        es, ee = e.get("start", ""), e.get("end", "")
        if start and es == start:
            return e
        if end and ee == end and (not es or not start):
            return e
    return None


def add_event(cal: dict, place: str, name: str, start: str, end: str, url: str, origin: str) -> str:
    """カレンダーに展覧会を加える。"added" / "merged"（既存を補った）/ "skip" を返す。"""
    pkey = kc.place_key(place)
    hit = find_match(cal, pkey, start, end)
    if hit:
        before = (hit.get("start"), hit.get("end"))
        kc.merge_into(hit, start, end)
        return "merged" if (hit.get("start"), hit.get("end")) != before else "skip"
    key = kc.event_key(pkey, start, end)
    cal["events"].append({
        "id": hashlib.md5("|".join(key).encode()).hexdigest()[:12],
        "event_name": name,
        "place": place,
        "place_key": pkey,
        "start": start,
        "end": end,
        "category": CATEGORY,
        "source_url": url,
        "source_title": f"{place} {name}",
        "source_score": 0,
        "first_seen": datetime.now(JST).isoformat(timespec="seconds"),
        "reminded": [],
        "origin": origin,
        "remind": False,
    })
    return "added"


def judge(client, usage, entries: list[dict]) -> dict[str, bool]:
    """展覧会の一覧（id・会場・展覧会名）が、仏像の出る展覧会か軽いモデルで判定する。"""
    from curate_news import call_json

    out: dict[str, bool] = {}
    for i in range(0, len(entries), 40):
        batch = entries[i:i + 40]
        payload = [{"id": e["id"], "venue": e["venue"], "name": e["name"]} for e in batch]
        res = call_json(client, usage, JUDGE_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1),
                        JUDGE_SCHEMA, effort="low", model=LIGHT_MODEL)
        for r in (res or {}).get("results", []):
            out[r["id"]] = bool(r["has_buddha"])
    return out


def fetch(url: str) -> tuple[str, str, list[tuple[str, str]]]:
    """ページの (最終URL, 本文テキスト, リンク一覧)。失敗時は空。"""
    try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
        except requests.exceptions.SSLError:
            r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            return url, "", []
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception:
        return url, "", []
    for tag in soup(["script", "style", "noscript", "form"]):
        tag.decompose()
    links = []
    for a in soup.find_all("a", href=True):
        href = urlsplit(urljoin(r.url, a["href"]))._replace(fragment="").geturl()
        text = " ".join(a.get_text(" ", strip=True).split())
        if href.startswith("http") and text:
            links.append((text[:60], href))
    text = " ".join(soup.get_text(" ", strip=True).split())
    return r.url, text, links


def collect_museum_pages(m: dict) -> tuple[str, list[tuple[str, str]]]:
    """館のページ（指定の URL ＋ 展覧会関連のリンク先を MAX_SUBPAGES 件まで）の本文とリンク一覧。"""
    final, text, links = fetch(m["url"])
    host = urlsplit(final).hostname or ""
    pages = [f"## {final}\n{text[:PAGE_CHARS]}"] if text else []
    subs, seen = [], {final}
    for t, h in links:
        if h in seen or (urlsplit(h).hostname or "") != host or not LINK_RE.search(t):
            continue
        if re.search(r"過去|終了|アーカイブ|past|archive|pdf$", t + h, re.I):
            continue
        seen.add(h)
        subs.append(h)
        if len(subs) >= MAX_SUBPAGES:
            break
    for u in subs:
        _, t2, _ = fetch(u)
        if t2:
            pages.append(f"## {u}\n{t2[:PAGE_CHARS]}")
    return "\n\n".join(pages), links


def scan_museums(client, usage, cal: dict, state: dict, today: date) -> dict:
    from curate_news import call_json

    stats = {"museums": 0, "skipped_same": 0, "failed": 0, "added": 0, "merged": 0}
    sources = load_json(SOURCES_FILE, {}).get("museums", [])
    for m in sources:
        body, links = collect_museum_pages(m)
        if not body:
            stats["failed"] += 1
            print(f"  取得できず: {m['name']}")
            continue
        stats["museums"] += 1
        digest = hashlib.md5(body.encode()).hexdigest()
        if state["page_hash"].get(m["name"]) == digest:
            stats["skipped_same"] += 1
            continue
        link_list = "\n".join(f"- {t} {h}" for t, h in links[:80])
        user = (f"今日: {today:%Y-%m-%d}\n館: {m['name']}\n\n# ページ内容\n{body}\n\n# リンク一覧\n{link_list}")
        res = call_json(client, usage, EXTRACT_SYSTEM, user, EXTRACT_SCHEMA, effort="low", model=LIGHT_MODEL)
        if res is None:
            stats["failed"] += 1
            continue
        state["page_hash"][m["name"]] = digest
        valid = {h for _, h in links}
        for ev in res.get("events", []):
            start, end = kc.parse_date(ev["start_date"]), kc.parse_date(ev["end_date"])
            if not (start or end) or (end and end < today) or (start and (start - today).days > MAX_AHEAD_DAYS):
                continue
            url = ev["url"] if ev["url"] in valid else m["url"]
            r = add_event(cal, m["place"], ev["event_name"].strip(),
                          start.isoformat() if start else "", end.isoformat() if end else "", url, "museum")
            if r == "added":
                stats["added"] += 1
                print(f"  ＋{m['name']}: {ev['event_name']} {ev['start_date']}〜{ev['end_date']}")
            elif r == "merged":
                stats["merged"] += 1
    return stats


def main() -> int:
    today = datetime.now(JST).date()
    cal = kc.load_calendar()
    state = load_json(STATE_FILE, {})
    state.setdefault("judged", {})
    state.setdefault("page_hash", {})
    kc.dedupe_events(cal)

    client = usage = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        from curate_news import Usage

        client, usage = anthropic.Anthropic(), Usage()

    # A. 観仏三昧の展覧会情報
    items = [it for it in load_news_data()["items"] if it.get("source") == "kanbutsu"]
    cands = []
    for it in items:
        p = parse_kanbutsu(it.get("title", ""), today)
        if p:
            p["id"] = hashlib.md5(f"{p['venue']}|{p['name']}|{p['start']}".encode()).hexdigest()[:12]
            p["url"] = it.get("url", "")
            cands.append(p)
    todo = [c for c in cands if c["id"] not in state["judged"]]
    if todo and client:
        state["judged"].update(judge(client, usage, todo))
    a_added = a_merged = 0
    for c in cands:
        if state["judged"].get(c["id"]):
            r = add_event(cal, c["venue"], c["name"], c["start"], c["end"], c["url"], "kanbutsu")
            a_added += r == "added"
            a_merged += r == "merged"

    # B. 主な博物館・美術館の展覧会ページ
    stats = {"museums": 0, "skipped_same": 0, "failed": 0, "added": 0, "merged": 0}
    if client:
        stats = scan_museums(client, usage, cal, state, today)
    else:
        print("::warning::ANTHROPIC_API_KEY が無いため判定・館の確認をスキップ", file=sys.stderr)

    cal["last_updated"] = datetime.now(JST).isoformat(timespec="seconds")
    cal["events"].sort(key=lambda e: e.get("start") or e.get("end") or "")
    kc.CALENDAR_FILE.write_text(json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    state["last_run"] = cal["last_updated"]
    # 判定済みの記録は、終わった展覧会ぶんが溜まらないよう、今回の候補にあるものだけ残す
    live = {c["id"] for c in cands}
    state["judged"] = {k: v for k, v in state["judged"].items() if k in live}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cost = usage.cost() if usage else 0.0
    print(f"::notice::展覧会カレンダー 観仏三昧 候補{len(cands)}件（追加{a_added}・補完{a_merged}）/ "
          f"館 {stats['museums']}館（変更なし{stats['skipped_same']}・取得失敗{stats['failed']}）"
          f"追加{stats['added']}・補完{stats['merged']} / AI費用 約${cost:.3f}（上限見積り）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
