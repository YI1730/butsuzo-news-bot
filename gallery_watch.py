"""名品ギャラリーウォッチ: 国立博物館 4 館の平常展示（常設）で、仏像・神像・仏教彫刻の展示が替わる節目を捉えて配信候補にする。
毎日 1 回の実行。展覧会ウォッチ（exhibit_watch.py）が特別展を担当するのに対し、こちらは平常展示の展示替えを担当する。

  東京国立博物館 : 本館・東洋館・法隆寺宝物館の展示ページ（hall）と「今週の東博コレクション展」から、展示作品リスト（item）を読む
  京都国立博物館 : 名品ギャラリーの月ごとのページ（先の月まで公開される）。彫刻室のテーマごとに期間と作品リストがある
  九州国立博物館 : 文化交流展示室の作品一覧（作品ごとの展示場所・展示期間）
  奈良国立博物館 : 名品展のページと、そこにリンクされた展示品一覧（PDF）

- 仏像（彫刻室・彫刻部門、または作品名）だけを残す。作品名で判断がつかないものだけ軽いモデルで判定し、判定は state にためる
- state は data/gallery_watch.json。館ごとに作品（館・作品名・所蔵者から作る id）と展示期間・通知済みの節目を持つ
- 節目（同じ作品の同じ節目は 2 度通知しない）:
    appear@<開始日> : 新しく展示される（先の日付で告知されたもの、または新しく展示中になったもの）
    ending@<終了日> : 展示の終了まで 7 日以内（国宝・重要文化財・注目の作品だけ）
- 配信候補（見出しは kaicho_calendar.GALLERY_HEADERS）:
    【展示替え】       館ごと・日ごとに 1 件。新しく展示される像をまとめる（国宝 > 重要文化財 > 注目 > 重要美術品 > 寄託の寺院像）
    【展示終了まぢか】 館ごと・日ごとに 1 件。注目の像の展示終了が近いとき
    【今週の展示替え】 指定のない・注目でもない展示替えは個別にせず、月曜に全館分を 1 件にまとめる
- 各館で初めて取得したとき（シード）は、記録だけして配信候補にしない（開始日が先の像だけは候補にする）
- 新しく展示される像は、館の公式ページ（作品ページ・テーマの解説・展示品一覧）の本文だけを材料に、特徴の短い説明を作る。
  資料に書かれていないことは書かせず、資料の引用で裏付けが取れない説明は捨てる
- 配信候補は curate_news.score_items（ニュースと同じ基準）で採点し、70 点以上のものだけ作る（週のまとめは固定点）
- 1 回の実行で AI に渡す特徴の抽出は 30 件まで。リクエストは 3 秒おき
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
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
STATE_FILE = BASE_DIR / "data" / "gallery_watch.json"
SOURCE = kc.SOURCE
HEADERS_BY_KIND = kc.GALLERY_HEADERS
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQUEST_INTERVAL = 3.0          # リクエストの間隔（秒）
ENDING_DAYS = 7                 # 展示の終了まで何日以内で「終了まぢか」にするか
CONTINUE_GAP_DAYS = 7           # 前の展示期間の終了から次の開始までがこの日数以内なら、続けて展示されているものとみなす
MAX_ENRICH_PER_RUN = 30         # 1 回の実行で AI に渡す特徴の抽出の上限（費用の歯止め）
SEARCH_VERSION = 3              # 検索の方式を変えたら上げる（空振りに終わった像を、もう一度だけ調べ直す）
MAX_SEARCHES_PER_RUN = 10       # 公式サイトの web 検索（Claude の web_search ツール）の上限（1 回の実行）
SEARCH_COST = 0.01              # web 検索 1 回あたりの費用の見積り（$）
SEARCH_DOMAINS = ["kyohaku.go.jp", "narahaku.go.jp", "tnm.jp", "kyuhaku.jp", "collection.kyuhaku.jp",
                  "online.bunka.go.jp", "bunka.nii.ac.jp", "colbase.nich.go.jp"]   # 公式な情報源だけ（寺院のサイトは使わない）
MAX_FETCH_PER_ITEM = 4          # 検索結果のページ（許可ドメインのみ）を、像ごとに自分で開く最大数
MAX_JUDGE_CALLS = 3             # 作品名で判断がつかないものを軽いモデルで判定する呼び出しの上限
JUDGE_BATCH = 40
MAX_LISTED = 8                  # 配信候補の材料に入れる像の数（重要な順）
DIGEST_SCORE = 70               # 週のまとめは機械的に集めたものなので、採点せずこの点にする
DIGEST_MAX_ITEMS = 40
DIGEST_KEEP_DAYS = 21           # まとめ待ちのまま、この日数を過ぎた（または展示が終わった）ものは捨てる
PRUNE_DAYS = 60                 # 展示が終わってこの日数たった作品は state から外す
SOURCE_CHARS = 5000             # 特徴の抽出に渡す資料の文字数
PDF_MAX_BYTES = 6 * 1024 * 1024
PDF_MAX_PAGES = 6

MUSEUMS = {
    "tnm": {"name": "東京国立博物館", "gallery": "東博コレクション展", "url": "https://www.tnm.jp/modules/r_exhibition/index.php?controller=change"},
    "kyohaku": {"name": "京都国立博物館", "gallery": "名品ギャラリー", "url": "https://www.kyohaku.go.jp/jp/exhibitions/collection/"},
    "narahaku": {"name": "奈良国立博物館", "gallery": "名品展", "url": "https://www.narahaku.go.jp/exhibition_cat/usual/"},
    "kyuhaku": {"name": "九州国立博物館", "gallery": "文化交流展示室", "url": "https://collection.kyuhaku.jp/exhibition/"},
}
MUSEUM_ORDER = ("tnm", "kyohaku", "narahaku", "kyuhaku")

# 生成文のルール（配信候補の指示と、特徴の抽出の結果の検査に使う）
BANNED_WORDS = ("仏様", "仏さま", "癒やし", "癒し", "感動", "圧倒", "驚き", "最高", "興奮")
BANNED_RE = re.compile("|".join(BANNED_WORDS))
SCULPTOR_RE = re.compile(r"運慶|快慶|定朝|湛慶|康慶|康勝|康弁|行快|安阿弥|院覚|院派|円派|慶派")

DESIGNATIONS = {"国宝": "国宝", "重文": "重要文化財", "重要文化財": "重要文化財",
                "重美": "重要美術品", "重要美術品": "重要美術品"}
DESIGNATION_RANK = {"国宝": 0, "重要文化財": 1, "重要美術品": 3}

# ---------------------------------------------------------------- 仏像かどうかの判定

EXCLUDE_RE = re.compile(r"図|絵|画|経|曼荼羅|拓|縁起|版|摺|幅|巻|軸|屏風|書|箱|鏡|硯|鈴|杵|香炉|花瓶|仏具|舎利容器|幡|鐘|断簡|模本")
REPLICA_RE = re.compile(r"模造|復元|複製|レプリカ")
# 仏像ではないもの（面・工芸品）。彫刻の部門でも仏像には数えない
NON_IMAGE_RE = re.compile(r"伎楽|舞楽|仮面|能面|獅子頭|面(?:\s|$)|舎利容器|仏具|香炉|華鬘|五鈷|鈴|杵")
ARCHAEO_RE = re.compile(r"出土|骨蔵器|蔵骨器|土器|瓦|墓誌|古墳|経筒|経塚")
STRONG_RE = re.compile(
    r"如来|菩薩|観音|明王|天王|天立像|天坐像|天像|神像|男神|女神|僧形|権現|地蔵|仏頭|仏像|仏龕|塼仏|金銅仏|押出仏|造像|"
    r"仏陀|吉祥天|弁才天|毘沙門|不動|愛染|阿弥陀|薬師|釈迦|弥勒|大日|文殊|普賢|十一面|千手|四天王|十二神将|童子|聖徳太子")
GENERIC_RE = re.compile(r"坐像|立像|半跏像|倚像|跏趺")   # 仏像以外（外国の神像・人物像）にも使われる語

JUDGE_SYSTEM = """博物館の展示作品の名前（と所蔵者・展示室・テーマ）から、その作品が「仏像・神像・仏教彫刻（立体の像）」かどうかを判定してください。
絵画（仏画・羅漢図・曼荼羅など）、書跡、工芸品（仏具・舎利容器・鈴など）、考古資料（土器・瓦など）は false。
名前に「像」と付いていても、絵画・拓本・模造・複製なら false。展示室やテーマに「彫刻」とあれば手がかりにする。
作品名・テーマの文字だけで判断し、あなたが知っている作品の知識で補わない。迷うときは false。"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "is_sculpture": {"type": "boolean"}},
        "required": ["id", "is_sculpture"], "additionalProperties": False}}},
    "required": ["results"],
    "additionalProperties": False,
}

ENRICH_SYSTEM = """博物館の公式ページ・展示品一覧の本文（資料）だけを根拠に、展示される仏像（彫刻）の特徴を整理してください。
資料に書かれていないことは、あなたが知っていることでも書かない（推測・一般知識・他の像との比較は禁止）。分からない項目は空文字。
- era: 時代・世紀（資料の表記のまま）
- material: 材質・技法（資料の表記のまま。例: 木造、漆箔、銅造）
- height: 像高・法量（資料の表記のまま）
- designation: 国宝・重要文化財・重要美術品のいずれか（資料に書かれているときだけ）
- note: 資料に書かれた特徴（姿・表情・衣文・素材・技法・伝来・制作事情・見どころ）を 2〜3 文でまとめる。100 字前後。
  資料に特徴の記述がなければ空文字にする。作者名（運慶・快慶など）は資料に書かれたとおりに書き、「伝」とあれば必ず「伝」を付ける。
  像は「◯◯像」「◯◯さん」と書き、「仏様」「仏さま」とは書かない。「癒やし」「感動」「圧倒」「驚き」「最高」「興奮」は使わない。
  数字は算用数字（尊格名の漢数字はそのまま。十一面観音など）
- evidence: note の根拠にした資料の一節を、資料の文字のまま写した引用（1 つ 40 字以内、1〜3 件）。note が空なら空配列"""

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "era": {"type": "string"}, "material": {"type": "string"}, "height": {"type": "string"},
        "designation": {"type": "string"}, "note": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["era", "material", "height", "designation", "note", "evidence"],
    "additionalProperties": False,
}


def nospace(s: str) -> str:
    return re.sub(r"[\s　・、。,]", "", s or "")


def clean(s: str) -> str:
    return " ".join((s or "").replace("　", " ").split())


def norm_designation(s: str) -> str:
    s = clean(s)
    return next((v for k, v in DESIGNATIONS.items() if k in s), "")


def classify(name: str, hint: bool = False, strict: bool = False) -> bool | None:
    """作品名から仏像かどうかを決める。True / False、判断がつかないときは None。
    hint: 展示室・部門が彫刻と分かっているとき（複製でなければ仏像として扱う）。
    strict: 外国の彫刻が多い展示室（東洋館）。「坐像」「立像」だけでは仏像とみなさず、判断保留にする。
    絵・経・鏡・出土品などを示す語があれば False。仏像らしい語があれば True。名前に「像」だけがあるときは判断保留。"""
    if REPLICA_RE.search(name) or NON_IMAGE_RE.search(name):
        return False
    if hint:
        return True
    if ARCHAEO_RE.search(name) or EXCLUDE_RE.search(name):
        return False
    if STRONG_RE.search(name):
        return True
    if GENERIC_RE.search(name) and not strict:
        return True
    return None if "像" in name or GENERIC_RE.search(name) else False


# ---------------------------------------------------------------- 取得

_last_request = [0.0]


def _get(url: str, **kw):
    """3 秒おきに取得する。SSL の失敗は検証なしで再試行（exhibit_watch と同じ）。"""
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()
    try:
        return requests.get(url, headers=ec.HEADERS, timeout=20, **kw)
    except requests.exceptions.SSLError:
        return requests.get(url, headers=ec.HEADERS, timeout=20, verify=False, **kw)


def fetch_soup(url: str) -> BeautifulSoup | None:
    try:
        r = _get(url)
        if r.status_code != 200:
            print(f"  取得できず（{r.status_code}）: {url}")
            return None
        return BeautifulSoup(r.content, "lxml")
    except Exception as e:
        print(f"  取得に失敗: {url} {type(e).__name__}")
        return None


def fetch_pdf_text(url: str) -> str:
    """PDF の本文（改行を残す。大きさ・ページ数に上限）。取れなければ空文字。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        r = _get(url, stream=True)
        if r.status_code != 200 or int(r.headers.get("content-length") or 0) > PDF_MAX_BYTES:
            return ""
        buf = r.raw.read(PDF_MAX_BYTES + 1, decode_content=True)
        if len(buf) > PDF_MAX_BYTES:
            return ""
        reader = PdfReader(io.BytesIO(buf))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:PDF_MAX_PAGES])
    except Exception:
        return ""


# ---------------------------------------------------------------- 日付

_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_WAREKI_RE = re.compile(r"令和\s*\d+\s*年?\s*[（(]\s*(\d{4})\s*[）)]\s*年?")


def parse_range_jp(text: str) -> tuple[date | None, date | None]:
    """「2026年12月22日（火）～2027年3月22日（月・休）」「令和8(2026)年9月15日(火)～」などから (開始, 終了)。"""
    t = _WAREKI_RE.sub(r"\1年", text or "")
    parts = re.split(r"[～~〜]", t, maxsplit=1)

    def first(s: str) -> date | None:
        m = _DATE_RE.search(s)
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
        except ValueError:
            return None
    start = first(parts[0])
    end = first(parts[1]) if len(parts) > 1 else None
    if len(parts) == 1:   # 「2026年9月15日」だけ（1 日の展示）
        return start, None
    return start, end


def _md(month: int, day: int, year: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_md_range(text: str, today: date) -> tuple[date | None, date | None]:
    """九博の「～12/6」「12/22～4/11」「11/25～」のような年のない期間を、今日を基準に年を補って読む。"""
    m = re.fullmatch(r"\s*(?:(\d{1,2})/(\d{1,2}))?\s*[～~〜]\s*(?:(\d{1,2})/(\d{1,2}))?\s*", text or "")
    if not m:
        return None, None
    sm, sd, em, ed = (int(x) if x else None for x in m.groups())
    start = end = None
    if sm:
        for y in (today.year - 1, today.year, today.year + 1):
            c = _md(sm, sd, y)
            if c and c >= today - timedelta(days=200):
                start = c
                break
    if em:
        for y in (today.year - 1, today.year, today.year + 1, today.year + 2):
            c = _md(em, ed, y)
            if c and c >= (start or today - timedelta(days=45)):
                end = c
                break
    return start, end


def iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def to_date(s: str) -> date | None:
    return kc.parse_date(s) if s else None


# ---------------------------------------------------------------- 館ごとの読み取り
# 各 fetch_* は「作品の行（dict）」のリストを返す。取得できなければ None。
# 行: name, owner, designation, room, start, end（date | None）, origin, era, link, page_url, image_url,
#     notable（館の推薦・新指定）, reason, context（テーマ・展示の解説）, hint（彫刻と分かっているか）

def make_row(name: str, **kw) -> dict:
    row = {"name": clean(name), "owner": "", "designation": "", "room": "", "start": None, "end": None,
           "origin": "", "era": "", "code": "", "link": "", "page_url": "", "image_url": "", "notable": False,
           "reason": "", "context": "", "hint": False}
    row.update(kw)
    return row


# --- 京都国立博物館

def kyohaku_month_urls(today: date, months: int = 5) -> list[str]:
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        out.append(f"https://www.kyohaku.go.jp/jp/exhibitions/collection/{y}/{m:02d}/")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def parse_kyohaku(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """月ごとのページから、テーマごとの展示期間・展示室・作品リストを読む（日ごとに同じ記事が繰り返されるので重複は除く）。"""
    rows, seen = [], set()
    for art in soup.select("div.collectionArticle"):
        title_el = art.select_one(".collectionArticle__title h2.title")
        meta_el = art.select_one(".collectionArticle__title p.text")
        if not title_el or not meta_el:
            continue
        meta = meta_el.get_text("|", strip=True)
        key = (art.get("data-entry-check", ""), clean(title_el.get_text(" ", strip=True)), meta)
        if key in seen:   # 同じ記事が日ごとに（別の id で）繰り返される
            continue
        seen.add(key)
        start, end = parse_range_jp(meta.split("|")[0])
        kind_room = meta.split("|", 1)[1] if "|" in meta else ""
        kind, _, room = kind_room.partition("｜")
        kind, room = clean(kind), clean(room)
        desc = " ".join(clean(p.get_text(" ", strip=True)) for p in art.select(".collectionArticle__contents > p"))
        img = art.select_one(".photoList img")
        img_url = urljoin(page_url, img["src"]) if img and img.get("src") else ""
        img_alt = clean(img.get("alt", "")) if img else ""
        theme = clean(title_el.get_text(" ", strip=True))
        hint = "彫刻" in kind
        for tr in art.select(".collectionArticle__workList table tr"):
            td = [clean(c.get_text(" ", strip=True)) for c in tr.find_all("td")]
            if len(td) < 4 or not td[1]:
                continue
            row = make_row(
                td[1], owner=td[3], designation=norm_designation(td[0]), room=f"{room}（{kind}）" if kind else room,
                start=start, end=end, page_url=page_url, hint=hint,
                context=f"テーマ「{theme}」{kind}。{desc}",
                image_url=img_url if (img_alt and nospace(td[1]) in nospace(img_alt)) else "")
            if row["owner"] in ("", "　"):
                row["owner"] = ""
            rows.append(row)
    return rows


def fetch_kyohaku(today: date) -> list[dict] | None:
    rows, got = [], 0
    for url in kyohaku_month_urls(today):
        soup = fetch_soup(url)
        if soup is None:
            continue
        got += 1
        rows += parse_kyohaku(soup, url)
    return rows if got else None


# --- 九州国立博物館

def parse_kyuhaku(soup: BeautifulSoup, today: date, base: str = "https://collection.kyuhaku.jp/exhibition/") -> list[dict]:
    """作品一覧の表（展示品名称・指定・出土地や作者・所蔵者・展示場所・展示期間）。同じ行は 2 度現れるので除く。"""
    rows, seen = [], set()
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 6:
            continue
        v = [clean(c.get_text(" ", strip=True)) for c in tds]
        a = tds[0].find("a", href=True)
        link = urljoin(base, a["href"]) if a else ""
        sig = (v[0], v[2], v[3], v[4], v[5], link)
        if sig in seen or not v[0]:
            continue
        seen.add(sig)
        start, end = parse_md_range(v[5], today)
        rows.append(make_row(v[0], owner=v[3], designation=norm_designation(v[1]), room=v[4], start=start, end=end,
                             origin=v[2], link=link, page_url=link or base))
    return rows


def fetch_kyuhaku(today: date) -> list[dict] | None:
    base = "https://collection.kyuhaku.jp/exhibition/"
    first = fetch_soup(base)
    if first is None:
        return None
    rows = parse_kyuhaku(first, today, base)
    want = today.year * 100 + today.month
    pages = sorted({m.group(1) for a in first.find_all("a", href=True)
                    if (m := re.fullmatch(r"(\d{6})\.html", a["href"])) and int(m.group(1)) >= want})
    for p in pages:
        s = fetch_soup(urljoin(base, f"{p}.html"))
        if s is not None:
            rows += parse_kyuhaku(s, today, base)
    return rows


# --- 奈良国立博物館

PREFS = ("北海道 青森 岩手 宮城 秋田 山形 福島 茨城 栃木 群馬 埼玉 千葉 東京 神奈川 新潟 富山 石川 福井 山梨 長野 岐阜 静岡 愛知 三重 "
         "滋賀 京都 大阪 兵庫 奈良 和歌山 鳥取 島根 岡山 広島 山口 徳島 香川 愛媛 高知 福岡 佐賀 長崎 熊本 大分 宮崎 鹿児島 沖縄").split()
_COUNT_RE = re.compile(r"\s([0-9０-９一二三四五六七八九十]*)\s*(軀|躯|基|面|体|点|口|合|片|個|幅|枚|具|組|件|頭|柱|尊|巻|帖|旒|振|括)"
                       r"(?:\s*のうち\s*[0-9０-９]+\s*\S+)?|\s(一括|一式)")
_NARA_LINE_RE = re.compile(r"^(国宝|重文|重美)?(※)?\s*(?:(彫刻|絵画|書跡|工芸|考古|歴史|典籍|染織|金工|漆工)\s+)?(.+)$")
_SCULPTURE_UNITS = ("軀", "躯", "体", "尊", "基", "組")


def parse_nara_pdf(text: str) -> list[dict]:
    """展示品一覧（指定・作品名称・員数・時代・所蔵者。部門の列があるものもある）から、彫刻を読む。
    部門の列がないものは、員数の単位（軀・体・基など）と作品名で彫刻を選ぶ。指定の後ろの「※」は官報公示前（新指定）。
    行をまたぐ長い名前は取りこぼす。"""
    out = []
    for line in text.splitlines():
        m = _NARA_LINE_RE.match(clean(line))
        if not m:
            continue
        rest = " " + m.group(4)
        c = _COUNT_RE.search(rest)
        if not c:
            continue
        dept = m.group(3) or ""
        if (dept and dept != "彫刻") or (not dept and c.group(2) not in _SCULPTURE_UNITS):
            continue
        name, after = clean(rest[:c.start()]), clean(rest[c.end():])
        toks = after.split()
        owner, era = "", after
        if toks and toks[-1] == "当館":
            owner, era = "奈良国立博物館", " ".join(toks[:-1])
        elif len(toks) >= 2 and toks[-2] in PREFS:
            owner, era = toks[-2] + "・" + toks[-1], " ".join(toks[:-2])
        elif toks and re.search(r"寺|院|神社|宮|堂|坊|会|館|氏", toks[-1]):
            owner, era = toks[-1], " ".join(toks[:-1])
        if name:
            out.append({"name": name, "owner": owner, "era": era, "designation": norm_designation(m.group(1) or ""),
                        "pending": bool(m.group(2)), "dept": dept})
    return out


def nara_pdf_periods(main, page_text_range: tuple[date | None, date | None]) -> list[tuple[str, str, tuple[date | None, date | None], str]]:
    """詳細ページの展示品一覧リンクごとに (リンク文字, URL, 期間, 直前の文脈) を返す。
    期間は、リンクの直前にある日付範囲（①②のように分かれているとき）。なければページ全体の会期。"""
    marks, links = [], []
    for i, a in enumerate(main.find_all("a", href=True)):
        if ".pdf" in a["href"].lower() and re.search(r"一覧|リスト|展示品", a.get_text() + a["href"]):
            token = f"⟦PDF{i}⟧"
            a.insert_before(token)
            marks.append(token)
            links.append(a)
    text = main.get_text(" ", strip=True)
    out = []
    for token, a in zip(marks, links):
        pos = text.find(token)
        before = text[:pos] if pos >= 0 else ""
        rng = re.findall(r"(?:令和\s*\d+\s*年?\s*[（(]\s*\d{4}\s*[）)]|\d{4})\s*年?\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^～~〜]{0,12}[～~〜]\s*"
                         r"(?:(?:令和\s*\d+\s*年?\s*[（(]\s*\d{4}\s*[）)]|\d{4})\s*年?\s*\d{1,2}\s*月\s*\d{1,2}\s*日)?", before)
        period = parse_range_jp(rng[-1]) if rng else page_text_range
        out.append((clean(a.get_text(" ", strip=True)), urljoin("https://www.narahaku.go.jp/", a["href"]), period,
                    clean(before[-80:])))
    return out


def fetch_narahaku(today: date, read_pdf=None) -> list[dict] | None:
    read_pdf = read_pdf or fetch_pdf_text
    index_url = MUSEUMS["narahaku"]["url"]
    index = fetch_soup(index_url)
    if index is None:
        return None
    pages = []
    for a in index.find_all("a", href=True):
        h = urljoin(index_url, a["href"])
        if re.search(r"/exhibition/usual/[^/]+/?$", h) and h not in pages:
            pages.append(h)
    rows = []
    for url in pages:
        soup = fetch_soup(url)
        if soup is None:
            continue
        main = soup.find("main") or soup.body
        title = clean((soup.title.get_text() if soup.title else "").split(" - ")[0])
        text = clean(main.get_text(" ", strip=True))
        m = re.search(r"会\s*期\s*(.+?)\s*会\s*場", text)
        page_range = parse_range_jp(m.group(1)) if m else (None, None)
        v = re.search(r"会\s*場\s*(.+?)\s*(?:※|開館時間|観覧料|展示品一覧|$)", text)
        venue = clean(v.group(1)) if v else ""
        for label, pdf_url, (start, end), _ in nara_pdf_periods(main, page_range):
            body = read_pdf(pdf_url)
            for r in parse_nara_pdf(body):
                rows.append(make_row(
                    r["name"], owner=r["owner"], designation=r["designation"], room=venue, start=start, end=end,
                    era=r["era"], link="", page_url=url, hint=r["dept"] == "彫刻", notable=r["pending"],
                    reason="新指定（官報公示前）" if r["pending"] else "",
                    context=f"{title}。{text[:2500]}"))
    return rows


# --- 東京国立博物館

TNM_HALLS = {"本館": 12, "東洋館": 13, "法隆寺宝物館": 16}
TNM_BASE = "https://www.tnm.jp/modules/r_exhibition/index.php"
TNM_SCULPTURE_WORDS = re.compile(r"彫刻|仏像|仏|金銅|像|神像|如来|菩薩|観音|明王|羅漢|ガンダーラ")


def parse_tnm_blocks(soup: BeautifulSoup, hall_label: str = "") -> list[dict]:
    """館ごとのページ（hall）と「今週の東博コレクション展」から、展示（テーマ）の一覧を読む。
    返す dict: item_id（作品リストのページ）, title, room, start, end, desc。"""
    out = {}
    c = soup.find(id="contents2022") or soup
    for t in c.select("div.text"):          # hall のページ
        h3, date_el = t.find("h3", class_="title"), t.find("p", class_="date")
        a = t.find("a", href=re.compile(r"controller=item&(?:amp;)?id=\d+"))
        if not (h3 and date_el and a):
            continue
        item_id = re.search(r"id=(\d+)", a["href"]).group(1)
        room = clean(t.find("p", class_="room").get_text(" ", strip=True)) if t.find("p", class_="room") else ""
        start, end = parse_range_jp(date_el.get_text(" ", strip=True))
        desc = clean(t.find("div", class_="desc").get_text(" ", strip=True)) if t.find("div", class_="desc") else ""
        out[item_id] = {"item_id": item_id, "title": clean(h3.get_text(" ", strip=True)), "room": f"{hall_label} {room}".strip(),
                        "start": start, "end": end, "desc": desc}
    for b in c.select("div.r_exhibition_list_block"):   # 今週の展示替え
        a = next((x for x in b.find_all("a", href=re.compile(r"controller=item&(?:amp;)?id=\d+")) if clean(x.get_text())), None)
        if not a:
            continue
        item_id = re.search(r"id=(\d+)", a["href"]).group(1)
        if item_id in out:
            continue
        text = clean(b.get_text(" ", strip=True))
        rm = re.search(r"((?:特別)?\d+室(?:・\d+室)?|特別\d室)", text)
        start, end = parse_range_jp(text)
        out[item_id] = {"item_id": item_id, "title": clean(a.get_text(" ", strip=True)), "room": f"{hall_label} {rm.group(1)}".strip() if rm else hall_label,
                        "start": start, "end": end, "desc": text}
    return list(out.values())


def tnm_owner(cell: str) -> tuple[str, str]:
    """「所蔵者・寄贈者・列品番号」の欄から (所蔵者, 列品番号)。館蔵・寄贈品は所蔵者を東京国立博物館にし、寄託品は「蔵」を除く。"""
    m = re.search(r"\b[A-Z]{1,3}-?\d[\w-]*", cell)
    code = m.group(0) if m else ""
    rest = clean(cell.replace(code, "")) if code else cell
    if not rest or "寄贈" in rest or "旧蔵" in rest:
        return "東京国立博物館", code
    return re.sub(r"蔵$", "", rest), code


def parse_tnm_item(soup: BeautifulSoup, block: dict, item_url: str) -> list[dict]:
    """作品リスト（指定・名称・員数・作者・時代・所蔵者・備考）の表を読む。彫刻のテーマ・部屋は全作品、それ以外は作品名で判断する。"""
    rows = []
    # 本館の彫刻室と、金銅仏・押出仏の展示は全作品が仏像。東洋館などの「彫刻」は仏像以外も含むので作品名で判断する
    hint = (("彫刻" in block["title"] and block["room"].startswith("本館")) or "金銅仏" in block["title"]
            or "押出仏" in block["title"])
    for tr in soup.select("table.exhibition-item-list__item-list tr"):
        td = tr.find_all("td")
        if len(td) < 7:
            continue
        name = clean(td[2].get_text(" ", strip=True))
        if not name or name == "名称":
            continue
        a = td[2].find("a", href=True)
        recommended = "おすすめ" in td[0].get_text()
        owner, code = tnm_owner(clean(td[6].get_text(" ", strip=True)))
        rows.append(make_row(
            name, owner=owner, code=code, designation=norm_designation(td[1].get_text()), room=block["room"],
            start=block["start"], end=block["end"], origin=clean(td[4].get_text(" ", strip=True)),
            era=clean(td[5].get_text(" ", strip=True)), link=urljoin(item_url, a["href"]) if a else "",
            page_url=item_url, hint=hint, notable=recommended, reason="館のおすすめ" if recommended else "",
            context=f"テーマ「{block['title']}」。{block['desc']}"))
    return rows


def fetch_tnm(today: date) -> list[dict] | None:
    blocks, got = {}, 0
    for label, hid in TNM_HALLS.items():
        soup = fetch_soup(f"{TNM_BASE}?controller=hall&hid={hid}")
        if soup is None:
            continue
        got += 1
        for b in parse_tnm_blocks(soup, label):
            blocks.setdefault(b["item_id"], b)
    soup = fetch_soup(f"{TNM_BASE}?controller=change")
    if soup is not None:
        got += 1
        for b in parse_tnm_blocks(soup, "本館"):
            blocks.setdefault(b["item_id"], b)
    if not got:
        return None
    rows = []
    for b in blocks.values():
        if not TNM_SCULPTURE_WORDS.search(b["title"] + b["desc"]):
            continue
        url = f"{TNM_BASE}?controller=item&id={b['item_id']}"
        soup = fetch_soup(url)
        if soup is not None:
            rows += parse_tnm_item(soup, b, url)
    return rows


FETCHERS = {"tnm": fetch_tnm, "kyohaku": fetch_kyohaku, "narahaku": fetch_narahaku, "kyuhaku": fetch_kyuhaku}


# ---------------------------------------------------------------- 状態（data/gallery_watch.json）

def load_state() -> dict:
    d = ec.load_json(STATE_FILE, {})
    if not isinstance(d, dict):
        d = {}
    for k, v in (("museums", {}), ("judgments", {}), ("digest_pending", [])):
        if not isinstance(d.get(k), type(v)):
            d[k] = v
    return d


def save_state(state: dict) -> None:
    for m in state["museums"].values():   # 解説文は特徴の抽出にだけ使う（毎回の取得で作り直す）。ファイルには残さない
        for it in m.get("items", {}).values():
            it.pop("context", None)
    state["last_updated"] = datetime.now(JST).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def item_id(museum: str, name: str, owner: str, extra: str = "") -> str:
    return hashlib.md5("|".join([museum, nospace(name), nospace(owner), extra]).encode()).hexdigest()[:12]


def row_id(museum: str, row: dict) -> str:
    # 九博は同名の別の像が同じ所蔵者にあるので、作品ページの番号か出土地・作者で区別する
    extra = row.get("code", "") if museum == "tnm" else ""
    if museum == "kyuhaku":
        m = re.search(r"/gallery/(\d+)\.html", row.get("link", ""))
        extra = m.group(1) if m else nospace(row.get("origin", ""))
    return item_id(museum, row["name"], row["owner"], extra)


# ---------------------------------------------------------------- 注目度

def is_deposit_temple(owner: str, museum: str) -> bool:
    """寄託の寺院像（所蔵者が寺院・神社などで、館や国・個人ではない）。"""
    if not owner or re.search(r"国立博物館|当館|文化庁|個人|美術院|博物館|美術館", owner):
        return False
    return bool(re.search(r"寺|院|神社|大社|宮|堂|坊|庵|教会|八幡", owner))


def rank(item: dict) -> int:
    """重要な順（小さいほど先）: 国宝 0 > 重要文化財 1 > 注目（館のおすすめ・新指定）2 > 重要美術品 3 > 寄託の寺院像 4 > ほか 5。"""
    d = item.get("designation", "")
    if d in ("国宝", "重要文化財"):
        return DESIGNATION_RANK[d]
    if item.get("notable"):
        return 2
    if d == "重要美術品":
        return 3
    if is_deposit_temple(item.get("owner", ""), item.get("museum", "")):
        return 4
    return 5


def new_designation_titles(data: dict, today: date, days: int = 21) -> list[str]:
    """最近のニュースで「新指定」「新たに国宝・重要文化財に指定」と書かれた記事のタイトル（空白を除く）。"""
    cutoff = (datetime.combine(today, datetime.min.time(), tzinfo=JST) - timedelta(days=days)).isoformat()
    return [nospace(it.get("title", "")) for it in data.get("items", [])
            if it.get("fetched_at", "") >= cutoff
            and re.search(r"新指定|新たに.{0,10}(国宝|重要文化財)", it.get("title", ""))]


def owner_core(owner: str) -> str:
    o = re.sub(r"^(?:" + "|".join(PREFS) + r")[都道府県]?[・\s]*", "", owner or "")
    return nospace(o)


def mentions_new_designation(item: dict, titles: list[str]) -> bool:
    name = nospace(item["name"])
    if len(name) < 4:
        return False
    core = owner_core(item.get("owner", ""))
    return any(name in t and (not core or len(core) < 2 or core in t) for t in titles)


# ---------------------------------------------------------------- 差分の検出

def group_rows(museum: str, rows: list[dict]) -> dict[str, dict]:
    """行を作品ごとにまとめる（同じ id は展示期間を足す）。"""
    out: dict[str, dict] = {}
    for r in rows:
        iid = row_id(museum, r)
        g = out.get(iid)
        period = (iso(r["start"]), iso(r["end"]), r["room"])
        if not g:
            g = out[iid] = {"id": iid, "museum": museum, "name": r["name"], "owner": r["owner"], "designation": r["designation"],
                            "room": r["room"], "origin": r["origin"], "era": r["era"], "code": r["code"], "link": r["link"], "page_url": r["page_url"],
                            "image_url": r["image_url"], "notable": r["notable"], "reason": r["reason"], "context": r["context"],
                            "periods": []}
        else:
            for k in ("designation", "link", "image_url", "reason"):
                g[k] = g[k] or r[k]
            g["notable"] = g["notable"] or r["notable"]
            if len(r["context"]) > len(g["context"]):
                g["context"] = r["context"]
        if period[:2] not in [(p["start"], p["end"]) for p in g["periods"]]:
            g["periods"].append({"start": period[0], "end": period[1], "room": period[2]})
    return out


def merge_museum(mstate: dict, rows: list[dict], museum: str, today: date, titles: list[str]) -> list[dict]:
    """取得した行を館の state に取り込み、通知の対象になる節目（イベント）を返す。
    シード（館の初回）は、開始日が先の展示だけをイベントにし、ほかは通知済みとして記録する。"""
    now_iso = datetime.now(JST).isoformat(timespec="seconds")
    items = mstate.setdefault("items", {})
    seeding = not mstate.get("seeded")
    events = []
    for iid, g in group_rows(museum, rows).items():
        cur = items.get(iid)
        if cur is None:
            cur = items[iid] = {**g, "first_seen": today.isoformat(), "notified": {}}
        else:
            keep = {k: cur[k] for k in ("first_seen", "notified", "feature") if k in cur}
            was_notable, was_reason = cur.get("notable"), cur.get("reason", "")
            cur.update({**g, **keep})
            if was_notable and not cur["notable"]:   # 注目の印（新指定など）は、元の記事が古くなっても残す
                cur["notable"], cur["reason"] = True, was_reason
        cur["last_seen"] = today.isoformat()
        if not cur["notable"] and mentions_new_designation(cur, titles):
            cur["notable"], cur["reason"] = True, "新指定"
        cur["rank"] = rank(cur)
        periods = sorted(cur["periods"], key=lambda p: p["start"] or "0")
        for i, p in enumerate(periods):
            start, end = to_date(p["start"]), to_date(p["end"])
            if end and end < today:
                continue
            prev_end = max((to_date(q["end"]) for j, q in enumerate(periods) if j != i and q["end"]
                            and to_date(q["end"]) <= (start or today)), default=None)
            nxt_start = min((to_date(q["start"]) for j, q in enumerate(periods) if j != i and q["start"]
                             and to_date(q["start"]) >= (end or date.max)), default=None) if end else None
            continuation = bool(prev_end and start and (start - prev_end).days <= CONTINUE_GAP_DAYS)
            continued = bool(nxt_start and end and (nxt_start - end).days <= CONTINUE_GAP_DAYS)
            akey = f"appear@{p['start'] or 'open'}"
            if akey not in cur["notified"] and not continuation:
                future = bool(start and start > today)
                if seeding and not future:
                    cur["notified"][akey] = {"at": now_iso, "candidate": "", "seed": True}
                else:
                    events.append({"museum": museum, "item": cur, "key": akey, "kind": "appear", "period": p})
            elif akey not in cur["notified"]:
                cur["notified"][akey] = {"at": now_iso, "candidate": "", "continued": True}
            if end and not continued:
                left = (end - today).days
                ekey = f"ending@{p['end']}"
                if 0 <= left <= ENDING_DAYS and ekey not in cur["notified"] and cur["rank"] <= 2:
                    if seeding:
                        cur["notified"][ekey] = {"at": now_iso, "candidate": "", "seed": True}
                    else:
                        events.append({"museum": museum, "item": cur, "key": ekey, "kind": "ending", "period": p})
    mstate["seeded"] = mstate.get("seeded") or today.isoformat()
    return events


def prune_state(mstate: dict, today: date) -> int:
    drop = []
    for iid, it in mstate.get("items", {}).items():
        if classify(it["name"], True) is False:   # 面・工芸品などを仏像として記録していた分
            drop.append(iid)
            continue
        ends = [to_date(p["end"]) for p in it["periods"]]
        last = max((e for e in ends if e), default=None)
        if all(ends) and last and (today - last).days > PRUNE_DAYS:
            drop.append(iid)
        elif not last and it.get("last_seen") and (today - to_date(it["last_seen"])).days > PRUNE_DAYS:
            drop.append(iid)
    for iid in drop:
        del mstate["items"][iid]
    return len(drop)


# ---------------------------------------------------------------- AI（軽いモデル）

def resolve_sculptures(rows: list[dict], state: dict, client, usage, budget: list[int]) -> tuple[list[dict], int]:
    """行から仏像だけを残す。作品名で判断がつかないものは、判定のキャッシュか軽いモデルで決める。
    返す: (残した行, 判断できず除外した数)。"""
    keep, pending, undecided = [], [], 0
    cache = state["judgments"]
    for r in rows:
        v = classify(r["name"], r["hint"], strict=r["room"].startswith("東洋館"))
        if v is None:
            ck = nospace(r["name"]) + "|" + nospace(r["context"][:40])
            if ck in cache:
                v = cache[ck]
            else:
                pending.append((ck, r))
                continue
        if v:
            keep.append(r)
    uniq = {}
    for ck, r in pending:
        uniq.setdefault(ck, r)
    if uniq:
        decided = {}
        if client and budget[0] > 0:
            cks = list(uniq)
            for i in range(0, len(cks), JUDGE_BATCH):
                if budget[0] <= 0:
                    break
                batch = cks[i:i + JUDGE_BATCH]
                budget[0] -= 1
                user = "## 判定する作品\n" + "\n".join(
                    f"- id={n} 名前={uniq[ck]['name']} 所蔵={uniq[ck]['owner'] or '（なし）'} 展示室={uniq[ck]['room']} "
                    f"テーマ={uniq[ck]['context'][:60]}" for n, ck in enumerate(batch))
                import curate_news
                res = curate_news.call_json(client, usage, JUDGE_SYSTEM, user, JUDGE_SCHEMA, effort="low", model=ec.LIGHT_MODEL)
                for x in (res or {}).get("results", []):
                    try:
                        decided[batch[int(x["id"])]] = bool(x["is_sculpture"])
                    except (ValueError, IndexError, KeyError):
                        continue
        for ck, r in uniq.items():
            if ck in decided:
                cache[ck] = decided[ck]
            else:
                undecided += 1
        for ck, r in pending:
            if cache.get(ck):
                keep.append(r)
    return keep, undecided


def verify_enrichment(res: dict, source: str, museum: str) -> dict:
    """AI の結果のうち、資料に書かれていると確かめられたものだけを残す（項目は資料に含まれる文字、説明は引用で裏付ける）。"""
    src = nospace(source)
    out = {k: "" for k in ("era", "material", "height", "designation", "note")}
    for k in ("era", "material", "height"):
        v = clean(res.get(k, ""))
        if v and nospace(v) in src:
            out[k] = v
    d = norm_designation(res.get("designation", ""))
    if d and (d in source or next(k for k, v in DESIGNATIONS.items() if v == d) in source):
        out["designation"] = d
    note = clean(res.get("note", ""))
    quotes = [clean(q) for q in res.get("evidence", []) if clean(q)]
    if note and quotes and all(nospace(q) in src for q in quotes) and not BANNED_RE.search(note) \
            and attribution_ok(note, source):
        out["note"] = note
    return out


def attribution_ok(text: str, source: str) -> bool:
    """作者名は、資料に書かれたものだけ。資料が「伝◯◯」なら、文章も「伝」を付ける。"""
    for m in SCULPTOR_RE.finditer(text):
        name = m.group(0)
        if name not in source:
            return False
        if re.search("伝\\s*" + name, source) and not re.search("伝\\s*" + name, text):
            return False
    return True


def item_source_text(item: dict, page_text: str, web_text: str = "") -> str:
    """特徴の抽出に渡す資料: 一覧の記載（作品名・指定・所蔵・時代・展示室・期間）、作品ページの本文、テーマ・展示の解説。"""
    facts = [f"作品名: {item['name']}"]
    for label, k in (("指定", "designation"), ("所蔵者", "owner"), ("時代", "era"), ("出土地・作者", "origin"), ("展示室", "room")):
        if item.get(k):
            facts.append(f"{label}: {item[k]}")
    per = " / ".join(f"{p['start'] or '?'}〜{p['end'] or '?'}" for p in item["periods"])
    facts.append(f"展示期間: {per}")
    parts = ["## 一覧の記載\n" + "\n".join(facts)]
    if page_text:
        parts.append("## 作品ページの本文\n" + page_text[:2500])
    if item.get("context"):
        parts.append("## テーマ・展示の解説\n" + item["context"][:2000])
    text = "\n\n".join(parts)[:SOURCE_CHARS]
    if web_text:   # 公式サイトの検索結果の引用（出典 URL つき）
        text += "\n\n## 公式サイトの検索結果の引用\n" + web_text[:2500]
    return text


def needs_search(item: dict) -> bool:
    """作品ごとのページがなく（京博・奈良博など）、国宝・重文・重美・注目・寄託の寺院像で、特徴がまだ空の像。"""
    f = item.get("feature") or {}
    empty = not (f.get("note") or f.get("material") or f.get("height"))
    return bool(empty and (f.get("searched") or 0) < SEARCH_VERSION and not item.get("link") and item.get("rank", 5) <= 4)


def search_features(client, usage, item: dict, budget: list) -> tuple[str, str] | None:
    """Claude の web_search ツール（公式な情報源のドメインだけ）で「所蔵＋像名」を調べ、引用された本文と出典 URL を返す。
    返すのは検索結果の引用そのもの（モデルの言い換えは使わない）。像名・所蔵者が引用に出てこないものは捨てる。
    budget: [残りの検索回数, 使った検索回数]。"""
    if budget[0] <= 0:
        return None
    owner = re.sub(r"^(?:" + "|".join(PREFS) + r")[都道府県]?[・\s]*", "", item.get("owner", ""))
    query = f"{owner} {item['name']}".strip()
    try:
        resp = client.messages.create(
            model=ec.LIGHT_MODEL, max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": min(2, budget[0]),
                    "allowed_domains": SEARCH_DOMAINS}],
            messages=[{"role": "user", "content": (
                f"次の仏像について、公式サイトのページから、時代・素材（技法）・像高・解説を探して、見つけた内容を引用して答えてください。\n"
                f"所蔵: {item.get('owner') or '（不明）'}\n像名: {item['name']}\n指定: {item.get('designation') or '（なし）'}\n"
                f"検索語の例: {query}\n見つからなければ、見つからなかったと答える。推測は書かない。")}])
    except Exception as e:   # API の拒否・ツール非対応・接続エラー。特徴なしのまま進める
        print(f"::warning::web 検索に失敗: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        return "", ""
    usage.add(resp.usage)
    used = getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", None)
    used = used if isinstance(used, int) else 1
    budget[0] -= max(used, 1)
    budget[1] += used
    name_core = nospace(item["name"])[:5]
    full_core = nospace(re.sub(r"[（(].*?[）)]", "", item["name"]))   # ページを開く照合は像名の全体で（同名の別の像を避ける）
    owner_core_ = owner_core(item.get("owner", ""))
    snippets, urls, raw = [], [], 0
    seen_urls, errors, said, results = [], [], "", []
    for block in resp.content:
        btype = getattr(block, "type", "")
        if btype == "web_search_tool_result":
            content = getattr(block, "content", None)
            if isinstance(content, list):
                seen_urls += [getattr(r, "url", "") for r in content]
                results += [(getattr(r, "url", ""), getattr(r, "title", "") or "") for r in content]
            else:
                errors.append(str(getattr(content, "error_code", content))[:60])
        if btype != "text":
            continue
        said = said or (getattr(block, "text", "") or "")[:100]
        for c in getattr(block, "citations", None) or []:
            raw += 1
            text, url = getattr(c, "cited_text", "") or "", getattr(c, "url", "") or ""
            n = nospace(text + (getattr(c, "title", "") or ""))   # 出典ページの題名も照合に使う（引用の断片に寺名がないことが多い）
            if not url or text in snippets:
                continue
            if name_core and name_core not in n:
                continue
            if len(owner_core_) >= 2 and owner_core_ not in n:
                continue
            snippets.append(text)
            urls.append(url)
    print(f"  web 検索の結果: 検索結果URL{len(seen_urls)}件 {[u[:60] for u in seen_urls[:3]]} / 引用{raw}件 / エラー{errors or 'なし'} / 回答「{clean(said)}」")
    # 検索結果のページ（許可ドメインのみ）は、引用がなくても自分で開いて本文を読む。題名に像名があり、本文に所蔵者がある 1〜4 ページ
    opened = 0
    for url, title in results:
        if opened >= MAX_FETCH_PER_ITEM or url in urls:
            continue
        host = urlsplit(url).hostname or ""
        if not any(host == d or host.endswith("." + d) for d in SEARCH_DOMAINS) or (full_core and full_core not in nospace(title)):
            continue
        opened += 1
        page = fetch_soup(url)
        if page is None:
            continue
        for t in page(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            t.decompose()
        text = clean((page.find("main") or page.body or page).get_text(" ", strip=True))
        n = nospace(text)
        if full_core not in n or (len(owner_core_) >= 2 and owner_core_ not in n):
            continue
        snippets.append(text[:1500])
        urls.append(url)
    out = "\n".join(f"[{u}] {t}" for u, t in zip(urls, snippets))
    print(f"  web 検索: {query} → 資料{len(snippets)}件（引用{raw}件・開いたページ{opened}件・検索{used}回）")
    return out, (urls[0] if urls else "")


def fetch_item_page(item: dict) -> tuple[str, str]:
    """作品の公式ページ（一覧からリンクされているもの）の本文と画像 URL。ないときは空。"""
    url = item.get("link", "")
    if not url:
        return "", ""
    soup = fetch_soup(url)
    if soup is None:
        return "", ""
    og = soup.find("meta", property="og:image")
    img = urljoin(url, og["content"]) if og and og.get("content") else ""
    if re.search(r"logo|ogp_?default|noimage|common", img, re.I):
        img = ""
    for t in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        t.decompose()
    root = soup.find("main") or soup.find(id="contents2022") or soup.body or soup
    return clean(root.get_text(" ", strip=True)), img


def enrich(client, usage, item: dict, fetch_page=None, search=None) -> bool:
    """作品の特徴（時代・材質・像高・指定・説明）を、公式ページ・一覧・公式サイトの検索結果の資料だけを根拠に作って item["feature"] に入れる。
    search: (item) -> (引用の本文, 出典 URL)。作品ページがない国宝・重文などだけ呼ぶ。"""
    import curate_news

    page_text, img = (fetch_page or fetch_item_page)(item)
    web_text = web_url = ""
    searched = False
    if search and needs_search(item):
        found = search(item)
        if found is not None:   # None は検索の上限（翌日以降にやり直す）
            web_text, web_url = found
            searched = True
    source = item_source_text(item, page_text, web_text)
    res = curate_news.call_json(client, usage, ENRICH_SYSTEM, f"## 資料\n{source}", ENRICH_SCHEMA,
                                effort="low", model=ec.LIGHT_MODEL)
    if res is None:
        return False
    feat = verify_enrichment(res, source, item["museum"])
    feat["era"] = feat["era"] or item.get("era", "")
    feat["designation"] = feat["designation"] or item.get("designation", "")
    feat["source_url"] = item.get("link") or (web_url if web_text and (feat["note"] or feat["material"] or feat["height"])
                                              else item.get("page_url", ""))
    feat["searched"] = SEARCH_VERSION if searched else (item.get("feature") or {}).get("searched", 0)
    feat["enriched_at"] = datetime.now(JST).isoformat(timespec="seconds")
    item["feature"] = feat
    if img and not item.get("image_url"):
        item["image_url"] = img
    return True


# ---------------------------------------------------------------- 配信候補

STATUE_RULES = (
    "像は「◯◯像」「◯◯さん」と書き、「仏様」「仏さま」とは書かない。"
    "「癒やし」「感動」「圧倒」「驚き」「最高」「興奮」は使わない。抽象的な感想語に頼らず、光の入り方・素材の質感・表情・衣文など、"
    "資料に書かれた具体的な見どころで伝える。数字は算用数字（十一面観音・十二神将のように尊格名の漢数字はそのまま）。"
    "運慶・快慶などの作者名は、資料に書かれたとおりにだけ書き、「伝」とあれば必ず「伝」を付けて断定しない。"
    "資料に書かれていないこと（日付・所蔵・見どころ・作者）は書かない。「行くならいま」「必見」のような決まり文句は使わない。")


def venue_label(museum: str, room: str) -> str:
    m = MUSEUMS[museum]
    return f"{m['name']} {m['gallery']}" + (f"（{room}）" if room else "")


def period_label(item: dict, key: str) -> str:
    """節目の期間の表記（例: 2026-12-22〜2027-03-22）。終了日がない展示は「◯日から」。"""
    start, _, end = key.partition("|")
    if start and end:
        return f"{start}〜{end}"
    return f"{start}から（終了日未定）" if start else (f"〜{end}" if end else "展示中")


def event_room(event: dict) -> str:
    return event["period"].get("room") or event["item"].get("room", "")


def period_of(event: dict) -> str:
    p = event["period"]
    return period_label(event["item"], f"{p['start']}|{p['end']}")


def describe_statue(event: dict) -> str:
    item = event["item"]
    f = item.get("feature") or {}
    head = f"{item['name']}"
    bits = [b for b in (item.get("designation"), item.get("owner"), f.get("era") or item.get("era"), f.get("material"), f.get("height")) if b]
    if item.get("reason"):
        bits.append(item["reason"])
    line = f"- {head}（{'、'.join(bits)}）展示室: {event_room(event) or '?'} / 期間: {period_of(event)}"
    if f.get("note"):
        line += f"\n  特徴（館の資料より）: {f['note']}"
    return line


def swap_extra(museum: str, events: list[dict], today: date, kind: str) -> str:
    first = events[0]
    venue = venue_label(museum, event_room(first))
    if kind == "swap":
        lead = (f"これは、{MUSEUMS[museum]['name']}の平常展示（{MUSEUMS[museum]['gallery']}）で仏像の展示が替わることを知らせる投稿です"
                f"（今日は {today:%Y-%m-%d}）。展示される像は、次の一覧にあるものだけを使う（資料に書かれた範囲）:\n")
        tail = ("先頭の像を主役にして、像の名前・会期・館（展示室）を入れ、資料の「特徴」に書かれた見どころを 1 つ具体的に書く。"
                "展示が替わるのを楽しみに待つ気持ちで書く。特徴の記載がない像は、名前と指定・所蔵だけで触れる。")
    else:
        lead = (f"これは、{MUSEUMS[museum]['name']}の平常展示（{MUSEUMS[museum]['gallery']}）で展示が終わりに近づく像を知らせる投稿です"
                f"（今日は {today:%Y-%m-%d}）。対象の像は、次の一覧にあるものだけを使う（資料に書かれた範囲）:\n")
        tail = ("先頭の像を主役にして、像の名前・終了日・館（展示室）を入れ、資料の「特徴」に書かれた見どころを 1 つ具体的に書く。"
                "あと何日かを書いてよい。")
    theme = theme_text(events)
    return (lead + "\n".join(describe_statue(e) for e in events[:MAX_LISTED])
            + (f"\nテーマの解説（館の資料。ここに書かれた範囲で使う）: {theme}" if theme else "")
            + f"\n会場: {venue}\n" + tail + "\n" + STATUE_RULES)


def theme_text(events: list[dict]) -> str:
    """展示室のテーマの解説（館の資料。京博の「仏像入門」の説明など）。"""
    ctx = next((e["item"].get("context", "") for e in events if e["item"].get("context")), "")
    return ctx[:350]


def bundle_material(museum: str, events: list[dict], kind: str) -> str:
    text = "\n".join(describe_statue(e) for e in events[:MAX_LISTED])
    theme = theme_text(events)
    return (text + (f"\nテーマの解説（館の資料）: {theme}" if theme else ""))[:2200]


def post_ok(text: str, material: str) -> str | None:
    """生成した投稿文の検査。禁止語や、資料にない作者名があれば理由を返す。"""
    m = BANNED_RE.search(text)
    if m:
        return f"禁止語「{m.group(0)}」"
    if not attribution_ok(text, material):
        return "作者名が資料と合わない"
    return None


def make_bundle_candidate(client, usage, museum: str, events: list[dict], kind: str, data: dict, today: date,
                          dry_run: bool, topic: str, stats: dict) -> dict | str | None:
    """館・日ごとの 1 件の配信候補。成功したら item、基準未満や重複なら "recorded"、失敗は None。"""
    import curate_news

    events = sorted(events, key=lambda e: (e["item"]["rank"], e["item"]["name"]))
    header = HEADERS_BY_KIND[kind]
    first = events[0]["item"]
    material = bundle_material(museum, events, kind)
    names = "、".join(e["item"]["name"] for e in events[:MAX_LISTED])
    more = f" ほか{len(events) - MAX_LISTED}件" if len(events) > MAX_LISTED else ""
    now = datetime.now(JST)
    title = f"{MUSEUMS[museum]['name']} {MUSEUMS[museum]['gallery']}"
    known = [it for it in data["items"]
             if it.get("source") == SOURCE and it.get("fetched_at", "") >= (now - timedelta(days=7)).isoformat()
             and (it.get("ai") or {}).get("post_header") in kc.EXHIBIT_HEADERS_ALL
             and it["ai"].get("score", 0) >= CANDIDATE_THRESHOLD and not it["ai"].get("duplicate")]
    what = "新しく展示される" if kind == "swap" else "展示の終了が近い"
    scored = {
        "id": f"gallery-{museum}-{today:%Y%m%d}-{kind}", "title": f"{header.strip('【】')} {title}（{names}{more}）",
        "description": f"国立博物館の平常展示で{what}仏像。{material}", "url": first.get("page_url", ""),
        "source": "exhibit_calendar", "published_at": now.isoformat(timespec="seconds"),
    }
    curate_news.score_items(client, usage, [scored], known)
    ai = scored.get("ai")
    if not ai:
        print(f"  採点失敗（翌日にやり直し）: {title} {header}")
        return None
    if ai["duplicate"] or ai["score"] < CANDIDATE_THRESHOLD:
        print(f"  配信候補にせず記録のみ: {title} {header} {ai['score']}点{'・重複' if ai['duplicate'] else ''}")
        return "recorded"
    extra = swap_extra(museum, events, today, kind)
    item = {"url": first.get("page_url", ""), "title": title, "description": f"{what}仏像: {names}{more}"}
    text = curate_news.write_post(client, usage, item, header=header, extra=extra, fetch_body=False)
    problem = post_ok(text, material) if text else "投稿文なし"
    if text and problem:
        text = curate_news.write_post(client, usage, item, header=header,
                                      extra=extra + f"\n（前回の案は不適切でした: {problem}。避けて書き直す）", fetch_body=False)
        problem = post_ok(text, material) if text else "投稿文なし"
    if not text or problem:
        print(f"  投稿文を作れず（{problem}）: {title} {header}")
        return None
    cid = unique_id(data, scored["id"])
    cand = {
        "id": cid, "title": title, "url": first.get("page_url", ""), "source": SOURCE, "header": header, "hashtags": "",
        "fetched_at": now.isoformat(timespec="seconds"), "published_at": now.isoformat(timespec="seconds"),
        "image_url": next((e["item"].get("image_url", "") for e in events if e["item"].get("image_url")), ""),
        "ai": {
            "score": ai["score"], "category": ai.get("category") or "展覧会", "duplicate": False,
            "reason": f"国立博物館の{'展示替え' if kind == 'swap' else '展示終了まぢか'}（{len(events)}件）・{ai['reason']}",
            "post_text": text, "post_header": header, "post_format": curate_news.POST_FORMAT,
        },
    }
    if not dry_run:
        data["items"].insert(0, cand)
        if topic:
            curate_news.notify(topic, cand)
            cand["ai"]["notified"] = True
    return cand


def unique_id(data: dict, base: str) -> str:
    ids = {it.get("id") for it in data["items"]}
    if base not in ids:
        return base
    n = 2
    while f"{base}-{n}" in ids:
        n += 1
    return f"{base}-{n}"


def make_digest_candidate(client, usage, pending: list[dict], data: dict, today: date, dry_run: bool, topic: str) -> dict | None:
    """月曜に、指定のない・注目でもない展示替えを全館分 1 件にまとめる。採点はせず固定点。"""
    import curate_news

    pending = sorted(pending, key=lambda p: (p["museum"], p["start"] or "", p["name"]))[:DIGEST_MAX_ITEMS]
    lines = []
    for m in MUSEUM_ORDER:
        mine = [p for p in pending if p["museum"] == m]
        if mine:
            lines.append(f"## {MUSEUMS[m]['name']}（{MUSEUMS[m]['gallery']}）")
            lines += [f"- {p['name']}（{p['owner'] or '所蔵者の記載なし'}）{p['room']} / "
                      f"{period_label({}, (p['start'] or '') + '|' + (p['end'] or ''))}" for p in mine]
    material = "\n".join(lines)[:2500]
    first = pending[0]
    now = datetime.now(JST)
    title = "国立博物館 今週の展示替え"
    extra = ("これは、国立博物館の平常展示で仏像の展示が替わる・新しく並ぶものを、1 週間分まとめて知らせる投稿です"
             f"（今日は {today:%Y-%m-%d}）。次の一覧にある像だけを使う:\n{material}\n"
             "館名と像の名前をいくつか挙げ、会期・会場を入れる。全部を並べず、2〜3 件を選んで具体的に書く。" + STATUE_RULES)
    item = {"url": first["url"], "title": title, "description": material}
    text = curate_news.write_post(client, usage, item, header=HEADERS_BY_KIND["digest"], extra=extra, fetch_body=False)
    problem = post_ok(text, material) if text else "投稿文なし"
    if text and problem:
        text = curate_news.write_post(client, usage, item, header=HEADERS_BY_KIND["digest"],
                                      extra=extra + f"\n（前回の案は不適切でした: {problem}。避けて書き直す）", fetch_body=False)
        problem = post_ok(text, material) if text else "投稿文なし"
    if not text or problem:
        print(f"  まとめの投稿文を作れず（{problem}）")
        return None
    cand = {
        "id": unique_id(data, f"gallery-digest-{today:%Y%m%d}"), "title": title, "url": first["url"], "source": SOURCE,
        "header": HEADERS_BY_KIND["digest"], "hashtags": "", "fetched_at": now.isoformat(timespec="seconds"),
        "published_at": now.isoformat(timespec="seconds"), "image_url": "",
        "ai": {"score": DIGEST_SCORE, "category": "展覧会", "duplicate": False,
               "reason": f"国立博物館の展示替えの週のまとめ（{len(pending)}件・機械集計）",
               "post_text": text, "post_header": HEADERS_BY_KIND["digest"], "post_format": curate_news.POST_FORMAT},
    }
    if not dry_run:
        data["items"].insert(0, cand)
        if topic:
            curate_news.notify(topic, cand)
            cand["ai"]["notified"] = True
    return cand


# ---------------------------------------------------------------- 実行

def mark(events: list[dict], candidate_id: str, now_iso: str) -> None:
    for e in events:
        entry = {"at": now_iso, "candidate": candidate_id}
        if not candidate_id:
            entry["featureless"] = not has_feature(e["item"])   # 特徴が空のまま記録のみにした（補えたら 1 回だけ採点し直す）
        if e.get("rescore"):
            entry["rescored"] = SEARCH_VERSION   # 特徴を補ったあとの採点し直しは 1 回だけ（方式を変えたときは版を上げてやり直す）
        e["item"]["notified"][e["key"]] = entry


def has_feature(item: dict) -> bool:
    f = item.get("feature") or {}
    return bool(f.get("note") or f.get("material") or f.get("height"))


def rescore_events(mstate: dict, museum: str, today: date) -> list[dict]:
    """特徴が空のまま基準未満で「記録のみ」にした節目（候補にならなかったもの）を、補ったあとの採点し直しの対象として拾う。
    シード・続けて展示・まとめ待ちのものや、すでに採点し直したものは除く。展示がまだ終わっていないものだけ。"""
    out = []
    for iid, it in mstate.get("items", {}).items():
        for key, entry in it.get("notified", {}).items():
            if entry.get("candidate") or (entry.get("rescored") or 0) >= SEARCH_VERSION or not entry.get("featureless", True) \
                    or entry.get("seed") or entry.get("continued") or entry.get("digest"):
                continue
            kind, _, d = key.partition("@")
            if kind not in ("appear", "ending"):
                continue
            period = next((p for p in it["periods"] if (p["start"] or "open") == d or p["end"] == d), None)
            if not period or (to_date(period["end"]) and to_date(period["end"]) < today):
                continue
            out.append({"museum": museum, "item": it, "key": key, "kind": kind, "period": period, "rescore": True})
    return out


def run(state: dict, data: dict, today: date, client, usage, fetchers: dict, *, dry_run: bool = False, topic: str = "",
        fetch_page=None, titles: list[str] | None = None) -> dict:
    """館を 1 周する。state と data は呼び出し元が保存する（このなかでは書かない）。"""
    titles = titles if titles is not None else new_designation_titles(data, today)
    now_iso = datetime.now(JST).isoformat(timespec="seconds")
    stats = {"fetched": {}, "sculptures": {}, "failed": [], "undecided": 0, "seeded": [], "appear": 0, "ending": 0,
             "queued": 0, "made": 0, "digest": 0, "enriched": 0, "pruned": 0, "judge_calls": 0, "searches": 0, "rescored": 0}
    budget = [MAX_JUDGE_CALLS]
    sbudget = [MAX_SEARCHES_PER_RUN, 0]
    all_events: dict[str, list[dict]] = {}
    for museum in MUSEUM_ORDER:
        if museum not in fetchers:
            continue
        rows = fetchers[museum](today)
        if not rows:
            stats["failed"].append(museum)   # 取得できなかった館は state を変えない（消えたと誤解しない）
            continue
        stats["fetched"][museum] = len(rows)
        keep, und = resolve_sculptures(rows, state, client, usage, budget)
        stats["undecided"] += und
        stats["sculptures"][museum] = len(keep)
        if not keep:
            stats["failed"].append(museum)
            continue
        mstate = state["museums"].setdefault(museum, {})
        if not mstate.get("seeded"):
            stats["seeded"].append(museum)
        all_events[museum] = merge_museum(mstate, keep, museum, today, titles)
        stats["pruned"] += prune_state(mstate, today)
        if client:
            all_events[museum] += rescore_events(mstate, museum, today)
    stats["judge_calls"] = MAX_JUDGE_CALLS - budget[0]

    # 低優先（指定なし・注目でもない）は個別にせず、月曜のまとめ待ちにする
    pending = state["digest_pending"]
    for museum, events in all_events.items():
        low = [e for e in events if e["kind"] == "appear" and e["item"]["rank"] == 5]
        for e in low:
            it, p = e["item"], e["period"]
            pending.append({"id": it["id"], "museum": museum, "name": it["name"], "owner": it["owner"], "room": p.get("room") or it["room"],
                            "start": p["start"], "end": p["end"], "url": it.get("page_url", ""), "queued": today.isoformat()})
            mark([e], "", now_iso)
            it["notified"][e["key"]]["digest"] = True
            stats["queued"] += 1
        low_ids = {id(e) for e in low}
        all_events[museum] = [e for e in events if id(e) not in low_ids]

    enriched = 0
    for museum, events in all_events.items():
        appear = [e for e in events if e["kind"] == "appear"]
        ending = [e for e in events if e["kind"] == "ending"]
        stats["appear"] += sum(1 for e in appear if not e.get("rescore"))
        stats["ending"] += sum(1 for e in ending if not e.get("rescore"))
        for kind, evs in (("swap", appear), ("ending", ending)):
            if not evs:
                continue
            evs.sort(key=lambda e: (e["item"]["rank"], e["item"]["name"]))
            if client:   # 重要な像から、特徴の抽出（1 回の実行で上限まで）。作品ページがない像は公式サイトを検索して補う
                for e in evs[:MAX_LISTED]:
                    it = e["item"]
                    if ("feature" in it and not needs_search(it)) or enriched >= MAX_ENRICH_PER_RUN:
                        continue
                    enriched += 1
                    if enrich(client, usage, it, fetch_page, search=lambda i: search_features(client, usage, i, sbudget)):
                        stats["enriched"] += 1
                for e in list(evs):   # 採点し直しは、特徴を補えたものだけ。検索したのに補えなかったものは諦める
                    if e.get("rescore") and not has_feature(e["item"]):
                        evs.remove(e)
                        if ((e["item"].get("feature") or {}).get("searched") or 0) >= SEARCH_VERSION:
                            mark([e], "", now_iso)
                if not evs:
                    continue
                stats["rescored"] += sum(1 for e in evs if e.get("rescore"))
            if not client:
                print(f"  変化あり（AI なし）: {MUSEUMS[museum]['name']} {HEADERS_BY_KIND[kind]} {len(evs)}件 "
                      + "、".join(e["item"]["name"] for e in evs[:3]))
                continue
            res = make_bundle_candidate(client, usage, museum, evs, kind, data, today, dry_run, topic, stats)
            if res is None:
                continue   # 失敗した節目は通知済みにせず、翌日にやり直す
            mark(evs, res["id"] if isinstance(res, dict) else "", now_iso)
            if isinstance(res, dict):
                stats["made"] += 1
                print(f"  候補を作成: {MUSEUMS[museum]['name']} {HEADERS_BY_KIND[kind]} {len(evs)}件\n    {res['ai']['post_text']}")

    stats["searches"] = sbudget[1]

    # 月曜のまとめ
    cutoff = (today - timedelta(days=DIGEST_KEEP_DAYS)).isoformat()
    state["digest_pending"] = pending = [p for p in pending if p["queued"] >= cutoff and (not p["end"] or p["end"] >= today.isoformat())]
    if today.weekday() == 0 and pending and client:
        cand = make_digest_candidate(client, usage, pending, data, today, dry_run, topic)
        if cand:
            stats["digest"] = 1
            state["digest_pending"] = []
            print(f"  候補を作成: {HEADERS_BY_KIND['digest']} {len(pending)}件\n    {cand['ai']['post_text']}")
    return stats


# ---------------------------------------------------------------- 東博の「新指定 国宝・重要文化財」展を展覧会ウォッチへ

_TNM_RANGE_RE = re.compile(r"：\s*(\d{4}年\d+月\d+日[^～：]*～\s*\d{4}年\d+月\d+日)")
_TNM_LABEL_RE = re.compile(r"(?:特別展|特別企画|特集)\s+")
_TNM_ROOM_RE = re.compile(r"\s+(?:本館|平成館|表慶館|東洋館|法隆寺宝物館|黒田記念館)\b.*$")


def find_tnm_new_designation(texts: list[tuple[str, str]], today: date) -> dict | None:
    """東博の展示ページの本文（URL, 本文）から、これから始まる「新指定 国宝・重要文化財」展を探す。
    本文は「特集 ◯◯ 本館 特別1室 ：2027年1月2日（土） ～ 2027年3月1日（月）」の並び。"""
    for url, text in texts:
        t = clean(text)
        prev = 0
        for m in _TNM_RANGE_RE.finditer(t):
            seg = t[prev:m.start()]
            prev = m.end()
            if "新指定" not in seg:
                continue
            start, end = parse_range_jp(m.group(1))
            if not (start and start > today):
                continue
            name = _TNM_LABEL_RE.split(seg)[-1]
            name = clean(_TNM_ROOM_RE.sub("", name))
            return {"name": name or "新指定 国宝・重要文化財", "start": start, "end": end, "url": url}
    return None


def register_new_designation_exhibit(today: date, dry_run: bool) -> str:
    """見つけたら exhibit_watch の監視リストに加える（展覧会ウォッチが続報を追う）。"""
    import exhibit_watch as ew

    texts = []
    for cid in (1, 6, 7):
        url = f"{TNM_BASE}?controller=ctg&cid={cid}"
        soup = fetch_soup(url)
        if soup is not None:
            c = soup.find(id="contents2022") or soup
            for t in c(["script", "style"]):
                t.decompose()
            texts.append((url, c.get_text(" ", strip=True)))
    found = find_tnm_new_designation(texts, today)
    if not found:
        return "なし"
    museums = ec.load_json(ec.SOURCES_FILE, {}).get("museums", [])
    watch = ew.load_watch()
    r = ew.add_to_watch(watch, "東京国立博物館（東京都台東区）", found["name"], found["start"].isoformat(),
                        iso(found["end"]), found["url"], museums)
    if r == "added" and not dry_run:
        ew.save_watch(watch)
    return f"{found['name']} {found['start']}〜{found['end']}（{'監視リストに追加' if r == 'added' else '登録済み'}）"


# ---------------------------------------------------------------- 実行（CLI）

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ファイルに書かず、結果だけ表示する")
    ap.add_argument("--museum", default="", help="館を絞る（tnm,kyohaku,narahaku,kyuhaku をカンマ区切り）")
    args = ap.parse_args()
    now = datetime.now(JST)
    today = now.date()
    chosen = [m for m in MUSEUM_ORDER if not args.museum or m in args.museum.split(",")]
    state, data = load_state(), load_news_data()

    client = usage = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        from curate_news import Usage

        client, usage = anthropic.Anthropic(), Usage()
    else:
        print("::warning::ANTHROPIC_API_KEY が無いため、仏像の判定・特徴の抽出・配信候補の作成（AI）をスキップ", file=sys.stderr)

    topic = "" if args.dry_run else os.environ.get("NTFY_TOPIC", "").strip()
    stats = run(state, data, today, client, usage, {m: FETCHERS[m] for m in chosen}, dry_run=args.dry_run, topic=topic)
    for m in chosen:
        items = state["museums"].get(m, {}).get("items", {})
        if m in stats["fetched"]:
            print(f"{MUSEUMS[m]['name']}: 行{stats['fetched'][m]}件 → 仏像{stats['sculptures'][m]}件")
            for it in sorted(items.values(), key=lambda i: (i["rank"], i["name"]))[:3]:
                per = " / ".join(f"{p['start'] or '?'}〜{p['end'] or '?'}" for p in it["periods"])
                print(f"    例: {it['name']}（{it.get('designation') or '指定なし'}・{it['owner'] or '所蔵者なし'}）{it['room']} {per}")
    designation = ""
    if not args.museum:
        designation = register_new_designation_exhibit(today, args.dry_run)
    if not args.dry_run:
        save_news_data(data)
        save_state(state)
    cost = (usage.cost() if usage else 0.0) + stats["searches"] * SEARCH_COST
    counts = "・".join(f"{MUSEUMS[m]['name'][:2]}{stats['sculptures'][m]}" for m in stats["sculptures"])
    print(f"::notice::名品ギャラリーウォッチ 仏像 {counts or '取得なし'} / 取得失敗 {','.join(stats['failed']) or 'なし'}・"
          f"初回記録 {','.join(stats['seeded']) or 'なし'}・判断保留{stats['undecided']} / 新規展示{stats['appear']}・終了まぢか{stats['ending']}・"
          f"まとめ待ち{stats['queued']} / 配信候補{stats['made']}件・週まとめ{stats['digest']}件・特徴抽出{stats['enriched']}件（web検索{stats['searches']}回・採点し直し{stats['rescored']}件） / "
          f"東博の新指定展: {designation or '未確認'} / AI費用 約${cost:.3f}（上限見積り）"
          + ("（dry-run: 書き込みなし）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
