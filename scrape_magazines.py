"""雑誌の「仏像特集号」を見つけて docs/data/news.json の書籍欄（item_type=book, book_kind=magazine）に加える。

「サライ 2026年12月号」のように号の題名に「仏像」が入らない雑誌は、scrape_amazon.py の
キーワード検索（k=仏像）では見つからない。そこで 2 つの入口から候補を集める。

  (b) Fujisan（雑誌の定期購読サイト）の雑誌ごとの商品ページ
      最新号の目次と次号予告が 1 ページに載っている。robots.txt で禁止されていない
      /product/<id>/ だけを、誌名の一覧（MAGAZINES）× 1 回ずつ、間隔を空けて読む。
  (a) news.json のニュース（Google ニュース・公式サイト）に、雑誌の発売告知が出ていないか
      （プレスリリースが「サライ 12 月号は奈良の御仏が大特集」のように報じる）

候補は「仏像らしい語」を含むものだけを AI（curate_news.call_json）に渡し、特集の中心が
仏像・寺院の仏像かどうかを判定させる。判定が通った号は Amazon を「誌名 号」で検索して商品ページ（ASIN）を探し、
アソシエイトタグ付きのリンクにする。見つからなければ Fujisan の商品ページ（タグなし）にする。

Amazon の検索は、仏像特集と判定された号 1 件につき 1 回だけ。Fujisan は誌名ごとに 1 回（間隔 3 秒）。
ANTHROPIC_API_KEY が無いときは何もしない。
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from news_store import BOOK_KIND_MAGAZINE, BOOK_HEADER_MAGAZINE, JST_TZ, trim_per_source
from scrape_amazon import (REQUEST_HEADERS, REQUEST_TIMEOUT, SOURCE_AMAZON, build_product_url, fetch_page)

# 見る雑誌: (誌名, Fujisan の商品 ID)。誌名は Amazon の題名の先頭と照合するのにも使う。
# 一個人（休刊）、日経おとなの OFF（最新号が古い）は除いた。足すときは Fujisan の商品ページ番号を調べる。
MAGAZINES: list[tuple[str, str]] = [
    ("サライ", "1003"),
    ("芸術新潮", "692"),
    ("和樂", "5834"),
    ("男の隠れ家", "308"),
    ("目の眼", "2629"),
    ("歴史人", "1281690942"),
    ("婦人画報", "2289"),
    ("家庭画報", "390"),
    ("時空旅人", "1281693070"),
    ("散歩の達人", "1281679558"),
    ("ひととき", "1281695974"),
]
MAGAZINE_NAMES = [n for n, _ in MAGAZINES] + ["一個人", "日経おとなのOFF", "週刊朝日", "BRUTUS", "Pen", "美術手帖", "旅の手帖"]

# Amazon 起点で見る雑誌（Fujisan の一覧に加えて）。各誌の新しい号を Amazon の雑誌カテゴリで探し、
# 題名と商品ページの説明（特集・目次）に仏像らしい語があれば AI に判定させる
AMAZON_EXTRA_MAGAZINES = ["BRUTUS", "Pen", "一個人", "美術手帖", "旅の手帖", "大人の休日倶楽部"]
AMAZON_MAGAZINE_NODE = "13384021"   # Amazon.co.jp の「雑誌」
AMAZON_ISSUES_PER_MAGAZINE = 2      # 1 誌あたり見る号（新しい順）
AMAZON_ISSUE_WINDOW_DAYS = 45       # 発売からこの日数以内の号と、発売前の号だけ見る

FUJISAN_URL = "https://www.fujisan.co.jp/product/{pid}/"
SLEEP_FUJISAN = 3
SLEEP_AMAZON = 4

# 目次・次号予告・ニュースに、これらがあれば「仏像特集かもしれない」候補として AI に渡す
BUTSUZO_WORDS = ("仏像", "御仏", "仏教美術", "仏師", "秘仏", "観音", "如来", "菩薩", "明王", "大仏", "運慶", "快慶",
                 "古寺", "仏閣", "阿修羅", "国宝仏", "仏", "寺")
# 上のうち「寺」「仏」だけの一般語を除く、強い語。これが無い候補（寺の旅行特集など）は AI に渡す前に落とす
STRONG_WORDS = tuple(w for w in BUTSUZO_WORDS if w not in ("仏", "寺"))

NEWS_WINDOW_DAYS = 21       # この日数以内に取り込んだニュースだけ見る
MAX_NEWS_CANDIDATES = 15
LATEST_MAX_AGE_DAYS = 120   # 発売からこの日数を超えた号は「新刊」として加えない
SCHEMA_RESULT = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "is_butsuzo_special": {"type": "boolean"},
                    "scope": {"type": "string", "enum": ["main", "part", "none"]},
                    "magazine": {"type": "string"},
                    "issue_label": {"type": "string"},
                    "release_date": {"type": "string"},
                    "special_title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "is_butsuzo_special", "scope", "magazine", "issue_label",
                             "release_date", "special_title", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """あなたは仏像専門のコミュニティ「仏像リンク」の編集者です。雑誌の号の目次・次号予告・
発売ニュースの文章から、その号に「仏像の特集」があるかを判定してください。

# 仏像特集とみなす（is_butsuzo_special = true）
- scope = "main": 大特集・巻頭特集の中心が、仏像・御仏・仏像のある寺院・仏教美術・仏師（運慶・快慶など）
- scope = "part": 特集の一部（第 1 部、第 2 特集、連載の回）が仏像・御仏を中心に扱う

# 仏像特集とみなさない（is_butsuzo_special = false, scope = "none"）
- 寺や神社の旅行・参拝・御朱印・庭園・建築・歴史の特集で、仏像が主役ではないもの
- 美術全般・骨董・和食・鉄道などで、仏像が触れられていないもの
- 「寺」「仏」の語がたまたま入っただけ（寺町、仏間、地名など）

# 書き方
- magazine: 誌名（例: サライ）。issue_label: 号の表記（例: 2026年12月号）。文章に無ければ空文字
- release_date: 発売日（YYYY-MM-DD）。文章に無ければ空文字。推測しない
- special_title: 仏像に関わる特集の見出しを、文章にある表記のまま 60 字以内（例: 大特集 古代史と御仏の「奈良」をゆく）
- summary: 仏像に関わる部分で何を扱うかを、文章に書かれている範囲だけで 120 字以内。載っていない寺や像を足さない
- 判定できない・仏像と関係なければ false にして、special_title と summary は空文字"""


# ── Fujisan の商品ページ（雑誌 1 誌ぶん）を読む ──

_ISSUE_RE = re.compile(r"(最新号|未来号)\s*\n\s*(.+?)\s*\n\s*発売日:\s*(\d{4})/(\d{2})/(\d{2})")


def parse_fujisan_page(html: str) -> dict:
    """Fujisan の雑誌ページから、最新号（label, date）・次号（label, date）・目次・次号予告を取り出す。
    取れなかった項目は空文字（None）。"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    out = {"latest_label": "", "latest_date": "", "next_label": "", "next_date": "", "mokuji": "", "preview": "",
           "monthly_day": ""}
    m = re.search(r"発行間隔\n月刊\n発売日\n毎月(\d{1,2})日", text)
    if m:
        out["monthly_day"] = m.group(1)
    seen_latest = seen_next = False
    for m in _ISSUE_RE.finditer(text):
        kind, label, y, mo, d = m.group(1), m.group(2).strip(), m.group(3), m.group(4), m.group(5)
        if kind == "最新号" and not seen_latest:
            out["latest_label"], out["latest_date"] = label, f"{y}-{mo}-{d}"
            seen_latest = True
        elif kind == "未来号" and not seen_next:
            out["next_label"], out["next_date"] = label, f"{y}-{mo}-{d}"
            seen_next = True
    flat = " ".join(text.split("\n"))
    # 目次: 「参考価格 … 目次 <本文> 目次配信サービス」
    i = flat.find("参考価格")
    if i < 0:
        i = flat.find("発行間隔")   # 参考価格が載っていない雑誌（芸術新潮など）
    j = flat.find("目次", i) if i >= 0 else -1
    k = flat.find("目次配信サービス", j) if j >= 0 else -1
    if j >= 0:
        out["mokuji"] = flat[j + 2:(k if k > j else j + 1500)][:1500].strip()
    # 次号予告（ページ下部の「次号予告 次号予告 …」）
    p = flat.rfind("次号予告")
    if p >= 0:
        out["preview"] = flat[p + 4:p + 4 + 900].replace("次号予告", "", 1).strip()
    return out


def has_butsuzo_words(text: str) -> bool:
    """AI に渡す前の足切り。「仏像」「御仏」「観音」など強い語がひとつでもあるか。"""
    return any(w in text for w in STRONG_WORDS)


# ── ニュースから候補を拾う ──

_ISSUE_IN_NEWS = re.compile(r"\d{4}年\s*\d{1,2}(?:・\d{1,2})?月号|\d{1,2}月号|最新号|増刊|ムック|号が発売|発売")


def news_candidates(items: list[dict], now: datetime | None = None) -> list[dict]:
    """news.json から「雑誌の発売告知で、仏像らしい語を含む」記事を拾う（未判定のものだけ）。"""
    now = now or datetime.now(JST_TZ)
    cutoff = now - timedelta(days=NEWS_WINDOW_DAYS)
    out = []
    for it in items:
        if it.get("source") not in ("google_news", "official_site") or it.get("mag_checked"):
            continue
        try:
            fetched = datetime.fromisoformat(it.get("fetched_at", ""))
            if fetched < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        text = f"{it.get('title', '')} {it.get('description', '')}"
        is_mag = ("雑誌" in text or any(n in text for n in MAGAZINE_NAMES)) and _ISSUE_IN_NEWS.search(text)
        if is_mag and has_butsuzo_words(text):
            out.append(it)
    return out[:MAX_NEWS_CANDIDATES]


# ── Amazon で号の商品ページ（ASIN）を探す ──

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def find_amazon_match(cards: list[dict], magazine: str, issue_label: str) -> dict | None:
    """検索結果（{asin, title, image_url, release_date}）から、誌名で始まり号の表記を含む商品を返す。
    電子版（題名が「[雑誌]」付き）と紙の雑誌が並ぶときは、紙の雑誌を優先する。"""
    name, label = _norm(magazine), _norm(issue_label)
    hits = [c for c in cards if _norm(c.get("title", "")).startswith(name) and label and label in _norm(c.get("title", ""))]
    paper = [c for c in hits if "[雑誌]" not in c.get("title", "") and "Kindle" not in c.get("title", "")]
    return (paper or hits or [None])[0]


def parse_amazon_cards(soup: BeautifulSoup) -> list[dict]:
    """Amazon の検索結果ページから商品を {asin, title, image_url, release_date} で取り出す（題名の絞り込みはしない）。"""
    cards = soup.find_all("div", attrs={"data-component-type": "s-search-result", "data-asin": True})
    out = []
    for card in cards:
        asin = card.get("data-asin", "").strip()
        h2 = card.find("h2")
        if not asin or not h2:
            continue
        img = card.find("img", class_="s-image")
        release = ""
        for sp in card.find_all("span", class_="a-text-normal"):
            t = sp.get_text(strip=True)
            m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", t)
            if m:
                release = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break
        out.append({"asin": asin, "title": h2.get_text(strip=True),
                    "image_url": (img.get("src", "") if img else ""), "release_date": release})
    return out


def search_amazon_issue(session: requests.Session, magazine: str, issue_label: str) -> dict | None:
    url = f"https://www.amazon.co.jp/s?k={quote(magazine + ' ' + issue_label)}&i=stripbooks"
    soup = fetch_page(session, url)
    if not soup:
        print(f"  Amazon 検索: ページを取得できませんでした（{magazine} {issue_label}）", file=sys.stderr)
        return None
    cards = parse_amazon_cards(soup)
    hit = find_amazon_match(cards, magazine, issue_label)
    if not hit:
        print(f"  Amazon 検索: 一致なし（{magazine} {issue_label}）カード {len(cards)} 件・上位: "
              + " / ".join(c["title"][:25] for c in cards[:3]), file=sys.stderr)
    return hit


# ── 書籍欄に加える項目を作る ──

def magazine_key(magazine: str, issue_label: str) -> str:
    return f"{_norm(magazine)}|{_norm(issue_label)}"


def build_magazine_item(magazine: str, issue_label: str, release_date: str, special_title: str, summary: str,
                        amazon: dict | None, fallback_url: str, now: datetime | None = None) -> dict:
    """書籍欄（item_type=book）の雑誌項目。Amazon の ASIN が分かればアソシエイトタグ付きのリンク、
    分からなければ出版社・Fujisan のページ（タグなし）。"""
    now = now or datetime.now(JST_TZ)
    title = f"{magazine} {issue_label}"
    asin = (amazon or {}).get("asin", "")
    url = build_product_url(asin) if asin else fallback_url
    rd = release_date or (amazon or {}).get("release_date", "")
    desc = special_title if not summary else f"{special_title}。{summary}"
    uid = hashlib.md5(("magazine:" + magazine_key(magazine, issue_label)).encode()).hexdigest()[:12]
    return {
        "id": uid,
        "title": title,
        "url": url,
        "asin": asin,
        "source": SOURCE_AMAZON,
        "item_type": "book",
        "book_kind": BOOK_KIND_MAGAZINE,
        "mag_key": magazine_key(magazine, issue_label),
        "header": BOOK_HEADER_MAGAZINE,
        "hashtags": "#仏像 #雑誌",
        "image_url": (amazon or {}).get("image_url", ""),
        "published_at": (datetime.fromisoformat(rd).replace(tzinfo=JST_TZ).isoformat() if rd else now.isoformat()),
        "release_date": rd,
        "fetched_at": now.isoformat(),
        "description": desc[:300],
    }


MAX_AMAZON_TRIES = 10   # GitHub Actions からの Amazon 検索は弾かれる回が多いので、多めに再試行する


def upgrade_missing_asin(session: requests.Session, items: list[dict], have_asin: set[str]) -> int:
    """Amazon の商品ページが見つからず出版社・Fujisan のリンクのままの雑誌を、再度 Amazon で探して差し替える
    （発売直後は Amazon の検索に載らないことがある）。1 冊につき MAX_AMAZON_TRIES 回まで。"""
    n = 0
    for it in items:
        if it.get("book_kind") != BOOK_KIND_MAGAZINE or it.get("asin") or it.get("mag_amazon_tries", 0) >= MAX_AMAZON_TRIES:
            continue
        magazine, _, label = (it.get("title", "")).partition(" ")
        it["mag_amazon_tries"] = it.get("mag_amazon_tries", 0) + 1
        time.sleep(SLEEP_AMAZON)
        hit = search_amazon_issue(session, magazine, label)
        if hit and hit["asin"] not in have_asin:
            it["asin"], it["url"] = hit["asin"], build_product_url(hit["asin"])
            it["image_url"] = it.get("image_url") or hit.get("image_url", "")
            have_asin.add(hit["asin"])
            n += 1
            print(f"Amazon の商品ページに差し替え: {it['title']} → {hit['asin']}")
    return n


# ── AI で判定 ──

def judge(client, usage, entries: list[dict]) -> dict[str, dict]:
    """entries: [{id, hint, text}]。判定結果を id で引ける dict にして返す。失敗時は空。"""
    import json

    from curate_news import call_json

    if not entries:
        return {}
    payload = json.dumps(entries, ensure_ascii=False, indent=1)
    res = call_json(client, usage, JUDGE_SYSTEM, payload, SCHEMA_RESULT, effort="low")
    return {r["id"]: r for r in (res or {}).get("results", []) if r.get("id")}


# ── main ──

def collect_fujisan(session: requests.Session, today) -> list[dict]:
    """各誌の Fujisan ページを読み、AI に渡す候補 [{id, hint, text, ...}] を返す。"""
    entries = []
    for i, (name, pid) in enumerate(MAGAZINES):
        if i:
            time.sleep(SLEEP_FUJISAN)
        url = FUJISAN_URL.format(pid=pid)
        try:
            r = session.get(url, headers=REQUEST_HEADERS | {"Referer": "https://www.fujisan.co.jp/"}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"  Fujisan 取得失敗 ({name}): {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            print(f"  Fujisan HTTP {r.status_code} ({name})", file=sys.stderr)
            continue
        info = parse_fujisan_page(r.text)
        if not info["latest_label"] and info["monthly_day"]:
            # 芸術新潮のように最新号の号名がページに無い月刊誌: 「毎月◯日発売」から最新号を割り出す
            # （その日を過ぎていれば今月発売の号。号名は発売月の翌月号とする、月刊誌のふつうの付け方）
            day = int(info["monthly_day"])
            y, mo = today.year, today.month
            if today.day < day:
                y, mo = (y - 1, 12) if mo == 1 else (y, mo - 1)
            ny, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
            info["latest_label"], info["latest_date"] = f"{ny}年{nmo}月号", f"{y}-{mo:02d}-{day:02d}"
        if info["latest_label"] and info["mokuji"] and has_butsuzo_words(info["mokuji"]):
            age_ok = True
            try:
                age_ok = (today - datetime.strptime(info["latest_date"], "%Y-%m-%d").date()).days <= LATEST_MAX_AGE_DAYS
            except ValueError:
                pass
            if age_ok:
                entries.append({"id": f"fj-{pid}-latest", "magazine": name, "issue_label": info["latest_label"],
                                "release_date": info["latest_date"], "page": url, "text": info["mokuji"]})
        if info["next_label"] and info["preview"] and has_butsuzo_words(info["preview"]):
            entries.append({"id": f"fj-{pid}-next", "magazine": name, "issue_label": info["next_label"],
                            "release_date": info["next_date"], "page": url, "text": info["preview"]})
    return entries


_ISSUE_IN_TITLE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?\s*号")


def issue_label_from_title(title: str) -> str:
    """「サライ 2026年 12月号 [雑誌]」→「2026年12月号」、「BRUTUS 2026年12月01日号」→「2026年12月1日号」。"""
    t = unicodedata.normalize("NFKC", title)
    m = _ISSUE_IN_TITLE.search(t)
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), m.group(3)
    return f"{y}年{mo}月{int(d)}日号" if d else f"{y}年{mo}月号"


def _starts_with_magazine(title: str, name: str) -> bool:
    t, n = _norm(title).lower(), _norm(name).lower()
    # 「Penguin」のように誌名の後ろに英字が続くものは別の誌名（数字・記号・日本語なら同じ誌）
    return t.startswith(n) and (len(t) == len(n) or not (t[len(n)].isascii() and t[len(n)].isalpha()))


def amazon_description(session: requests.Session, asin: str) -> str:
    """商品ページの説明（雑誌は特集・目次が書かれていることが多い）。取れなければ空。"""
    soup = fetch_page(session, f"https://www.amazon.co.jp/dp/{asin}")
    if not soup:
        return ""
    parts = []
    for sel in ("#bookDescription_feature_div", "#productDescription", "#editorialReviews_feature_div"):
        el = soup.select_one(sel)
        if el:
            parts.append(el.get_text(" ", strip=True).replace("続きを読む", ""))
    return " ".join(parts)[:1500]


def collect_amazon(session: requests.Session, today, skip_asins: set[str], seen: list[str]) -> list[dict]:
    """各誌の新しい号を Amazon の雑誌カテゴリで探し、仏像らしい語がある号を AI に渡す候補にする。
    一度見た ASIN（seen。data["mag_az_seen"] に残す）は商品ページを読み直さない。"""
    names = [n for n, _ in MAGAZINES] + AMAZON_EXTRA_MAGAZINES
    entries = []
    for name in names:
        time.sleep(SLEEP_AMAZON)
        url = (f"https://www.amazon.co.jp/s?k={quote(name)}&i=stripbooks"
               f"&rh=n%3A{AMAZON_MAGAZINE_NODE}&s=date-desc-rank")
        soup = fetch_page(session, url)
        if not soup:
            print(f"  Amazon 雑誌検索: 取得できませんでした（{name}）", file=sys.stderr)
            continue
        picked = 0
        for c in parse_amazon_cards(soup):
            if picked >= AMAZON_ISSUES_PER_MAGAZINE:
                break
            if not _starts_with_magazine(c["title"], name):
                continue
            label = issue_label_from_title(c["title"])
            if not label:
                continue
            try:
                age = (today - datetime.strptime(c["release_date"], "%Y-%m-%d").date()).days
            except ValueError:
                age = 0
            if age > AMAZON_ISSUE_WINDOW_DAYS:
                break   # 新しい順なので、ここから先は古い号
            picked += 1
            if c["asin"] in skip_asins or c["asin"] in seen:
                continue
            seen.append(c["asin"])
            text = c["title"]
            if not has_butsuzo_words(text):
                time.sleep(SLEEP_AMAZON)
                text = f"{c['title']} {amazon_description(session, c['asin'])}"
            if has_butsuzo_words(text):
                entries.append({"id": f"az-{c['asin']}", "magazine": name, "issue_label": label,
                                "release_date": c["release_date"], "page": build_product_url(c["asin"]),
                                "text": text, "amazon": c})
    return entries


def main() -> int:
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY が無いため雑誌の仏像特集を探しません")
        return 0
    import anthropic

    from curate_news import Usage
    from post_news import load_news_data, save_news_data

    client, usage = anthropic.Anthropic(), Usage()
    data = load_news_data()
    items = data["items"]
    today = datetime.now(JST_TZ).date()
    have = {it.get("mag_key") for it in items if it.get("book_kind") == BOOK_KIND_MAGAZINE}
    have_asin = {it.get("asin") for it in items if it.get("source") == SOURCE_AMAZON and it.get("asin")}

    session = requests.Session()
    try:   # Amazon の検索はクッキーが無いと弾かれやすいので、先にトップページを訪ねる（scrape_amazon.py と同じ）
        session.get("https://www.amazon.co.jp/", headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  Amazon トップページ訪問失敗（続行）: {e}", file=sys.stderr)
    upgraded = upgrade_missing_asin(session, items, have_asin)
    entries = collect_fujisan(session, today)
    # 判定に渡すもの: Fujisan の号（すでに加えた号は除く）と、ニュースの記事
    # 一度判定した号（仏像特集でなかった号も）は data["mag_judged"] に覚えて、毎回 AI に出さない
    judged_keys = set(data.get("mag_judged", []))
    entries = [e for e in entries
               if magazine_key(e["magazine"], e["issue_label"]) not in have | judged_keys]
    # Amazon 起点: 各誌の新しい号の題名・商品ページの説明から（Fujisan と同じ号は Fujisan 側を使う）
    fj_keys = {magazine_key(e["magazine"], e["issue_label"]) for e in entries}
    az_seen = list(data.get("mag_az_seen", []))
    for e in collect_amazon(session, today, have_asin, az_seen):
        k = magazine_key(e["magazine"], e["issue_label"])
        if k not in have and k not in judged_keys and k not in fj_keys:
            entries.append(e)
    data["mag_az_seen"] = az_seen[-400:]
    news = news_candidates(items)
    for it in news:
        entries.append({"id": f"news-{it['id']}", "magazine": "", "issue_label": "", "release_date": "",
                        "page": it.get("url", ""), "text": f"{it.get('title', '')} {it.get('description', '')}"})
    if not entries:
        print("仏像の言葉を含む雑誌の候補はありませんでした")
        save_news_data(data)   # ASIN の差し替えがあったときのため
        return 0
    n_az = sum(1 for e in entries if e["id"].startswith("az-"))
    print(f"判定に出す候補: Fujisan {len(entries) - len(news) - n_az} 件・Amazon {n_az} 件・ニュース {len(news)} 件")
    judged = judge(client, usage, [{"id": e["id"], "magazine": e["magazine"], "issue_label": e["issue_label"],
                                    "release_date": e["release_date"], "text": e["text"][:1200]} for e in entries])
    for it in news:
        it["mag_checked"] = True   # 判定済み。次回から見ない
    judged_keys |= {magazine_key(e["magazine"], e["issue_label"]) for e in entries if e["magazine"]}
    data["mag_judged"] = sorted(judged_keys)[-300:]

    added = 0
    hit_keys = set()
    for e in entries:
        r = judged.get(e["id"])
        if not r or not r.get("is_butsuzo_special") or r.get("scope") == "none":
            continue
        magazine = e["magazine"] or r.get("magazine", "")
        label = e["issue_label"] or r.get("issue_label", "")
        if not magazine or not label:
            print(f"  誌名・号が分からず見送り: {e['id']}", file=sys.stderr)
            continue
        key = magazine_key(magazine, label)
        if key in have or key in hit_keys:
            continue
        hit_keys.add(key)
        if e.get("amazon"):
            amazon = e["amazon"]   # Amazon 起点の候補は、見つけた商品をそのまま使う
        else:
            time.sleep(SLEEP_AMAZON)
            amazon = search_amazon_issue(session, magazine, label)
        if amazon and amazon["asin"] in have_asin:
            continue
        item = build_magazine_item(magazine, label, e["release_date"] or r.get("release_date", ""),
                                   r.get("special_title", ""), r.get("summary", ""), amazon,
                                   e["page"] if e["page"].startswith("http") else "")
        if not item["url"]:
            continue
        items.insert(0, item)
        if amazon:
            have_asin.add(amazon["asin"])
        added += 1
        print(f"追加 [雑誌]: {item['title']} / {item['description'][:50]} / {item['url']}")
    data["items"] = trim_per_source(items)
    save_news_data(data)
    print(f"::notice::雑誌の仏像特集 追加 {added} 件・判定費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
