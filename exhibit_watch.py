"""展覧会ウォッチ: 発表済みで開幕前の展覧会が、続報（詳細情報）を出した節目を捉えて配信候補にする。毎日 1 回の実行。

展覧会は、発表時に 1 回（【仏像速報】）、開幕の前日に 1 回（【あすから開幕】）だけ配信候補になる（kaicho_calendar.py）。
その間に、館が詳細を出していく。次の 3 段階を、出た順に 1 回ずつ配信候補にする（運営者が承認して投稿する）。

  page   : その展覧会の専用ページが館のサイトに出た           → 【展覧会続報】
  visual : ポスター・チラシ・キービジュアルが出た             → 【展覧会続報・ビジュアル公開】
  lineup : 主な出品作品・出品リスト・見どころ（仏像の名前）が出た → 【出品作品公開】（いちばん重要）

- 監視の対象は data/exhibit_watch.json（開幕前の展覧会。カレンダーの展覧会項目と、館の取得で見つけた先の展覧会）。
  開幕したら外す（以後は【あすから開幕】が担当）
- 詳細ページが分からないときは、館の展覧会一覧ページのリンクから、展覧会名で探す
- ページの内容（本文・画像・資料リンク）が前回と同じなら AI は呼ばない。変わったときだけ軽いモデルで 3 段階を判定する
- 出品リスト・プレスリリース・チラシの PDF は、リンクの文字から見分けて本文を取り出し、判定の材料に加える
- 同じ日に複数の段階が揃ったら、いちばん重要な見出しで 1 件にまとめる（lineup > visual > page）
- 出品作品は、あとで 3 件以上増えたときだけ、もう 1 回【出品作品公開】を出す。それより少ない増加は記録だけ
- 配信候補は curate_news.score_items（ニュースと同じ基準）で採点し、70 点以上のものだけ作る
"""

from __future__ import annotations

import argparse
import hashlib
import io
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

import exhibition_calendar as ec
import kaicho_calendar as kc
from news_store import CANDIDATE_THRESHOLD
from post_news import JST, load_news_data, save_news_data

BASE_DIR = Path(__file__).parent
WATCH_FILE = BASE_DIR / "data" / "exhibit_watch.json"
SOURCE = kc.SOURCE
HEADERS_BY_STAGE = kc.WATCH_HEADERS
STAGE_PRIORITY = ("lineup", "visual", "page")   # 重要な順
STAGE_LABEL = {"page": "専用ページの公開", "visual": "ポスター・チラシの公開", "lineup": "出品作品の公開"}
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WATCH_AHEAD_DAYS = 550          # 約 18 か月先の開幕まで監視する
MAX_CHECKS_PER_RUN = 25         # 1 回の実行で AI に渡すページ数の上限（費用の歯止め）
MAX_CANDIDATES_PER_RUN = 6      # 1 回の実行で作る配信候補の上限（超えた分は翌日に回す）
LINEUP_REPOST_MIN = 3           # 出品作品の 2 回目以降の配信候補は、新しい作品がこれ以上増えたとき
PAGE_TEXT_CHARS = 6000          # AI に渡す詳細ページの文字数
PDF_TEXT_CHARS = 5000           # AI に渡す PDF 1 件の文字数
PDF_MAX_BYTES = 6 * 1024 * 1024
PDF_MAX_PAGES = 8
MAX_PDFS = 3
PDF_RE = re.compile(r"出品|作品リスト|作品一覧|目録|プレスリリース|プレス|リリース|チラシ|フライヤー|press|flyer|list", re.I)
FLYER_RE = re.compile(r"チラシ|ポスター|フライヤー|キービジュアル|flyer|poster|visual|pdf", re.I)
IMG_SKIP_RE = re.compile(r"logo|icon|sprite|banner|btn|button|arrow|spacer|common|header|footer|\.svg|\.gif|^data:", re.I)

ANALYZE_SYSTEM = """博物館・美術館の展覧会の詳細ページ（と、そこからリンクされた PDF）の内容から、次を判定してください。
対象の展覧会は「展覧会情報」に書かれたものです。ページが別の展覧会・館の一覧・トップページなら matches_exhibition は false。
- matches_exhibition: このページは、対象の展覧会そのもののページか
- is_dedicated_page: 対象の展覧会専用のページか（館のトップ・展覧会の一覧・複数の展覧会が並ぶページは false）
- has_visual: ポスター・チラシ・キービジュアルが公開されているか。画像として載っている、または「チラシ」「ポスター」の画像・PDF へのリンクがある
- visual_image_url: ポスター・チラシ・キービジュアルの画像の URL。渡された「画像一覧」にあるものだけ。分からなければ空文字
- has_lineup: 展示される作品が具体的に分かるか（主な出品作品・出品リスト・見どころ・展示予定の作品名）
- works: 本文・PDF に書かれた、仏像・神像・仏教彫刻の作品。temple は所蔵（寺院・所蔵先）で、書かれていなければ空文字。
  name は書かれたとおりに写す（「十一面観音立像」「伝◯◯作」など。書かれていない作品、推測、言い換えは入れない）。
  絵画・工芸・書跡などの作品は入れない
今日の日付や展覧会名から推測して埋めない。ページに書かれていることだけで判断する。"""

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches_exhibition": {"type": "boolean"},
        "is_dedicated_page": {"type": "boolean"},
        "has_visual": {"type": "boolean"},
        "visual_image_url": {"type": "string"},
        "has_lineup": {"type": "boolean"},
        "works": {"type": "array", "items": {
            "type": "object",
            "properties": {"temple": {"type": "string"}, "name": {"type": "string"}},
            "required": ["temple", "name"], "additionalProperties": False}},
    },
    "required": ["matches_exhibition", "is_dedicated_page", "has_visual", "visual_image_url",
                 "has_lineup", "works"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- 監視リスト

def load_watch() -> dict:
    d = ec.load_json(WATCH_FILE, {})
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    return d


def save_watch(watch: dict) -> None:
    watch["last_updated"] = datetime.now(JST).isoformat(timespec="seconds")
    WATCH_FILE.write_text(json.dumps(watch, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def entry_id(place: str, start: str, end: str) -> str:
    """カレンダーの項目 id と同じ作り方（exhibition_calendar.add_event）。"""
    return hashlib.md5("|".join(kc.event_key(kc.place_key(place), start, end)).encode()).hexdigest()[:12]


def find_museum(place: str, museums: list[dict]) -> dict | None:
    return next((m for m in museums if ec.same_place(m.get("place", ""), place)), None)


def add_to_watch(watch: dict, place: str, name: str, start: str, end: str, url: str,
                 museums: list[dict], event_id: str = "", official: bool = False) -> str:
    """監視リストに展覧会を加える。"added" / "skip" を返す。
    official: url がその展覧会の個別ページと分かっているとき（館の取得で見つけたもの）。"""
    eid = event_id or entry_id(place, start, end)
    entries = watch["entries"]
    if eid in entries or any(ec.same_place(e["venue"], place) and e["start"] == start
                             for e in entries.values()):
        return "skip"
    m = find_museum(place, museums)
    detail = url if (official and url and (not m or url.rstrip("/") != m["url"].rstrip("/"))) else ""
    now = datetime.now(JST).isoformat(timespec="seconds")
    entries[eid] = {
        "id": eid, "venue": place, "name": name, "start": start, "end": end,
        "list_url": m["url"] if m else "", "detail_url": detail,
        "stages": {"page": {"at": now, "candidate": "", "baseline": True}} if detail else {},
        "page_hash": "", "works": [], "works_seen": [], "rejected_urls": [],
        "added_at": now,
    }
    return "added"


def seed_from_calendar(watch: dict, cal: dict, today: date, museums: list[dict]) -> int:
    """カレンダーの展覧会（remind: false）で、開幕がこれから（約 18 か月先まで）のものを監視リストに加える。"""
    n = 0
    for e in cal["events"]:
        start = kc.parse_date(e.get("start", ""))
        if e.get("remind") is not False or not start or start <= today:
            continue
        if (start - today).days > WATCH_AHEAD_DAYS:
            continue
        r = add_to_watch(watch, e.get("place", ""), e.get("event_name", ""), e["start"], e.get("end", ""),
                         e.get("source_url", ""), museums, event_id=e["id"],
                         official=e.get("origin") == "museum")
        n += r == "added"
    return n


def prune(watch: dict, today: date) -> int:
    """開幕した展覧会（開幕日が今日以前）と、開幕日が分からないものを外す。"""
    drop = [k for k, e in watch["entries"].items()
            if not (kc.parse_date(e.get("start", "")) and kc.parse_date(e["start"]) > today)]
    for k in drop:
        del watch["entries"][k]
    return len(drop)


# ---------------------------------------------------------------- 取得

def _get(url: str, **kw):
    try:
        return requests.get(url, headers=ec.HEADERS, timeout=15, **kw)
    except requests.exceptions.SSLError:
        return requests.get(url, headers=ec.HEADERS, timeout=15, verify=False, **kw)


def fetch_detail(url: str) -> dict | None:
    """詳細ページの {url, text, links, images}。取得に失敗したら None。
    ヘッダー・フッター・ナビは除き、本文の領域（main / article）を優先する。"""
    try:
        r = _get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html").lower():
            return None
        soup = BeautifulSoup(r.content, "html.parser")
    except Exception:
        return None
    for tag in soup(["script", "style", "noscript", "form", "header", "footer", "nav", "aside"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    if len(root.get_text(strip=True)) < 200 and soup.body:
        root = soup.body
    links, images = [], []
    for a in root.find_all("a", href=True):
        href = urlsplit(urljoin(r.url, a["href"]))._replace(fragment="").geturl()
        text = " ".join(a.get_text(" ", strip=True).split()) or (a.find("img") or {}).get("alt", "") or ""
        if href.startswith("http"):
            links.append((text[:60], href))
    for img in root.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or IMG_SKIP_RE.search(src):
            continue
        images.append((urljoin(r.url, src), (img.get("alt") or "")[:60]))
    text = " ".join(root.get_text(" ", strip=True).split())
    return {"url": r.url, "text": text, "links": links, "images": images[:30]}


def content_hash(page: dict) -> str:
    """本文・画像・資料リンクのハッシュ。ポスターや PDF が増えたときも変化として捉える。"""
    docs = sorted(h for _, h in page["links"] if ".pdf" in h.lower())
    imgs = sorted(s for s, _ in page["images"])
    return hashlib.md5("\n".join([page["text"][:30000], *docs, *imgs]).encode()).hexdigest()


def doc_links(page: dict) -> list[tuple[str, str]]:
    """出品リスト・プレスリリース・チラシらしい PDF のリンク（テキスト, URL）。"""
    out, seen = [], set()
    for t, h in page["links"]:
        if ".pdf" in h.lower() and h not in seen and PDF_RE.search(t + " " + h):
            seen.add(h)
            out.append((t, h))
    return out[:MAX_PDFS]


def read_pdf_text(url: str) -> str:
    """PDF の本文（大きさ・ページ数に上限）。取れなければ空文字。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        r = _get(url, stream=True)
        if r.status_code != 200:
            return ""
        if int(r.headers.get("content-length") or 0) > PDF_MAX_BYTES:
            return ""
        buf = r.raw.read(PDF_MAX_BYTES + 1, decode_content=True)
        if len(buf) > PDF_MAX_BYTES:
            return ""
        reader = PdfReader(io.BytesIO(buf))
        text = " ".join((p.extract_text() or "") for p in reader.pages[:PDF_MAX_PAGES])
        return " ".join(text.split())[:PDF_TEXT_CHARS * 2]
    except Exception:
        return ""


def museum_links(m: dict) -> list[tuple[str, str]]:
    """館の指定ページと、展覧会関連のリンク先（ec.MAX_SUBPAGES 件まで）にあるリンク一覧。"""
    final, _, links = ec.fetch(m["url"])
    host = urlsplit(final).hostname or ""
    out, seen = list(links), {final}
    n = 0
    for t, h in links:
        if h in seen or (urlsplit(h).hostname or "") != host or not ec.LINK_RE.search(t):
            continue
        if re.search(r"過去|終了|アーカイブ|past|archive|pdf$", t + h, re.I):
            continue
        seen.add(h)
        out.extend(ec.fetch(h)[2])
        n += 1
        if n >= ec.MAX_SUBPAGES:
            break
    return out


_NAME_STRIP = re.compile(r"特別展|企画展|特別陳列|開館記念|令和\d+年度|[\s　「」『』【】〈〉《》()（）\[\]・、。,.:：!！?？~〜～\-－—―]")


def name_core(s: str) -> str:
    return _NAME_STRIP.sub("", s or "")


def find_detail_url(entry: dict, links: list[tuple[str, str]]) -> str:
    """館の一覧ページのリンクから、展覧会名が合う個別ページを探す。見つからなければ空文字。"""
    core = name_core(entry["name"])
    if len(core) < 4:
        return ""
    skip = {entry.get("list_url", "")} | set(entry.get("rejected_urls", []))
    for t, h in links:
        lt = name_core(t)
        if h in skip or h.lower().split("?")[0].endswith(".pdf") or len(lt) < 4:
            continue
        if core in lt or (len(lt) >= 8 and len(lt) >= len(core) * 0.6 and lt in core):
            return h
    return ""


# ---------------------------------------------------------------- 判定

def nospace(s: str) -> str:
    return re.sub(r"\s|[　・、。,]", "", s or "")


def analyze(client, usage, entry: dict, page: dict, pdfs: list[tuple[str, str, str]]) -> dict | None:
    """詳細ページの内容を軽いモデルで判定する。pdfs は (リンク文字, URL, 本文)。"""
    import curate_news

    flyer_links = [(t, h) for t, h in page["links"] if FLYER_RE.search(t + " " + h)][:15]
    user = (
        f"## 展覧会情報\n展覧会: {entry['name']}\n会場: {entry['venue']}\n会期: {entry['start']}〜{entry.get('end') or '?'}\n"
        f"ページ URL: {page['url']}\n\n## ページ本文\n{page['text'][:PAGE_TEXT_CHARS]}\n\n"
        "## 画像一覧（URL / alt）\n" + ("\n".join(f"- {s} / {a}" for s, a in page["images"]) or "（なし）")
        + "\n\n## チラシ・ポスター・PDF らしいリンク（文字 / URL）\n"
        + ("\n".join(f"- {t} / {h}" for t, h in flyer_links) or "（なし）")
        + "".join(f"\n\n## リンク先の PDF「{t}」の本文\n{x[:PDF_TEXT_CHARS] or '（本文を取り出せませんでした）'}"
                  for t, _, x in pdfs)
    )
    return curate_news.call_json(client, usage, ANALYZE_SYSTEM, user, ANALYZE_SCHEMA,
                                 effort="low", model=ec.LIGHT_MODEL)


def work_label(w: dict) -> str:
    return f"{w['temple']} {w['name']}".strip()


def work_key(w) -> str:
    return nospace(work_label(w) if isinstance(w, dict) else w)


def same_work(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 6 and (a in b or b in a))


def verified_works(res: dict, corpus: str) -> list[dict]:
    """AI が挙げた作品のうち、作品名が本文・PDF に実際に書かれているものだけ（重複は除く）。"""
    corpus = nospace(corpus)
    out, keys = [], []
    for w in res.get("works", []):
        name = nospace(w.get("name", ""))
        if len(name) < 3 or name not in corpus:
            continue
        w = {"temple": w.get("temple", "").strip(), "name": w["name"].strip()}
        k = work_key(w)
        if any(same_work(k, o) for o in keys):
            continue
        keys.append(k)
        out.append(w)
    return out


def new_works(works: list[dict], known: list[str]) -> list[dict]:
    kk = [work_key(k) for k in known]
    return [w for w in works if not any(same_work(work_key(w), k) for k in kk)]


def decide(entry: dict, res: dict, works: list[dict]) -> tuple[list[str], list[dict]]:
    """判定結果から、今回新しく届いた段階（重要な順）と、その出品作品を決める。"""
    stages = entry["stages"]
    fresh = new_works(works, entry.get("works_seen", []))
    reached = []
    if res.get("has_lineup") and works:
        if "lineup" not in stages:
            reached.append("lineup")
        elif len(new_works(works, entry.get("works", []))) >= LINEUP_REPOST_MIN:
            reached.append("lineup")
    if res.get("has_visual") and "visual" not in stages:
        reached.append("visual")
    if (res.get("is_dedicated_page") or reached) and "page" not in stages:
        reached.append("page")
    order = {s: i for i, s in enumerate(STAGE_PRIORITY)}
    reached.sort(key=lambda s: order[s])
    return reached, fresh


# ---------------------------------------------------------------- 配信候補

def watch_extra(entry: dict, reached: list[str], works: list[dict], today: date, repost: bool) -> str:
    start = kc.parse_date(entry["start"])
    left = (start - today).days if start else 0
    lines = [
        f"これは、開幕前の展覧会に新しい情報が出たことを知らせる投稿です（今日は {today:%Y-%m-%d}、開幕まであと{left}日）。",
        f"展覧会: {entry['name']} / 会場: {entry['venue']} / 会期: {entry['start']}〜{entry.get('end') or '?'}",
        "今回新しく分かったこと: " + "、".join(STAGE_LABEL[s] for s in reached) + ("（出品作品の追加）" if repost else ""),
        "会期・会場を必ず入れる。新しく分かったことを中心に書き、すでに知られている開催情報の繰り返しは短くする。",
    ]
    if "lineup" in reached:
        lines.append(
            "出品作品は、次の一覧にある名前だけを使う（情報源に書かれた範囲。書かれていない作品を足さない・推測しない）:\n"
            + "\n".join(f"- {work_label(w)}" for w in works[:12])
            + "\n作品名と寺院名は一覧どおりに書く。運慶・快慶などの作者名は、一覧と情報源に書かれたとおりにだけ書き、"
              "「伝」とあれば必ず「伝」を付ける。作者を断定しない。数字は算用数字を使う。"
        )
    if "visual" in reached:
        lines.append("ポスター・チラシが公開されたことに触れ、画像から読み取れることを断定せずに書く。画像はこの投稿に添付される。")
    if reached == ["page"]:
        lines.append("専用ページが公開されたことを知らせる。ページに書かれた範囲の情報だけを使う。")
    lines.append("「行くならいま」「必見」のような決まり文句や、抽象的な感想語に頼らない。")
    return "\n".join(lines)


def score_candidate(client, usage, entry: dict, header: str, description: str, data: dict, today: date) -> dict | None:
    """続報を、ニュースと同じ基準で採点する。成功したら "ai" の辞書。失敗は None。"""
    import curate_news

    now = datetime.now(JST)
    week_ago = (now - timedelta(days=7)).isoformat()
    # 重複判定の材料: 直近の、ほかの展覧会の続報（同じ展覧会の過去の配信候補は材料にしない）
    known = [it for it in data["items"]
             if it.get("source") == SOURCE and it.get("fetched_at", "") >= week_ago
             and (it.get("ai") or {}).get("post_header") in HEADERS_BY_STAGE.values()
             and (it["ai"].get("score", 0) >= CANDIDATE_THRESHOLD) and not it["ai"].get("duplicate")
             and not it.get("id", "").startswith(f"watch-{entry['id']}-")]
    start = kc.parse_date(entry["start"])
    item = {
        "id": f"watch-{entry['id']}-{header}",
        "title": f"{header.strip('【】')} {entry['name']}（{entry['venue']}、{entry['start']}〜{entry.get('end') or '?'}）",
        "description": f"開幕前の展覧会の続報（開幕まであと{(start - today).days}日）。{description}",
        "url": entry["detail_url"],
        "source": "exhibit_calendar",
        "published_at": now.isoformat(timespec="seconds"),
    }
    curate_news.score_items(client, usage, [item], known)
    return item.get("ai")


def make_candidate(client, usage, entry: dict, reached: list[str], works: list[dict], res: dict,
                   page: dict, data: dict, today: date, repost: bool, dry_run: bool, topic: str) -> dict | None:
    """続報の配信候補を news.json の形で作る。採点が基準未満・失敗のときは None。"""
    import curate_news

    header = HEADERS_BY_STAGE[reached[0]]
    summary = "、".join(STAGE_LABEL[s] for s in reached)
    description = f"新しく分かったこと: {summary}。"
    if works:
        description += " 出品作品: " + "、".join(work_label(w) for w in works[:12]) + "。"
    ai = score_candidate(client, usage, entry, header, description, data, today)
    if not ai or ai["duplicate"] or ai["score"] < CANDIDATE_THRESHOLD:
        print(f"  配信候補にせず記録のみ: {entry['name']} {header} "
              f"{'採点失敗' if not ai else str(ai['score']) + '点' + ('・重複' if ai['duplicate'] else '')}")
        return None
    now = datetime.now(JST)
    title = f"{entry['venue']} {entry['name']}"
    text = curate_news.write_post(
        client, usage, {"url": entry["detail_url"], "title": title, "description": description},
        header=header, extra=watch_extra(entry, reached, works, today, repost), fetch_body=False)
    if not text:
        return None
    image = res.get("visual_image_url", "") if "visual" in reached else ""
    n = len(entry.get("lineup_posts", [])) + 1 if "lineup" in reached else 0
    item = {
        "id": f"watch-{entry['id']}-" + ("lineup" + (str(n) if n > 1 else "") if "lineup" in reached else reached[0]),
        "title": title,
        "url": entry["detail_url"],
        "source": SOURCE,
        "header": header,
        "hashtags": "",
        "fetched_at": now.isoformat(timespec="seconds"),
        "published_at": now.isoformat(timespec="seconds"),
        "image_url": image,
        "ai": {
            "score": ai["score"],
            "category": ai.get("category") or "展覧会",
            "duplicate": False,
            "reason": f"展覧会の続報（{summary}）・{ai['reason']}",
            "post_text": text,
            "post_header": header,
            "post_format": curate_news.POST_FORMAT,
        },
    }
    if not dry_run:
        data["items"].insert(0, item)
        if topic:
            curate_news.notify(topic, item)
            item["ai"]["notified"] = True
    return item


# ---------------------------------------------------------------- 実行

def run(watch: dict, data: dict, today: date, client, usage, museums: list[dict], *,
        dry_run: bool = False, topic: str = "", fetch_page=None, links_of=None, read_pdf=None) -> dict:
    """監視リストを 1 周する。watch と data は呼び出し元が保存する（このなかでは書かない）。"""
    fetch_page = fetch_page or fetch_detail
    links_of = links_of or museum_links
    read_pdf = read_pdf or read_pdf_text
    stats = {"entries": len(watch["entries"]), "no_detail": 0, "failed": 0, "same": 0, "checked": 0,
             "found": 0, "made": 0, "lineup": 0, "visual": 0, "page": 0, "deferred": 0}
    link_cache: dict[str, list] = {}
    now_iso = datetime.now(JST).isoformat(timespec="seconds")

    for entry in sorted(watch["entries"].values(), key=lambda e: e["start"]):
        discovered = False
        if not entry.get("detail_url"):
            m = find_museum(entry["venue"], museums)
            if not m:
                stats["no_detail"] += 1
                continue
            if m["url"] not in link_cache:
                link_cache[m["url"]] = links_of(m)
            url = find_detail_url(entry, link_cache[m["url"]])
            if not url:
                stats["no_detail"] += 1
                continue
            entry["detail_url"], discovered = url, True
            stats["found"] += 1
            print(f"  詳細ページを発見: {entry['name']} → {url}")

        page = fetch_page(entry["detail_url"])
        if not page:
            stats["failed"] += 1
            if discovered:
                entry["detail_url"] = ""
            continue
        digest = content_hash(page)
        if digest == entry.get("page_hash") and not discovered:
            stats["same"] += 1
            continue
        if not client:
            print(f"  変化あり（AI なし）: {entry['name']} 画像{len(page['images'])}枚 PDF候補{len(doc_links(page))}件 {page['url']}")
            continue
        if stats["checked"] >= MAX_CHECKS_PER_RUN:
            stats["deferred"] += 1
            continue
        stats["checked"] += 1

        pdfs = [(t, h, read_pdf(h)) for t, h in doc_links(page)]
        res = analyze(client, usage, entry, page, pdfs)
        if res is None:
            stats["failed"] += 1  # 判定に失敗したら、ハッシュを更新せず翌日にやり直す
            continue
        if not res.get("matches_exhibition"):
            entry["page_hash"] = digest
            if discovered:  # 探して見つけたページが別の展覧会だった。次回は別の候補を探す
                entry["rejected_urls"] = entry.get("rejected_urls", []) + [entry["detail_url"]]
                entry["detail_url"] = ""
            continue

        corpus = page["text"] + " " + " ".join(x for _, _, x in pdfs)
        works = verified_works(res, corpus)
        reached, fresh = decide(entry, res, works)
        repost = "lineup" in reached and "lineup" in entry["stages"]
        item = None
        if reached:
            if stats["made"] >= MAX_CANDIDATES_PER_RUN:
                stats["deferred"] += 1
                continue  # ハッシュを更新しないので、翌日にもう一度判定する
            item = make_candidate(client, usage, entry, reached, works, res, page, data, today, repost,
                                  dry_run, topic)
            if item:
                stats["made"] += 1
            for s in reached:
                stats[s] += 1
                entry["stages"].setdefault(s, {"at": now_iso, "candidate": ""})
                if item:
                    entry["stages"][s]["candidate"] = item["id"]
            if "lineup" in reached:
                entry.setdefault("lineup_posts", []).append(item["id"] if item else "")
                entry["works"] = [work_label(w) for w in works]
            print(f"  {'候補を作成' if item else '記録のみ'}: {entry['name']} {[HEADERS_BY_STAGE[s] for s in reached]}"
                  + (f"\n    {item['ai']['post_text']}" if item else ""))
        entry["works_seen"] = sorted(set(entry.get("works_seen", [])) | {work_label(w) for w in works})
        entry["page_hash"] = digest
        entry["last_checked"] = now_iso
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ファイルに書かず、結果だけ表示する")
    args = ap.parse_args()
    now = datetime.now(JST)
    today = now.date()
    museums = ec.load_json(ec.SOURCES_FILE, {}).get("museums", [])
    watch, cal, data = load_watch(), kc.load_calendar(), load_news_data()
    seeded = seed_from_calendar(watch, cal, today, museums)
    dropped = prune(watch, today)

    client = usage = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        from curate_news import Usage

        client, usage = anthropic.Anthropic(), Usage()
    else:
        print("::warning::ANTHROPIC_API_KEY が無いため、詳細ページの判定（AI）をスキップ", file=sys.stderr)

    topic = "" if args.dry_run else os.environ.get("NTFY_TOPIC", "").strip()
    if args.dry_run:
        print(f"監視リスト {len(watch['entries'])}件（今回追加 {seeded}・開幕で除外 {dropped}）")
        for e in sorted(watch["entries"].values(), key=lambda e: e["start"]):
            print(f"  - {e['start']} {e['venue']} {e['name']} 詳細={e.get('detail_url') or '未発見'}")
    stats = run(watch, data, today, client, usage, museums, dry_run=args.dry_run, topic=topic)
    if not args.dry_run:
        save_news_data(data)
        save_watch(watch)
    cost = usage.cost() if usage else 0.0
    print(f"::notice::展覧会ウォッチ 対象{stats['entries']}件（新規{seeded}・開幕で除外{dropped}）/ "
          f"詳細ページ未発見{stats['no_detail']}・新規発見{stats['found']}・取得失敗{stats['failed']}・変更なし{stats['same']}・"
          f"AI判定{stats['checked']}件 / 配信候補{stats['made']}件（出品作品{stats['lineup']}・ビジュアル{stats['visual']}・"
          f"ページ{stats['page']}段階）持ち越し{stats['deferred']} / AI費用 約${cost:.3f}（上限見積り）"
          + ("（dry-run: 書き込みなし）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
