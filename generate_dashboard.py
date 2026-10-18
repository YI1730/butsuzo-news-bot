"""docs/data/news.json を読み込み、スマホ最適化の静的ダッシュボード HTML を生成する。

生成ファイル:
  docs/index.html        ダッシュボード本体（Tailwind CSS + localStorage）
  docs/manifest.json     PWA マニフェスト
  docs/sw.js             Service Worker（オフライン対応）
  docs/icons/icon-192.png  PWA アイコン（単色 PNG）
  docs/icons/icon-512.png  PWA アイコン（単色 PNG）
"""

import json
import struct
import urllib.parse
import zlib
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from news_store import CANDIDATE_THRESHOLD, NOISE_THRESHOLD, compose_news_post

DOCS_DIR = Path(__file__).parent / "docs"
NEWS_JSON_FILE = DOCS_DIR / "data" / "news.json"
# Streamlit 管理画面が編集する訪問記・予約投稿（PWA からも参照させる）
ARCHIVES_SRC = Path(__file__).parent / "data" / "archives.json"
ARCHIVES_DST = DOCS_DIR / "data" / "archives.json"
SCHEDULED_SRC = Path(__file__).parent / "data" / "scheduled_posts.json"
SCHEDULED_DST = DOCS_DIR / "data" / "scheduled_posts.json"
JST = timezone(timedelta(hours=9))

# ブランドカラー #00AE95 = RGB(0, 174, 149)
ICON_COLOR = (0, 174, 149)

SOURCE_LABELS: dict[str, str] = {
    "google_news":              "📰 ニュース",
    "official_site":            "🏛 公式サイト",
    "reminder":                 "⏰ リマインド",
    "bangumi_tv":               "📺 仏像TV",
    "exhibition":               "🏛 特別展",
    "exhibition_rss":           "🏛 特別展",
    "kanbutsu":                 "🛕 仏像公開",
    "amazon_goods":             "🛒 Amazon",
    "東京国立博物館":            "🏛 東京国博",
    "奈良国立博物館":            "🏛 奈良国博",
    "京都国立博物館":            "🏛 京都国博",
    "九州国立博物館":            "🏛 九州国博",
    "京都非公開文化財特別公開":  "⛩ 京都特別公開",
    "祈りの回廊":               "🙏 奈良秘仏",
}

# タブ別ソース定義
# 「特別展」タブは更新頻度が低いため「特別公開」タブにマージ（other 扱い）。
NEWS_TAB_SOURCES = {"google_news", "official_site", "reminder", "bangumi_tv"}
GOODS_TAB_SOURCES = {"amazon_goods"}
# EXHIBITION 系は OTHER に統合（other は明示的にこの集合の補集合として求める）
_EXHIBITION_SOURCES_MERGED = {"exhibition", "exhibition_rss"}

# 取り込みセッション区切りの閾値（秒）— これ以上 fetched_at が離れると新セッション扱い
SEPARATOR_THRESHOLD_SECONDS = 30 * 60


# ---------------------------------------------------------------------------
# PNG 生成（Pillow 不要）
# ---------------------------------------------------------------------------


def create_solid_png(size: int, color: tuple[int, int, int]) -> bytes:
    """指定サイズ・単色の PNG バイト列を純 Python で生成する。"""
    r, g, b = color

    def chunk(name: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)

    scanline = b"\x00" + bytes([r, g, b]) * size
    raw = scanline * size
    compressed = zlib.compress(raw, 9)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------


def format_fetched_at(iso: str) -> str:
    """ISO 8601 文字列を「M月D日 H:MM」形式に変換する。"""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%-m月%-d日 %H:%M")
    except Exception:
        return iso[:10] if iso else ""


def format_published_at(iso: str) -> str:
    """ISO 8601 の公開日を「M月D日」形式に変換する。空文字なら空文字を返す。"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%-m月%-d日")
    except Exception:
        return ""


def build_card_html(item: dict, show_copy_btn: bool = False) -> str:
    uid = item.get("id", "")
    raw_title = item.get("title", "")
    title = raw_title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # コピーボタン用に属性値として安全な形式（"を&quot;にエスケープ）
    title_for_attr = (
        raw_title.replace("&", "&amp;")
                 .replace('"', "&quot;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
    )
    url = item.get("url", "")
    source = item.get("source", "")
    header = item.get("header", "【仏像速報】")
    hashtags = item.get("hashtags", "#仏像")
    fetched_at = format_fetched_at(item.get("fetched_at", ""))
    published_at = format_published_at(item.get("published_at", ""))
    source_label = SOURCE_LABELS.get(source, f"📌 {source}")
    image_url = item.get("image_url", "")
    desc = item.get("description", "")
    desc_escaped = (
        desc.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

    # X Web Intent URL（description は JS 側で動的挿入するため、ここでは含めない）
    post_text = f"{header}\n{item.get('title', '')}\n{hashtags}"
    ai = item.get("ai") or {}
    if ai.get("post_text"):
        # AI（curate_news.py）が記事を読んで書いた文章 → 【仏像速報】記事タイトル（→ X が URL を付ける）
        post_text = compose_news_post(ai["post_text"], item.get("title", ""), ai.get("post_header", "【仏像速報】"))
    if source in ("google_news", "official_site", "reminder"):
        # ニュースの description は AI 採点用の要約。投稿文には差し込まない
        desc_escaped = ""
    intent_params = urllib.parse.urlencode({"text": post_text, "url": url})
    intent_url = f"https://x.com/intent/post?{intent_params}"

    # OGP 画像ブロック（image_url がある場合のみ）
    image_html = ""
    if image_url:
        img_url_escaped = image_url.replace('"', "&quot;")
        image_html = (
            f'      <img src="{img_url_escaped}" alt="" loading="lazy"\n'
            f'           class="w-full h-40 object-cover rounded-xl mb-3"\n'
            f'           onerror="this.style.display=\'none\'">\n'
        )

    # 公開日バッジ（published_at がある場合のみ）
    pub_badge_html = ""
    if published_at:
        pub_badge_html = f'<span class="text-xs text-gray-500 shrink-0">📅 {published_at}</span>'

    # メタ行：ソースラベル / 公開日 / 取込日
    if pub_badge_html:
        meta_html = (
            f'      <div class="flex items-center justify-between mb-2 gap-2">\n'
            f'        <span class="text-xs font-medium text-brand-800 bg-brand-50 px-2 py-0.5 rounded-full whitespace-nowrap">{source_label}</span>\n'
            f'        <div class="flex items-center gap-1.5 shrink-0">\n'
            f'          {pub_badge_html}\n'
            f'          <span class="text-gray-300">·</span>\n'
            f'          <span class="text-xs text-gray-400">取込 {fetched_at}</span>\n'
            f'        </div>\n'
            f'      </div>'
        )
    else:
        meta_html = (
            f'      <div class="flex items-center justify-between mb-2 gap-2">\n'
            f'        <span class="text-xs font-medium text-brand-800 bg-brand-50 px-2 py-0.5 rounded-full whitespace-nowrap">{source_label}</span>\n'
            f'        <span class="text-xs text-gray-400 shrink-0">{fetched_at}</span>\n'
            f'      </div>'
        )

    # AI 採点バッジ（採点済みのニュースのみ）
    ai_html = ""
    if "score" in ai:
        score = ai["score"]
        if score >= CANDIDATE_THRESHOLD and not ai.get("duplicate"):
            badge_cls = "bg-amber-100 text-amber-800 border-amber-300"
            label = f"⭐ {score}点"
        else:
            badge_cls = "bg-gray-100 text-gray-500 border-gray-200"
            label = f"AI {score}点" + ("・重複" if ai.get("duplicate") else "")
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ai_html = (
            f'      <div class="flex items-center gap-1.5 mb-2 flex-wrap">\n'
            f'        <span class="text-[11px] font-bold border px-2 py-0.5 rounded-full {badge_cls}">{label}</span>\n'
            f'        <span class="text-[11px] text-gray-500">{esc(ai.get("category", ""))}・{esc(ai.get("reason", ""))}</span>\n'
            f'      </div>\n'
        )
        if ai.get("post_text"):
            ai_html += (
                f'      <p class="text-xs text-gray-700 bg-amber-50 border border-amber-100 rounded-lg p-2 mb-3 '
                f'whitespace-pre-wrap leading-relaxed">{esc(post_text)}</p>\n'
            )

    # 書籍: 発売日と紹介価値（rank_books.py）のバッジ
    if item.get("_book_badge"):
        esc_b = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ai_html += (
            f'      <div class="flex items-center gap-1.5 mb-2 flex-wrap">\n'
            f'        <span class="text-[11px] font-bold border px-2 py-0.5 rounded-full bg-sky-50 text-sky-800 border-sky-200">{esc_b(item["_book_badge"])}</span>\n'
            f'        <span class="text-[11px] text-gray-500">{esc_b((item.get("book_ai") or {}).get("reason", ""))}</span>\n'
            f'      </div>\n'
        )

    # タイトル行：show_copy_btn=True のときはコピーボタンを横に並べる
    if show_copy_btn:
        title_block_html = (
            f'      <div class="flex items-start gap-2 mb-3">\n'
            f'        <p class="text-sm font-semibold text-gray-800 leading-relaxed flex-1">{title}</p>\n'
            f'        <button type="button" class="copy-title-btn shrink-0 text-gray-400 hover:text-brand-700 active:text-brand-800 transition-colors p-1 -m-1" '
            f'data-copy-text="{title_for_attr}" onclick="copyText(this)" title="タイトルをコピー" aria-label="タイトルをコピー">\n'
            f'          <svg class="w-4 h-4 fill-none stroke-current" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>\n'
            f'        </button>\n'
            f'      </div>'
        )
    else:
        title_block_html = (
            f'      <p class="text-sm font-semibold text-gray-800 leading-relaxed mb-3">{title}</p>'
        )

    return f"""    <div class="card bg-white rounded-2xl shadow-sm p-4 border border-brand-100 transition-opacity duration-300" data-item-id="{uid}">
{image_html}{meta_html}
{title_block_html}
{ai_html}
      <div class="flex items-center gap-3">
        <a href="{intent_url}" target="_blank" rel="noopener noreferrer"
           onclick="handlePostClick(event,'{uid}')"
           data-description="{desc_escaped}"
           class="post-btn flex-1 flex items-center justify-center gap-1.5 bg-black text-white text-sm font-bold py-2.5 px-4 rounded-full active:bg-gray-700 transition-colors">
          <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.253 5.622 5.911-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          にポスト
        </a>
        <a href="{url}" target="_blank" rel="noopener noreferrer"
           data-external-url="{url}"
           class="read-btn text-xs text-gray-400 underline underline-offset-2 shrink-0">記事を読む</a>
      </div>
    </div>"""


def build_calendar_html(days_ahead: int = 30) -> str:
    """御開帳カレンダー（kaicho_calendar.py）のうち、公開中と days_ahead 日以内に始まるもの。"""
    path = Path(__file__).parent / "data" / "kaicho_calendar.json"
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except Exception:
        return ""
    today = datetime.now(JST).date()
    horizon = (today + timedelta(days=days_ahead)).isoformat()
    t = today.isoformat()
    rows = []
    for e in events:
        start, end = e.get("start", ""), e.get("end", "")
        if (end and end < t) or (start and start > horizon) or (not end and start and start < t):
            continue
        rows.append(e)
    if not rows:
        return ""
    rows.sort(key=lambda e: e.get("start") or e.get("end"))
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    def md(d: str) -> str:
        return f"{int(d[5:7])}/{int(d[8:10])}" if d else "?"
    lines = []
    for e in rows:
        live = (not e.get("start") or e["start"] <= t)
        badge = ('<span class="text-[10px] font-bold text-white bg-brand-600 px-1.5 py-0.5 rounded">公開中</span>'
                 if live else "")
        lines.append(
            f'        <a href="{esc(e["source_url"])}" target="_blank" rel="noopener noreferrer" '
            f'class="flex items-start gap-2 py-2 border-b border-gray-100 last:border-0">\n'
            f'          <span class="text-xs font-bold text-brand-800 w-24 shrink-0">{md(e.get("start", ""))}〜{md(e.get("end", ""))}</span>\n'
            f'          <span class="text-xs text-gray-700 leading-snug">{badge} {esc(e.get("event_name") or e["source_title"])}'
            f'<span class="block text-[11px] text-gray-400">{esc(e.get("place", ""))}</span></span>\n'
            f'        </a>'
        )
    return (
        '    <details class="bg-white rounded-2xl border border-brand-100 shadow-sm p-3" open>\n'
        f'      <summary class="text-sm font-bold text-brand-800 cursor-pointer">📅 これからの公開（公開中・{days_ahead}日以内・{len(rows)}件）</summary>\n'
        '      <p class="text-[11px] text-gray-500 mt-1 mb-1">記事からAIが抜き出した公開期間。「あすから」「終了まであと数日」で自動的に配信候補になります</p>\n'
        '      <div>\n' + "\n".join(lines) + '\n      </div>\n    </details>'
    )


def build_separator_html(dt: datetime) -> str:
    """取り込み時刻を表す区切り線（カード間に挿入）を生成。"""
    label = dt.strftime("%-m月%-d日 %H:%M")
    return f"""    <div class="separator flex items-center gap-3 my-2">
      <div class="flex-1 h-px bg-brand-300"></div>
      <span class="text-[11px] font-semibold text-brand-700 bg-brand-50 px-3 py-0.5 rounded-full whitespace-nowrap shadow-sm border border-brand-200">📥 {label} 取込</span>
      <div class="flex-1 h-px bg-brand-300"></div>
    </div>"""


def build_book_cards(books: list[dict]) -> str:
    """書籍を「新刊（発売予定〜発売直後）」→「おすすめ（紹介する価値の高い順）」に並べる。
    紹介する価値は rank_books.py が Claude で採点した book_ai.score。"""
    from news_store import (BOOK_HIDE_BELOW as HIDE_BELOW, BOOK_NEW_MIN_SCORE as NEW_MIN_SCORE,
                            BOOK_NEW_RELEASE_DAYS as NEW_RELEASE_DAYS, book_release_date as release_date)

    today = datetime.now(JST).date()

    def score(b: dict) -> int:
        return (b.get("book_ai") or {}).get("score", 50)  # 未採点は中くらい扱い

    new, rest = [], []
    for b in books:
        if score(b) < HIDE_BELOW:
            continue
        rd = release_date(b)
        b = dict(b)
        if rd and rd > today:
            b["_book_badge"] = f"📅 {rd:%Y/%m/%d} 発売予定・予約受付中"
        elif rd:
            b["_book_badge"] = f"📅 {rd:%Y/%m/%d} 発売"
        else:
            b["_book_badge"] = "📅 発売日不明"
        if rd and (rd - today).days >= -NEW_RELEASE_DAYS and score(b) >= NEW_MIN_SCORE:
            new.append((rd, b))
        else:
            rest.append(b)
    new.sort(key=lambda t: t[0], reverse=True)
    rest.sort(key=lambda b: (score(b), (release_date(b) or date.min)), reverse=True)

    def heading(text: str, sub: str) -> str:
        return (f'    <p class="text-xs font-bold text-brand-800 px-1 pt-1">{text}'
                f'<span class="font-normal text-gray-500 ml-1">{sub}</span></p>')

    out = []
    if new:
        out.append(heading(f"🆕 新刊（{len(new)}冊）", "発売予定・発売60日以内"))
        out += [build_card_html(b) for _, b in new]
    if rest:
        out.append(heading(f"📖 おすすめ（{len(rest)}冊）", "仏像リンクとして紹介したい順"))
        out += [build_card_html(b) for b in rest]
    return "\n".join(out)


def build_cards_with_separators(items: list[dict], show_copy_btn: bool = False) -> str:
    """fetched_at が一定以上離れた境界に区切り線を挟みつつカードを並べる。

    items は fetched_at の降順でソート済みである前提。
    各セッション（同じ取り込み実行で追加されたグループ）の頭に区切り線を挿入する。

    show_copy_btn=True のときは、カードのタイトル横にコピーボタンを表示する
    （特別公開タブで使用）。
    """
    output: list[str] = []
    prev_fetched: datetime | None = None

    for item in items:
        fetched_str = item.get("fetched_at", "")
        try:
            fetched = datetime.fromisoformat(fetched_str) if fetched_str else None
        except Exception:
            fetched = None

        # 新セッションの先頭（または最初の項目）で区切り線を出す
        if fetched is not None:
            if prev_fetched is None:
                output.append(build_separator_html(fetched))
            else:
                delta = (prev_fetched - fetched).total_seconds()
                if delta > SEPARATOR_THRESHOLD_SECONDS:
                    output.append(build_separator_html(fetched))
            prev_fetched = fetched

        output.append(build_card_html(item, show_copy_btn=show_copy_btn))

    return "\n".join(output)


def build_html(items: list[dict], last_updated: str) -> str:
    lu_display = format_fetched_at(last_updated) if last_updated else "—"

    # fetched_at の降順でソート（最新が先頭）
    items_sorted = sorted(
        items,
        key=lambda x: x.get("fetched_at", ""),
        reverse=True,
    )

    # 4タブに分割: ニュース / 特別公開 / 書籍・グッズ / 訪問記
    # （旧「特別展」は更新頻度が低いため、特別公開タブ＝other にマージ済み）
    news_items  = [x for x in items_sorted if x.get("source") in NEWS_TAB_SOURCES]
    goods_items = [x for x in items_sorted if x.get("source") in GOODS_TAB_SOURCES]
    classified  = NEWS_TAB_SOURCES | GOODS_TAB_SOURCES
    other_items = [x for x in items_sorted if x.get("source") not in classified]

    # 書籍・グッズタブは item_type で 書籍 / グッズ に分割（書籍を上に表示）
    book_items  = [x for x in goods_items if x.get("item_type") == "book"]
    other_goods = [x for x in goods_items if x.get("item_type") != "book"]

    # AI がノイズと判定したニュースは表示しない。配信候補は上部の専用欄に出し、
    # 時系列の一覧からは外す（同じ data-item-id のカードが 2 枚並ぶと投稿済み記録が崩れるため）
    def ai_of(x: dict) -> dict:
        return x.get("ai") or {}

    def is_candidate(x: dict) -> bool:
        return ai_of(x).get("score", 0) >= CANDIDATE_THRESHOLD and not ai_of(x).get("duplicate")

    news_items = [x for x in news_items if ai_of(x).get("score", 100) >= NOISE_THRESHOLD]
    candidate_cutoff = (datetime.now(JST) - timedelta(hours=72)).isoformat()
    candidates = sorted(
        (x for x in news_items if is_candidate(x) and x.get("fetched_at", "") >= candidate_cutoff),
        key=lambda x: (ai_of(x)["score"], x.get("fetched_at", "")),
        reverse=True,
    )
    candidate_ids = {x["id"] for x in candidates}
    news_items = [x for x in news_items if x["id"] not in candidate_ids]
    candidates_html = ""
    if candidates:
        candidates_html = (
            '    <div class="rounded-2xl border-2 border-amber-300 bg-amber-50/60 p-3 space-y-3">\n'
            f'      <p class="text-sm font-bold text-amber-800">⭐ 配信候補（AIが選定・{len(candidates)}件）</p>\n'
            f'      <p class="text-[11px] text-amber-700 -mt-2">記事を読んで配信価値を採点し、投稿文も作成済み。「にポスト」で下書き入りのXが開きます</p>\n'
            + "\n".join(build_card_html(x) for x in candidates)
            + "\n    </div>"
        )
    news_cards  = candidates_html + "\n" + build_calendar_html() + "\n" + build_cards_with_separators(news_items)
    book_cards  = build_book_cards(book_items)
    goods_cards = build_cards_with_separators(other_goods)
    # 特別公開タブ（other）はタイトル横にコピーボタンを表示
    other_cards = build_cards_with_separators(other_items, show_copy_btn=True)
    news_count  = len(news_items) + len(candidates)
    goods_count = len(goods_items)
    book_count  = len(book_items)
    other_goods_count = len(other_goods)
    other_count = len(other_items)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#00AE95">
  <!-- PWA として「アプリのように」起動。iOS 17+ では standalone PWA でも
       target="_blank" の外部リンクは自動で Safari の新規タブで開かれるため、
       アプリ感と外部リンクの両立ができる。 -->
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="仏像ニュース">
  <link rel="manifest" href="./manifest.json">
  <!-- ファビコン群 -->
  <link rel="icon" href="./favicon.ico" sizes="any">
  <link rel="icon" type="image/png" sizes="16x16" href="./icons/favicon-16x16.png">
  <link rel="icon" type="image/png" sizes="32x32" href="./icons/favicon-32x32.png">
  <!-- iOS ホーム画面アイコン -->
  <link rel="apple-touch-icon" sizes="180x180" href="./icons/apple-touch-icon.png">
  <link rel="apple-touch-icon" sizes="192x192" href="./icons/icon-192.png">
  <link rel="apple-touch-icon" sizes="512x512" href="./icons/icon-512.png">
  <title>仏像ニュース ダッシュボード</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{
        extend: {{
          colors: {{
            brand: {{
              50:  '#e6f9f7',
              100: '#ccf3ee',
              200: '#99e7de',
              300: '#66dbce',
              400: '#33cfbe',
              500: '#00AE95',
              600: '#008e78',
              700: '#006e5c',
              800: '#004e41',
              900: '#002e26',
              950: '#001812',
            }}
          }}
        }}
      }}
    }}
  </script>
  <style>
    body {{ -webkit-tap-highlight-color: transparent; }}
    .card.is-posted {{ opacity: 0.45; }}
    .card.is-posted .post-btn {{
      background: #d1d5db !important;
      color: #6b7280 !important;
      /* pointer-events: none を外して再投稿可能にする */
    }}
    .card.is-posted .post-btn:active {{ background: #9ca3af !important; }}
  </style>
</head>
<body class="bg-brand-50 min-h-screen pb-8">

  <!-- ヘッダー -->
  <header class="bg-brand-600 text-white px-4 pt-safe-top sticky top-0 z-20 shadow-lg">
    <div class="flex items-center justify-between py-3 max-w-xl mx-auto">
      <div>
        <h1 class="text-base font-bold leading-tight">🛕 仏像ニュース</h1>
        <p class="text-xs text-brand-200 mt-0.5">更新: {lu_display}</p>
      </div>
      <span id="count" class="text-xs bg-brand-500 text-white px-2 py-1 rounded-full font-medium">0件</span>
    </div>

    <!-- タブ切替（セグメント形式・5タブ） -->
    <div class="max-w-xl mx-auto bg-brand-900 p-1 rounded-xl flex gap-1">
      <button onclick="setTab('news')" id="tab-btn-news"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        📰 ニュース<span class="opacity-60 ml-0.5">{news_count}</span>
      </button>
      <button onclick="setTab('replies')" id="tab-btn-replies"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        💬 返信<span id="replies-count" class="ml-0.5"></span>
      </button>
      <button onclick="setTab('other')" id="tab-btn-other"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        🛕 特別公開<span class="opacity-60 ml-0.5">{other_count}</span>
      </button>
      <button onclick="setTab('goods')" id="tab-btn-goods"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        🛒 書籍・グッズ<span class="opacity-60 ml-0.5">{goods_count}</span>
      </button>
      <button onclick="setTab('archive')" id="tab-btn-archive"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        📜 訪問記
      </button>
    </div>

    <!-- フィルターバー -->
    <div class="flex gap-2 py-2 pb-3 max-w-xl mx-auto">
      <button onclick="setFilter('unposted')" id="btn-unposted"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">未投稿</button>
      <button onclick="setFilter('all')" id="btn-all"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">すべて</button>
      <button onclick="setFilter('posted')" id="btn-posted"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">投稿済み</button>
      <button onclick="resetAll()" class="ml-auto text-xs text-brand-300 underline">リセット</button>
    </div>
  </header>

  <!-- カードリスト（タブごとに分離） -->
  <main class="px-4 py-4 max-w-xl mx-auto">
    <div id="tab-news" class="tab-pane space-y-3">
{news_cards}
    </div>
    <div id="tab-other" class="tab-pane space-y-3 hidden">
{other_cards}
    </div>
    <div id="tab-goods" class="tab-pane space-y-3 hidden">
      <!-- 書籍（上段） -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm">
        <p class="text-sm font-bold text-brand-800">📚 書籍 <span class="opacity-60 ml-0.5">{book_count}</span></p>
        <p class="text-xs text-gray-500 mt-0.5">仏像関連の新刊・書籍</p>
      </div>
{book_cards}
      <!-- グッズ（下段） -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm mt-4">
        <p class="text-sm font-bold text-brand-800">🛒 グッズ <span class="opacity-60 ml-0.5">{other_goods_count}</span></p>
        <p class="text-xs text-gray-500 mt-0.5">仏像関連のグッズ・雑貨</p>
      </div>
{goods_cards}
    </div>
    <!-- 訪問記タブ：週次ピック（訪問記14件）＋告知（予約投稿） -->
    <div id="tab-archive" class="tab-pane space-y-3 hidden">
      <!-- 予約の手順ガイド + Xの投稿画面を開くボタン -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm">
        <p class="text-sm font-bold text-brand-800">🗓 予約投稿の手順</p>
        <p class="text-xs text-gray-500 mt-0.5 leading-relaxed">
          ①カードの「📋 本文をコピー」（告知は画像も）→ ②下の「✍️ Xの投稿画面を開く」→
          ③本文を貼り付け・画像を貼り付け → ④時計アイコンで日時を指定して「予約」。<br>
          ※ 予約は<b>スマホのブラウザ</b>版 x.com でのみ動作します（Xアプリ／簡易投稿では不可）。
        </p>
        <a href="https://x.com/compose/post" target="_blank" rel="noopener noreferrer"
          class="mt-2 w-full flex items-center justify-center gap-1.5 bg-black text-white text-sm font-bold py-2.5 px-4 rounded-full active:bg-gray-700 transition-colors">
          <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.253 5.622 5.911-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          ✍️ Xの投稿画面を開く
        </a>
      </div>
      <!-- 訪問記ピックリスト（今週分・ランダム14件） -->
      <div class="flex items-center justify-between bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm">
        <div>
          <p class="text-sm font-bold text-brand-800">📜 訪問記ピック（今週分・14件）</p>
          <p class="text-xs text-gray-500 mt-0.5">「📋 本文をコピー」→ Xの投稿画面に貼り付けて日時予約</p>
        </div>
        <button onclick="shuffleArchive()"
          class="bg-brand-600 active:bg-brand-700 text-white text-xs font-bold px-3 py-2 rounded-full transition-colors shrink-0">
          🔀 引き直す
        </button>
      </div>
      <div id="archive-cards" class="space-y-3">
        <p class="text-center text-sm text-gray-400 py-6">読み込み中...</p>
      </div>

      <!-- 告知（予約投稿）: 1日2件の枠 -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm mt-4">
        <p class="text-sm font-bold text-brand-800">📣 告知（1日2件の枠）</p>
        <p class="text-xs text-gray-500 mt-0.5">「📋 本文をコピー」「📋 画像をコピー」→ Xの投稿画面に貼り付けて日時予約</p>
      </div>
      <div id="scheduled-cards" class="space-y-3">
        <p class="text-center text-sm text-gray-400 py-6">読み込み中...</p>
      </div>

      <!-- ⚙️ 自動投稿の設定（訪問の時刻・告知の曜日時刻をこの端末から編集） -->
      <details id="settings-panel" class="bg-white rounded-2xl shadow-sm border border-brand-100 mt-4">
        <summary class="text-sm font-bold text-brand-800 cursor-pointer select-none px-4 py-3">
          ⚙️ 自動投稿の設定（訪問の時刻・告知の登録）
        </summary>
        <div class="px-4 pb-4 space-y-4">

          <!-- GitHubトークン -->
          <div id="settings-token-block" class="bg-brand-50 rounded-xl p-3">
            <p class="text-xs font-bold text-brand-800 mb-1">🔑 GitHub トークン</p>
            <p class="text-[11px] text-gray-500 mb-2 leading-relaxed">
              設定の保存には、この butsuzo-news-bot リポジトリへの書き込み権限を持つ
              GitHub Personal Access Token が必要です。この端末のブラウザにのみ保存され、外部には送信されません。
            </p>
            <div class="flex gap-2">
              <input id="settings-token-input" type="password" placeholder="ghp_..."
                class="flex-1 min-w-0 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
                autocomplete="off">
              <button onclick="saveGithubToken()"
                class="shrink-0 bg-brand-600 active:bg-brand-700 text-white text-xs font-bold px-3 py-2 rounded-lg transition-colors">
                保存
              </button>
            </div>
            <p id="settings-token-status" class="text-[11px] text-gray-500 mt-1"></p>
          </div>

          <div id="settings-body" class="space-y-4">
            <p class="text-xs text-gray-400 text-center py-2">トークンを保存すると設定を編集できます</p>
          </div>
        </div>
      </details>
    </div>
    <!-- 返信タブ：仏像リンク宛ての返信を AI が仕分け（triage_replies.py） -->
    <div id="tab-replies" class="tab-pane space-y-3 hidden">
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm">
        <p class="text-sm font-bold text-brand-800">💬 返信すべき返信</p>
        <p class="text-xs text-gray-500 mt-0.5 leading-relaxed">
          仏像リンク宛ての返信を毎時取得し、AIが返信すべきものだけを選んで返信案を作っています。
          文面を直して「仏像リンクで返信」を押すと、1〜2分で仏像リンクのアカウントから送信されます。
        </p>
      </div>
      <div id="replies-cards" class="space-y-3">
        <p class="text-center text-sm text-gray-400 py-6">読み込み中...</p>
      </div>
      <!-- 反応したい投稿：X リストの新着から AI が選ぶ（pick_reactions.py）。データは非公開リポジトリにあり、
           この端末の GitHub トークンで読む。いいね・リポストは自動では押さず、タップで X を開く -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm mt-4">
        <p class="text-sm font-bold text-brand-800">👍 反応したい投稿</p>
        <p class="text-xs text-gray-500 mt-0.5 leading-relaxed">
          AIが1日最大3件まで選び、返信案も付けています。返信は「返信文をコピーしてXで開く」→Xで「返信」を押して貼り付け。
          「いいね」「リポスト」もXが開くので、そこで押してください（Xアプリは仏像リンクのアカウントにしておいてください）。
        </p>
      </div>
      <div id="reactions-cards" class="space-y-3">
        <p class="text-center text-sm text-gray-400 py-6">読み込み中...</p>
      </div>
    </div>
  </main>

  <!-- フッター: ニュース検索キーワード設定（クライアント側フィルター） -->
  <footer class="px-4 pb-8 pt-2 max-w-xl mx-auto">
    <details id="kw-panel" class="bg-white rounded-2xl shadow-sm p-4 border border-brand-100">
      <summary class="text-sm font-bold text-brand-800 cursor-pointer select-none flex items-center justify-between">
        <span>🔍 ニュース検索キーワード</span>
        <span id="kw-status" class="text-xs font-normal text-gray-400"></span>
      </summary>
      <div class="mt-3 space-y-3">
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">
            含むキーワード <span class="text-gray-400">（カンマ区切り・いずれか1つ含めば表示）</span>
          </label>
          <input id="kw-include" type="text" placeholder="例: 仏像, 如来, 菩薩, 重要文化財"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            autocomplete="off" inputmode="search">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-700 mb-1">
            除外キーワード <span class="text-gray-400">（いずれか1つでも含めば非表示）</span>
          </label>
          <input id="kw-exclude" type="text" placeholder="例: グラビア, ヌード, ゲーム"
            class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            autocomplete="off" inputmode="search">
        </div>
        <div class="flex gap-2">
          <button onclick="applyKeywords()"
            class="flex-1 bg-brand-600 text-white text-sm font-bold py-2 rounded-lg active:bg-brand-700 transition-colors">
            適用
          </button>
          <button onclick="clearKeywords()"
            class="px-4 bg-gray-100 text-gray-700 text-sm font-bold py-2 rounded-lg active:bg-gray-200 transition-colors">
            クリア
          </button>
        </div>
        <p class="text-[11px] text-gray-500 leading-relaxed">
          ※ 取り込み済みの<strong>ニュースタブ</strong>記事のタイトルに対して適用されるフィルターです。GitHub Actions の取得自体には影響しません。
        </p>
      </div>
    </details>
  </footer>

  <script>
    let currentTab = 'news';
    let currentFilter = 'all';
    let archiveData = null;    // data/archives.json をキャッシュ
    let scheduledData = null;  // data/scheduled_posts.json をキャッシュ

    function setTab(tab) {{
      currentTab = tab;
      ['news','other','goods','archive','replies'].forEach(t => {{
        const btn = document.getElementById('tab-btn-' + t);
        if (btn) {{
          btn.className = 'tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight ' +
            (t === tab ? 'bg-white text-brand-800 shadow' : 'text-brand-200');
        }}
        const pane = document.getElementById('tab-' + t);
        if (pane) pane.classList.toggle('hidden', t !== tab);
      }});
      // 訪問記タブを開いたタイミングで初回ロード
      if (tab === 'archive') {{
        if (!archiveData) {{ loadArchive(); }} else {{ renderArchive(); }}
        if (!scheduledData) {{ loadScheduled(); }} else {{ renderScheduled(); }}
      }}
      if (tab === 'replies') {{ loadReplies(); }}
      applyFilter();
      window.scrollTo({{top: 0, behavior: 'instant'}});
    }}

    function setFilter(mode) {{
      currentFilter = mode;
      ['unposted','all','posted'].forEach(m => {{
        const btn = document.getElementById('btn-' + m);
        if (btn) {{
          btn.className = 'filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors ' +
            (m === mode ? 'bg-white text-brand-800' : 'bg-brand-700 text-brand-200');
        }}
      }});
      applyFilter();
    }}

    function parseKeywords(s) {{
      return (s || '').split(/[,、\s]+/).map(k => k.trim()).filter(Boolean);
    }}

    function matchKeywords(title, includes, excludes) {{
      for (const kw of excludes) {{
        if (title.indexOf(kw) !== -1) return false;
      }}
      if (includes.length === 0) return true;
      return includes.some(kw => title.indexOf(kw) !== -1);
    }}

    function getActiveKeywords() {{
      const incEl = document.getElementById('kw-include');
      const excEl = document.getElementById('kw-exclude');
      return {{
        inc: parseKeywords(incEl ? incEl.value : ''),
        exc: parseKeywords(excEl ? excEl.value : ''),
      }};
    }}

    function updateKeywordStatus() {{
      const status = document.getElementById('kw-status');
      if (!status) return;
      const {{ inc, exc }} = getActiveKeywords();
      if (inc.length === 0 && exc.length === 0) {{
        status.textContent = '';
      }} else {{
        const parts = [];
        if (inc.length) parts.push('含 ' + inc.length);
        if (exc.length) parts.push('除 ' + exc.length);
        status.textContent = '(' + parts.join(' / ') + ')';
      }}
    }}

    function applyFilter() {{
      const activePane = document.getElementById('tab-' + currentTab);
      if (!activePane) return;

      if (currentTab === 'replies') {{
        const n = document.querySelectorAll('#replies-cards [data-reply-id]').length;
        document.getElementById('count').textContent = n + '件';
        return;
      }}

      // 訪問記タブはフィルター対象外（カードは archive-cards / scheduled-cards 配下で動的生成）
      if (currentTab === 'archive') {{
        const archiveCards = document.getElementById('archive-cards');
        const scheduledCards = document.getElementById('scheduled-cards');
        const aShown = archiveCards ? archiveCards.querySelectorAll('[data-archive-id]').length : 0;
        const sShown = scheduledCards ? scheduledCards.querySelectorAll('[data-scheduled-id]').length : 0;
        document.getElementById('count').textContent = (aShown + sShown) + '件';
        return;
      }}

      // ニュースタブのみキーワードフィルターを適用
      const useKw = (currentTab === 'news');
      const {{ inc, exc }} = useKw ? getActiveKeywords() : {{ inc: [], exc: [] }};

      let shown = 0;
      activePane.querySelectorAll('[data-item-id]').forEach(card => {{
        const posted = card.classList.contains('is-posted');
        let show = true;
        if (currentFilter === 'unposted') show = !posted;
        if (currentFilter === 'posted')   show = posted;

        // キーワードフィルター（ニュースタブのみ）
        if (show && useKw && (inc.length || exc.length)) {{
          const titleEl = card.querySelector('p.text-sm.font-semibold');
          const title = titleEl ? titleEl.textContent : '';
          if (!matchKeywords(title, inc, exc)) show = false;
        }}

        card.style.display = show ? '' : 'none';
        if (show) shown++;
      }});
      // 区切り線（separator）は、その配下に表示中カードが1枚も無ければ隠す
      const children = Array.from(activePane.children);
      for (let i = 0; i < children.length; i++) {{
        const el = children[i];
        if (!el.classList.contains('separator')) continue;
        let hasVisible = false;
        for (let j = i + 1; j < children.length; j++) {{
          const next = children[j];
          if (next.classList.contains('separator')) break;
          if (next.style.display !== 'none') {{
            hasVisible = true;
            break;
          }}
        }}
        el.style.display = hasVisible ? '' : 'none';
      }}
      document.getElementById('count').textContent = shown + '件';
    }}

    // ────────────────────────────────────────────────────────
    // 外部リンクを「OSの規定ブラウザ」で開く（PWA 内蔵ブラウザ回避）
    //
    // Android (standalone PWA) では target="_blank" でも Chrome Custom Tabs
    // にリンクが閉じ込められる。intent:// URI で OS のリンクハンドラに渡すと
    // 規定のブラウザアプリ（Chrome 等）で開く。
    //
    // iOS / 通常ブラウザは preventDefault せず、href の target="_blank" に任せる
    // （iOS 17+ は standalone PWA から自動で Safari の新規タブで開く）。
    // ────────────────────────────────────────────────────────
    function isStandalonePWA() {{
      try {{
        if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
        if (window.navigator && window.navigator.standalone === true) return true;
      }} catch (e) {{}}
      return false;
    }}

    // Android standalone PWA では target="_blank" が Custom Tabs に閉じ込められる。
    // 起動時に該当環境を判定し、外部リンクの href 自体を intent:// に書き換える。
    // これにより Chrome が「intent URI = 外部アプリ起動」として処理し、
    // ユーザーの規定ブラウザでリンクが開かれる。
    function buildIntentUrl(url) {{
      const u = new URL(url, location.href);
      const scheme = (u.protocol || 'https:').replace(':', '');
      const fallback = encodeURIComponent(url);
      return (
        'intent://' + u.host + u.pathname + u.search + u.hash +
        '#Intent;scheme=' + scheme +
        ';action=android.intent.action.VIEW' +
        ';category=android.intent.category.BROWSABLE' +
        ';S.browser_fallback_url=' + fallback +
        ';end'
      );
    }}

    function rewriteExternalLinksForAndroidPWA() {{
      const ua = navigator.userAgent || '';
      if (!isStandalonePWA()) return;
      if (!/Android/i.test(ua)) return;
      document.querySelectorAll('a.read-btn[data-external-url]').forEach(function (a) {{
        const original = a.getAttribute('data-external-url');
        if (!original) return;
        try {{
          const intentUrl = buildIntentUrl(original);
          a.setAttribute('href', intentUrl);
          // intent URI は新規タブ不要・noopener 付与で逆に挙動が崩れる場合があるので解除
          a.removeAttribute('target');
          a.removeAttribute('rel');
        }} catch (e) {{}}
      }});
    }}

    // ────────────────────────────────────────────────────────
    // 過去アーカイブつぶやき: data/archives.json を fetch し、
    // ランダムに 3〜5 件選んでカード表示。各カードに X Web Intent ボタン。
    // ────────────────────────────────────────────────────────
    // ────────────────────────────────────────────────────────
    // 💬 返信タブ: data/replies.json を表示し、返信／返信不要を data/reply_queue.json に積む
    // （reply_send.yml が push を検知して仏像リンクのアカウントから送信する）
    // ────────────────────────────────────────────────────────
    let repliesData = null;
    const REPLY_QUEUE_PATH = 'data/reply_queue.json';
    const PRIORITY_ORDER = {{ '高': 0, '中': 1, '低': 2 }};

    function _replyQueued(id) {{
      try {{ return localStorage.getItem('reply_queued_' + id) === '1'; }} catch (e) {{ return false; }}
    }}

    function loadReplies() {{
      _fetchJson(_RAW_BASE + '/data/replies.json', './data/replies.json')
        .then(function (d) {{ repliesData = (d && Array.isArray(d.items)) ? d.items : []; renderReplies(); }})
        .catch(function () {{ repliesData = []; renderReplies(); }});
      loadReactions();
    }}

    function _replyCardHtml(it, open) {{
      const ai = it.ai || {{}};
      const when = (it.created_at || '').replace('T', ' ').slice(5, 16);
      const tweetUrl = 'https://x.com/' + encodeURIComponent(it.author_username || 'i') + '/status/' + it.id;
      const pri = ai.priority || '-';
      const priCls = pri === '高' ? 'bg-red-100 text-red-700' : (pri === '中' ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-500');
      let h = '<div class="bg-white rounded-2xl shadow-sm p-4 border border-brand-100" data-reply-id="' + escapeHtml(it.id) + '">';
      h += '<div class="flex items-center gap-1.5 mb-2 flex-wrap">' +
        '<span class="text-[11px] font-bold px-2 py-0.5 rounded-full ' + priCls + '">' + escapeHtml(pri) + '</span>' +
        '<span class="text-[11px] text-gray-500">' + escapeHtml(ai.category || '') + '・' + escapeHtml(ai.reason || '') + '</span></div>';
      h += '<p class="text-xs text-gray-500 mb-1">@' + escapeHtml(it.author_username || '') + '（' + escapeHtml(it.author_name || '') + '）・' + escapeHtml(when) + '</p>';
      h += '<p class="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap mb-2">' + escapeHtml(it.text || '') + '</p>';
      if (it.parent_text) {{
        h += '<p class="text-[11px] text-gray-500 bg-gray-50 rounded-lg p-2 mb-2 whitespace-pre-wrap">↪ ' +
          (it.parent_is_ours ? '仏像リンクの投稿: ' : '') + escapeHtml(it.parent_text.slice(0, 140)) + '</p>';
      }}
      if (!open) {{
        h += '<a href="' + tweetUrl + '" target="_blank" rel="noopener noreferrer" class="text-xs text-gray-400 underline">Xで見る</a></div>';
        return h;
      }}
      h += '<textarea data-role="reply-text" rows="3" class="w-full text-sm border border-gray-300 rounded-lg p-2 mb-1 focus:outline-none focus:ring-2 focus:ring-brand-500" placeholder="返信文を入力">' +
        escapeHtml(ai.reply_draft || '') + '</textarea>';
      h += '<p class="reply-status text-[11px] text-gray-500 mb-2"></p>';
      h += '<div class="flex gap-2 items-center">' +
        '<button type="button" onclick="queueReply(this, \\'reply\\')" class="flex-1 bg-black active:bg-gray-700 text-white text-sm font-bold py-2.5 rounded-full">仏像リンクで返信</button>' +
        '<button type="button" onclick="queueReply(this, \\'dismiss\\')" class="shrink-0 bg-gray-100 active:bg-gray-200 text-gray-600 text-xs font-bold py-2.5 px-3 rounded-full">返信不要</button>' +
        '<a href="' + tweetUrl + '" target="_blank" rel="noopener noreferrer" class="shrink-0 text-xs text-gray-400 underline">Xで見る</a>' +
        '</div></div>';
      return h;
    }}

    function renderReplies() {{
      const box = document.getElementById('replies-cards');
      const items = (repliesData || []).filter(function (it) {{ return it.status === 'open' && !_replyQueued(it.id) && it.ai; }});
      const need = items.filter(function (it) {{ return it.ai.needs_reply; }})
        .sort(function (a, b) {{
          return (PRIORITY_ORDER[a.ai.priority] - PRIORITY_ORDER[b.ai.priority]) || (b.created_at || '').localeCompare(a.created_at || '');
        }});
      const skip = items.filter(function (it) {{ return !it.ai.needs_reply; }});
      window._replyNeedCount = need.length;
      _updateRepliesBadge();
      if (!box) return;
      let h = need.length ? need.map(function (it) {{ return _replyCardHtml(it, true); }}).join('')
        : '<p class="text-center text-sm text-gray-400 py-6">返信すべき返信はありません。</p>';
      if (skip.length) {{
        h += '<details class="bg-white rounded-2xl border border-gray-200 p-3"><summary class="text-xs text-gray-500 cursor-pointer">AIが返信不要と判定（' +
          skip.length + '件）※返信したいものは「返信する」で戻せます</summary><div class="space-y-2 mt-2">' +
          skip.map(function (it) {{
            return _replyCardHtml(it, false).replace('</div>', '') +
              '<button type="button" onclick="promoteReply(\\'' + escapeHtml(it.id) + '\\')" class="text-xs text-brand-700 underline ml-3">返信する</button></div>';
          }}).join('') + '</div></details>';
      }}
      box.innerHTML = h;
      if (currentTab === 'replies') applyFilter();
    }}

    // 👍 反応したい投稿: 非公開リポジトリ butsuzo-private の reactions.json を、この端末のトークンで読む
    const PRIVATE_REPO = 'butsuzo-private';
    const REACTION_SHOW_HOURS = 72;
    let reactionsData = null;
    let reactionsSkipped = [];
    let reactionsLastRun = null;
    let reactionsError = '';

    function _reactionDone(id) {{
      try {{ return localStorage.getItem('reaction_done_' + id) === '1'; }} catch (e) {{ return false; }}
    }}

    function loadReactions() {{
      const token = getGithubToken();
      if (!token) {{
        reactionsData = []; reactionsError = '表示するには、訪問記タブの「⚙️ 自動投稿の設定」で GitHub トークンを保存してください。';
        renderReactions(); return;
      }}
      fetch(GH_API + '/repos/' + GH_OWNER + '/' + PRIVATE_REPO + '/contents/reactions.json?t=' + Date.now(), {{
        headers: {{ 'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json' }},
        cache: 'no-store',
      }}).then(function (r) {{
        if (r.status === 404 || r.status === 403) throw new Error('perm');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }}).then(function (body) {{
        const d = JSON.parse(base64ToUtf8(body.content || ''));
        reactionsData = Array.isArray(d.items) ? d.items : []; reactionsError = '';
        reactionsSkipped = Array.isArray(d.skipped) ? d.skipped : [];
        reactionsLastRun = d.last_run || null;
        renderReactions();
      }}).catch(function (e) {{
        reactionsData = [];
        reactionsError = e.message === 'perm'
          ? '保存済みの GitHub トークンに butsuzo-private の読み取り権限がありません（トークンの設定で追加してください）。'
          : '読み込みに失敗しました（' + e.message + '）';
        renderReactions();
      }});
    }}

    function _reactionCardHtml(it) {{
      const dt = new Date(it.created_at || '');
      const pad = function (n) {{ return String(n).padStart(2, '0'); }};
      const when = isNaN(dt) ? '' : pad(dt.getMonth() + 1) + '/' + pad(dt.getDate()) + ' ' + pad(dt.getHours()) + ':' + pad(dt.getMinutes());
      const likeUrl = 'https://x.com/intent/like?tweet_id=' + encodeURIComponent(it.id);
      const rtUrl = 'https://x.com/intent/retweet?tweet_id=' + encodeURIComponent(it.id);
      let h = '<div class="bg-white rounded-2xl shadow-sm p-4 border border-brand-100" data-reaction-id="' + escapeHtml(it.id) + '">';
      h += '<div class="flex items-center gap-2 mb-2">';
      if (it.icon) h += '<img src="' + escapeHtml(it.icon) + '" alt="" class="w-7 h-7 rounded-full" loading="lazy" onerror="this.style.display=\\'none\\'">';
      h += '<p class="text-xs text-gray-600 flex-1 min-w-0 truncate"><b>' + escapeHtml(it.name || '') + '</b> @' + escapeHtml(it.username || '') + '・' + escapeHtml(when) + '</p></div>';
      h += '<p class="text-[11px] text-sky-800 bg-sky-50 border border-sky-100 rounded-full inline-block px-2 py-0.5 mb-2">' +
        (it.action === 'repost' ? '🔁 リポストも向き' : '♡ いいね向き') + '・' + escapeHtml(it.reason || '') + '</p>';
      h += '<p class="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap mb-2">' + escapeHtml(it.text || '') + '</p>';
      if (it.image) h += '<img src="' + escapeHtml(it.image) + '" alt="" loading="lazy" class="w-full h-40 object-cover rounded-xl mb-2" onerror="this.style.display=\\'none\\'">';
      h += '<textarea data-role="reaction-reply" rows="2" class="w-full text-sm border border-gray-300 rounded-lg p-2 mb-2 focus:outline-none focus:ring-2 focus:ring-brand-500" placeholder="返信する場合は文章を入力">' +
        escapeHtml(it.reply_draft || '') + '</textarea>';
      h += '<p class="reaction-status text-[11px] text-gray-500 mb-1"></p>';
      h += '<a href="' + escapeHtml(it.url || '') + '" target="_blank" rel="noopener noreferrer" onclick="copyReactionReply(this)" ' +
        'class="block w-full text-center bg-black active:bg-gray-700 text-white text-sm font-bold py-2.5 rounded-full mb-2">📋 返信文をコピーしてXで開く</a>';
      h += '<div class="flex gap-2 items-center flex-wrap">' +
        '<a href="' + likeUrl + '" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-pink-600 active:bg-pink-700 text-white text-sm font-bold py-2.5 rounded-full">♡ いいね</a>';
      if (it.action === 'repost') {{
        h += '<a href="' + rtUrl + '" target="_blank" rel="noopener noreferrer" class="flex-1 text-center bg-emerald-600 active:bg-emerald-700 text-white text-sm font-bold py-2.5 rounded-full">🔁 リポスト</a>';
      }}
      h += '<a href="' + escapeHtml(it.url || '') + '" target="_blank" rel="noopener noreferrer" class="shrink-0 text-xs text-gray-500 underline">Xで開く</a>' +
        '<button type="button" onclick="markReactionDone(\\'' + escapeHtml(it.id) + '\\')" class="shrink-0 bg-gray-100 active:bg-gray-200 text-gray-600 text-xs font-bold py-2 px-3 rounded-full">対応済み</button>' +
        '</div></div>';
      return h;
    }}

    function renderReactions() {{
      const box = document.getElementById('reactions-cards');
      const cutoff = new Date(Date.now() - REACTION_SHOW_HOURS * 3600 * 1000).toISOString();
      const items = (reactionsData || []).filter(function (it) {{
        return !_reactionDone(it.id) && (it.created_at || '') >= cutoff;
      }}).sort(function (a, b) {{
        return (a.rank - b.rank) || (b.created_at || '').localeCompare(a.created_at || '');
      }});
      window._reactionCount = items.length;
      _updateRepliesBadge();
      if (!box) return;
      if (reactionsError) {{ box.innerHTML = '<p class="text-center text-xs text-gray-400 py-4 px-2">' + escapeHtml(reactionsError) + '</p>'; return; }}
      let h = items.length ? items.map(_reactionCardHtml).join('')
        : '<p class="text-center text-sm text-gray-400 py-6">いまは反応したい投稿はありません。</p>';
      const lr = reactionsLastRun;
      if (lr) {{
        const t = new Date(lr.at);
        const ts = isNaN(t) ? '' : (t.getMonth() + 1) + '/' + t.getDate() + ' ' + String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
        h += '<p class="text-center text-[11px] text-gray-400 -mt-1">最終取得 ' + ts + '・読み取り ' + lr.reads + '件・新しい投稿 ' + lr.fresh + '件・提案 ' + lr.picked + '件' +
          (lr.capped ? '（今日の読み取り上限に達したため読んでいません）' : '') + '</p>';
      }}
      // 対応済みにした投稿（誤って押したときに戻せるように）
      const done = (reactionsData || []).filter(function (it) {{
        return _reactionDone(it.id) && (it.created_at || '') >= cutoff;
      }});
      if (done.length) {{
        h += '<details class="bg-white rounded-2xl border border-gray-200 p-3"><summary class="text-xs text-gray-500 cursor-pointer">対応済みにした投稿（' +
          done.length + '件）※間違えて押したものは「戻す」で元に戻せます</summary><div class="space-y-2 mt-2">' +
          done.map(function (it) {{
            return '<div class="border-t border-gray-100 pt-2 flex items-start gap-2"><p class="text-xs text-gray-600 flex-1 min-w-0">' +
              escapeHtml(it.name || '') + '・' + escapeHtml(it.reason || '') + '</p>' +
              '<button type="button" onclick="undoReactionDone(\\'' + escapeHtml(it.id) + '\\')" class="shrink-0 bg-brand-600 active:bg-brand-700 text-white text-xs font-bold py-1.5 px-3 rounded-full">戻す</button></div>';
          }}).join('') + '</div></details>';
      }}
      if (reactionsSkipped.length) {{
        h += '<details class="bg-white rounded-2xl border border-gray-200 p-3"><summary class="text-xs text-gray-500 cursor-pointer">AIが見送った投稿（' +
          reactionsSkipped.length + '件・直近3日）</summary><div class="space-y-2 mt-2">' +
          reactionsSkipped.map(function (it) {{
            return '<div class="border-t border-gray-100 pt-2"><p class="text-[11px] text-gray-500">' + escapeHtml(it.name || '') + ' @' + escapeHtml(it.username || '') +
              '・見送り理由: ' + escapeHtml(it.reason || '') + '</p><p class="text-xs text-gray-700 whitespace-pre-wrap">' + escapeHtml(it.text || '') +
              '</p><a href="' + escapeHtml(it.url || '') + '" target="_blank" rel="noopener noreferrer" class="text-[11px] text-gray-400 underline">Xで開く</a></div>';
          }}).join('') + '</div></details>';
      }}
      box.innerHTML = h;
    }}

    // 返信: 文章をコピーして元の投稿を X で開く。X で「返信」を押して貼り付け、本人が送信する
    // （X アプリは intent の in_reply_to を無視して新規投稿として開くため、この方式にしている。費用はかからない）
    function copyReactionReply(link) {{
      const card = link.closest('[data-reaction-id]');
      const ta = card.querySelector('[data-role="reaction-reply"]');
      const status = card.querySelector('.reaction-status');
      const text = ta ? ta.value.trim() : '';
      if (!text) return;  // 文章が空なら投稿を開くだけ
      try {{
        navigator.clipboard.writeText(text).then(function () {{
          if (status) status.textContent = '✓ コピーしました。Xで「返信」を押して貼り付けてください';
        }}, function () {{
          if (status) status.textContent = 'コピーできませんでした。返信文を長押ししてコピーしてください';
        }});
      }} catch (e) {{
        if (status) status.textContent = 'コピーできませんでした。返信文を長押ししてコピーしてください';
      }}
    }}

    function undoReactionDone(id) {{
      try {{ localStorage.removeItem('reaction_done_' + id); }} catch (e) {{}}
      renderReactions();
    }}

    function markReactionDone(id) {{
      try {{ localStorage.setItem('reaction_done_' + id, '1'); }} catch (e) {{}}
      renderReactions();
    }}

    function _updateRepliesBadge() {{
      const badge = document.getElementById('replies-count');
      const n = (window._replyNeedCount || 0) + (window._reactionCount || 0);
      if (badge) badge.textContent = n ? ' ' + n : '';
    }}

    function promoteReply(id) {{
      const it = (repliesData || []).find(function (x) {{ return x.id === id; }});
      if (it && it.ai) {{ it.ai.needs_reply = true; it.ai.priority = it.ai.priority === '低' ? '中' : it.ai.priority; }}
      renderReplies();
    }}

    function queueReply(btn, action) {{
      const card = btn.closest('[data-reply-id]');
      const id = card.getAttribute('data-reply-id');
      const status = card.querySelector('.reply-status');
      const ta = card.querySelector('[data-role="reply-text"]');
      const text = ta ? ta.value.trim() : '';
      if (!getGithubToken()) {{
        status.textContent = '送信には GitHub トークンが必要です（📜訪問記タブ下の「⚙️ 自動投稿の設定」で保存）';
        return;
      }}
      if (action === 'reply') {{
        if (!text) {{ status.textContent = '返信文を入力してください'; return; }}
        if (!confirm('仏像リンクのアカウントから返信します。よろしいですか？\\n\\n' + text)) return;
      }}
      status.textContent = '送信予約中...';
      ghGetFile(REPLY_QUEUE_PATH).then(function (f) {{
        const q = Array.isArray(f.content) ? f.content : [];
        q.push({{ id: id, action: action, text: action === 'reply' ? text : '', queued_at: new Date().toISOString() }});
        return ghPutFile(REPLY_QUEUE_PATH, q, f.sha, 'chore(pwa): ' + action + ' reply ' + id);
      }}).then(function () {{
        try {{ localStorage.setItem('reply_queued_' + id, '1'); }} catch (e) {{}}
        card.innerHTML = '<p class="text-sm text-gray-500 text-center py-2">' +
          (action === 'reply' ? '✓ 返信を送信予約しました（1〜2分で仏像リンクから送信されます）' : '✓ 返信不要にしました') + '</p>';
        renderReplies();
      }}).catch(function (e) {{
        status.textContent = '❌ 失敗: ' + (e.message || e);
      }});
    }}

    function escapeHtml(s) {{
      return (s == null ? '' : String(s))
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }}

    // 週次ピックの表示件数（2件/日 × 7日）
    const VISIT_PICK_COUNT = 14;

    function pickRandom(arr, count) {{
      const a = arr.slice();
      for (let i = a.length - 1; i > 0; i--) {{
        const j = Math.floor(Math.random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
      }}
      return a.slice(0, Math.min(count, a.length));
    }}

    // テキストをクリップボードにコピーする（x.com の投稿画面に貼り付け→予約する用）。
    // data-text は encodeURIComponent 済み。特別公開タブの copyText（data-copy-text）
    // とは別関数にする（同名だと後の定義に上書きされてこちらが動かなくなる）。
    function copyEncodedText(btn) {{
      const t = (btn && btn.dataset) ? decodeURIComponent(btn.dataset.text || '') : '';
      if (!t) return;
      const original = btn.textContent;
      const flash = function (m) {{
        btn.textContent = m;
        setTimeout(function () {{ btn.textContent = original; }}, 1500);
      }};
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(t)
          .then(function () {{ flash('✓ 本文をコピーしました'); }})
          .catch(function () {{ _copyTextFallback(t, flash); }});
      }} else {{
        _copyTextFallback(t, flash);
      }}
    }}
    function _copyTextFallback(t, flash) {{
      try {{
        const ta = document.createElement('textarea');
        ta.value = t;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.focus(); ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        flash('✓ 本文をコピーしました');
      }} catch (e) {{ flash('コピー失敗'); }}
    }}

    function buildArchiveCardHtml(item) {{
      const text = item.text || '';
      const aid = item.id || '';
      const imgUrl = item.image_url || '';
      const safeText = escapeHtml(text).replace(/\\n/g, '<br>');
      // image_url が http(s) で始まる時のみ <img> を表示
      let imgHtml = '';
      if (imgUrl && /^https?:\/\//i.test(imgUrl)) {{
        imgHtml = '<img src="' + escapeHtml(imgUrl) + '" alt="" loading="lazy" ' +
                  'class="w-full h-40 object-cover rounded-xl mb-3" ' +
                  'onerror="this.style.display=\\'none\\'">';
      }}
      return (
        '<div class="archive-card bg-white rounded-2xl shadow-sm p-4 border border-brand-100" data-archive-id="' + escapeHtml(aid) + '">' +
          imgHtml +
          '<p class="text-sm text-gray-800 leading-relaxed mb-3 whitespace-pre-wrap break-words">' + safeText + '</p>' +
          '<button type="button" data-text="' + encodeURIComponent(text) + '" onclick="copyEncodedText(this)" ' +
              'class="w-full flex items-center justify-center gap-1.5 bg-brand-600 active:bg-brand-700 text-white text-sm font-bold py-2.5 px-4 rounded-full transition-colors">' +
            '📋 本文をコピー' +
          '</button>' +
        '</div>'
      );
    }}

    function renderArchive() {{
      const container = document.getElementById('archive-cards');
      if (!container) return;
      if (!Array.isArray(archiveData) || archiveData.length === 0) {{
        container.innerHTML = '<p class="text-center text-sm text-gray-400 py-6">訪問記はまだ登録されていません。<br>Streamlit 管理画面から追加してください。</p>';
        applyFilter();
        return;
      }}
      // 今週分としてランダム 14 件（2件/日×7日）を選ぶ。重複回避なし・件数が少なければ全件。
      const k = Math.min(archiveData.length, VISIT_PICK_COUNT);
      const picked = pickRandom(archiveData, k);
      container.innerHTML = picked.map(buildArchiveCardHtml).join('');
      applyFilter();
    }}

    // GitHub raw を優先的に参照する（Streamlit 保存後 ~5 分で即時反映される）。
    // 失敗時のみ同梱の docs/data/archives.json にフォールバック。
    const _RAW_BASE = 'https://raw.githubusercontent.com/YI1730/butsuzo-news-bot/main';

    function _fetchJson(remoteUrl, localUrl) {{
      // raw.githubusercontent.com の cache を回避するためタイムスタンプを付ける
      const ts = Date.now();
      return fetch(remoteUrl + '?t=' + ts, {{ cache: 'no-store' }})
        .then(r => r.ok ? r.json() : Promise.reject(new Error('remote ' + r.status)))
        .catch(() => fetch(localUrl, {{ cache: 'no-cache' }})
          .then(r => r.ok ? r.json() : []));
    }}

    function loadArchive() {{
      const container = document.getElementById('archive-cards');
      if (!container) return;
      _fetchJson(_RAW_BASE + '/data/archives.json', './data/archives.json')
        .then(data => {{
          archiveData = Array.isArray(data) ? data.filter(x => x && x.text) : [];
          renderArchive();
        }})
        .catch(() => {{
          archiveData = [];
          container.innerHTML = '<p class="text-center text-sm text-red-400 py-6">訪問記の読み込みに失敗しました。</p>';
        }});
    }}

    function shuffleArchive() {{
      if (archiveData) {{
        renderArchive();
      }} else {{
        loadArchive();
      }}
    }}

    // ────────────────────────────────────────────────────────
    // 予約投稿 (scheduled_posts.json) の表示
    // ────────────────────────────────────────────────────────
    const WEEKDAY_LABEL_JA = {{ mon:'月', tue:'火', wed:'水', thu:'木', fri:'金', sat:'土', sun:'日' }};

    // 告知画像をクリップボードにコピーする（X投稿画面に貼り付け→画像添付できる）。
    // 失敗時（CORS非対応・非対応ブラウザ）は画像を別タブで開いて長押しコピーに誘導。
    function _pngFromBlob(blob) {{
      return new Promise(function (resolve, reject) {{
        const img = new Image();
        img.onload = function () {{
          try {{
            const c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img, 0, 0);
            c.toBlob(function (b) {{ b ? resolve(b) : reject(new Error('toBlob')); }}, 'image/png');
          }} catch (e) {{ reject(e); }}
        }};
        img.onerror = function () {{ reject(new Error('img load')); }};
        img.src = URL.createObjectURL(blob);
      }});
    }}

    function copyPromoImage(btn) {{
      const url = (btn && btn.dataset) ? btn.dataset.img : '';
      if (!url) return;
      const original = btn.textContent;
      const flash = function (msg) {{
        btn.textContent = msg;
        setTimeout(function () {{ btn.textContent = original; }}, 1600);
      }};
      const fallback = function () {{
        window.open(url, '_blank', 'noopener');
        flash('↗ 画像を開きました（長押しでコピー）');
      }};
      if (navigator.clipboard && window.ClipboardItem) {{
        try {{
          const blobPromise = fetch(url, {{ mode: 'cors' }})
            .then(function (r) {{ if (!r.ok) throw new Error('http ' + r.status); return r.blob(); }})
            .then(function (b) {{ return (b.type === 'image/png') ? b : _pngFromBlob(b); }});
          const item = new ClipboardItem({{ 'image/png': blobPromise }});
          navigator.clipboard.write([item])
            .then(function () {{ flash('✓ 画像をコピーしました'); }})
            .catch(function () {{ fallback(); }});
          return;
        }} catch (e) {{ /* fallthrough */ }}
      }}
      fallback();
    }}

    function buildScheduledCardHtml(item) {{
      const text = item.text || '';
      const sid = item.id || '';
      const imgUrl = (item.image_url || '').trim();
      const safeText = escapeHtml(text).replace(/\\n/g, '<br>');
      // 告知用の画像：本文の下に表示し、ワンタップでコピー→X投稿画面に貼付できる
      let imgHtml = '';
      if (imgUrl && /^https?:\/\//i.test(imgUrl)) {{
        imgHtml =
          '<div class="mb-3 p-2 bg-brand-50 rounded-xl">' +
            '<p class="text-xs font-bold text-brand-800 mb-1">🖼 告知用の画像</p>' +
            '<img src="' + escapeHtml(imgUrl) + '" alt="" loading="lazy" ' +
              'class="w-full h-40 object-cover rounded-lg mb-2" onerror="this.style.display=\\'none\\'">' +
            '<button type="button" data-img="' + escapeHtml(imgUrl) + '" onclick="copyPromoImage(this)" ' +
              'class="w-full bg-brand-500 active:bg-brand-700 text-white text-xs font-bold py-2 rounded-full transition-colors">' +
              '📋 画像をコピー（つぶやきに貼付）' +
            '</button>' +
          '</div>';
      }}
      return (
        '<div class="scheduled-card bg-white rounded-2xl shadow-sm p-4 border border-brand-100" data-scheduled-id="' + escapeHtml(sid) + '">' +
          '<p class="text-sm text-gray-800 leading-relaxed mb-3 whitespace-pre-wrap break-words">' + safeText + '</p>' +
          imgHtml +
          '<button type="button" data-text="' + encodeURIComponent(text) + '" onclick="copyEncodedText(this)" ' +
              'class="w-full flex items-center justify-center gap-1.5 bg-brand-600 active:bg-brand-700 text-white text-sm font-bold py-2.5 px-4 rounded-full transition-colors">' +
            '📋 本文をコピー' +
          '</button>' +
        '</div>'
      );
    }}

    function renderScheduled() {{
      const container = document.getElementById('scheduled-cards');
      if (!container) return;
      if (!Array.isArray(scheduledData) || scheduledData.length === 0) {{
        container.innerHTML = '<p class="text-center text-sm text-gray-400 py-6">予約投稿はまだ登録されていません。<br>Streamlit 管理画面の「③ 予約投稿」から追加してください。</p>';
        return;
      }}
      // 曜日 → 時刻 でソート（毎日「*」は先頭）
      const wdOrder = {{ '*': -1, mon:0, tue:1, wed:2, thu:3, fri:4, sat:5, sun:6 }};
      const sorted = scheduledData.slice().sort((a, b) => {{
        const wa = wdOrder[a.weekday] != null ? wdOrder[a.weekday] : 99;
        const wb = wdOrder[b.weekday] != null ? wdOrder[b.weekday] : 99;
        if (wa !== wb) return wa - wb;
        return (a.time || '').localeCompare(b.time || '');
      }});
      container.innerHTML = sorted.map(buildScheduledCardHtml).join('');
    }}

    function loadScheduled() {{
      const container = document.getElementById('scheduled-cards');
      if (!container) return;
      _fetchJson(_RAW_BASE + '/data/scheduled_posts.json', './data/scheduled_posts.json')
        .then(data => {{
          scheduledData = Array.isArray(data) ? data.filter(x => x && x.text) : [];
          renderScheduled();
        }})
        .catch(() => {{
          scheduledData = [];
          container.innerHTML = '<p class="text-center text-sm text-red-400 py-6">予約投稿の読み込みに失敗しました。</p>';
        }});
    }}

    // ────────────────────────────────────────────────────────
    // ⚙️ 自動投稿の設定（GitHub Contents API 経由で直接コミット）
    // 訪問記の投稿時刻・回数と、告知の曜日・時刻・本文・画像URLをこの端末から編集する。
    // トークンは localStorage にのみ保存し、外部には送信しない。
    // ────────────────────────────────────────────────────────
    const GH_OWNER = 'YI1730';
    const GH_REPO = 'butsuzo-news-bot';
    const GH_API = 'https://api.github.com';
    const GH_TOKEN_KEY = 'butsuzo_gh_pat';
    const CONFIG_PATH = 'config/auto_post_config.json';
    const SCHED_PATH = 'data/scheduled_posts.json';

    function getGithubToken() {{
      try {{ return localStorage.getItem(GH_TOKEN_KEY) || ''; }} catch (e) {{ return ''; }}
    }}

    function saveGithubToken() {{
      const input = document.getElementById('settings-token-input');
      const status = document.getElementById('settings-token-status');
      const t = (input && input.value || '').trim();
      if (!t) {{ if (status) status.textContent = 'トークンを入力してください'; return; }}
      try {{
        localStorage.setItem(GH_TOKEN_KEY, t);
        if (input) input.value = '';
        if (status) status.textContent = '✓ 保存しました';
      }} catch (e) {{
        if (status) status.textContent = '保存に失敗しました（ブラウザの設定をご確認ください）';
        return;
      }}
      loadSettingsPanel();
    }}

    // UTF-8 文字列 <-> Base64（GitHub Contents API は Base64 でやり取りする）
    function utf8ToBase64(str) {{
      return btoa(unescape(encodeURIComponent(str)));
    }}
    function base64ToUtf8(b64) {{
      return decodeURIComponent(escape(atob(b64.replace(/\\n/g, ''))));
    }}

    function ghGetFile(path) {{
      const token = getGithubToken();
      return fetch(GH_API + '/repos/' + GH_OWNER + '/' + GH_REPO + '/contents/' + path + '?t=' + Date.now(), {{
        headers: {{
          'Authorization': 'Bearer ' + token,
          'Accept': 'application/vnd.github+json',
        }},
        cache: 'no-store',
      }}).then(function (r) {{
        if (!r.ok) throw new Error('GitHub API GET 失敗 HTTP ' + r.status);
        return r.json();
      }}).then(function (body) {{
        const content = JSON.parse(base64ToUtf8(body.content || ''));
        return {{ content: content, sha: body.sha }};
      }});
    }}

    function ghPutFile(path, contentObj, sha, message) {{
      const token = getGithubToken();
      const bodyStr = JSON.stringify(contentObj, null, 2) + '\\n';
      return fetch(GH_API + '/repos/' + GH_OWNER + '/' + GH_REPO + '/contents/' + path, {{
        method: 'PUT',
        headers: {{
          'Authorization': 'Bearer ' + token,
          'Accept': 'application/vnd.github+json',
          'Content-Type': 'application/json',
        }},
        body: JSON.stringify({{
          message: message,
          content: utf8ToBase64(bodyStr),
          sha: sha,
          branch: 'main',
        }}),
      }}).then(function (r) {{
        if (!r.ok) {{
          return r.json().catch(function () {{ return {{}}; }}).then(function (e) {{
            throw new Error('GitHub API PUT 失敗 HTTP ' + r.status + ': ' + (e.message || ''));
          }});
        }}
        return r.json();
      }}).then(function (body) {{
        return body.content && body.content.sha;
      }});
    }}

    let _settingsConfig = null;
    let _settingsConfigSha = null;
    let _settingsSched = null;
    let _settingsSchedSha = null;

    function loadSettingsPanel() {{
      const body = document.getElementById('settings-body');
      const tokenStatus = document.getElementById('settings-token-status');
      if (!body) return;
      const token = getGithubToken();
      if (!token) {{
        body.innerHTML = '<p class="text-xs text-gray-400 text-center py-2">トークンを保存すると設定を編集できます</p>';
        return;
      }}
      if (tokenStatus && !tokenStatus.textContent) {{
        tokenStatus.textContent = '✓ トークン設定済み';
      }}
      body.innerHTML = '<p class="text-xs text-gray-400 text-center py-4">読み込み中...</p>';

      Promise.all([
        ghGetFile(CONFIG_PATH).catch(function (e) {{ return {{ content: {{ visit_enabled: true, visit_hours: [8, 20] }}, sha: null, _err: e }}; }}),
        ghGetFile(SCHED_PATH).catch(function (e) {{ return {{ content: [], sha: null, _err: e }}; }}),
      ]).then(function (results) {{
        _settingsConfig = results[0].content || {{}};
        _settingsConfigSha = results[0].sha;
        _settingsSched = Array.isArray(results[1].content) ? results[1].content : [];
        _settingsSchedSha = results[1].sha;
        renderSettingsBody();
      }}).catch(function (e) {{
        body.innerHTML = '<p class="text-xs text-red-500 text-center py-4">読み込み失敗: ' + escapeHtml(String(e.message || e)) + '</p>';
      }});
    }}

    function visitSectionHtml() {{
      const cfg = _settingsConfig || {{}};
      const hours = Array.isArray(cfg.visit_hours) ? cfg.visit_hours.map(Number) : [];
      const enabled = cfg.visit_enabled !== false;

      // 訪問記の時刻設定（0-23 のトグルチップ）
      let hourChips = '';
      for (let h = 0; h < 24; h++) {{
        const on = hours.indexOf(h) !== -1;
        hourChips +=
          '<button type="button" data-hour="' + h + '" onclick="toggleVisitHour(' + h + ')" ' +
            'class="visit-hour-chip w-9 h-9 rounded-lg text-xs font-bold transition-colors ' +
            (on ? 'bg-brand-600 text-white' : 'bg-gray-100 text-gray-500') + '">' +
            h +
          '</button>';
      }}

      let html = '';
      html += '<p class="text-xs font-bold text-brand-800 mb-1">📜 訪問記の自動投稿</p>';
      html += '<label class="flex items-center gap-2 text-xs text-gray-600 mb-2">';
      html += '  <input type="checkbox" id="visit-enabled-checkbox" ' + (enabled ? 'checked' : '') + ' onchange="onVisitEnabledChange()">';
      html += '  自動投稿を有効にする';
      html += '</label>';
      html += '<p class="text-[11px] text-gray-500 mb-2">投稿する時刻（JST）をタップして選択。選んだ時刻の数 = 1日の投稿回数です。</p>';
      html += '<div class="grid grid-cols-6 gap-1.5 mb-2">' + hourChips + '</div>';
      html += '<button onclick="saveVisitConfig()" class="w-full bg-brand-600 active:bg-brand-700 text-white text-xs font-bold py-2 rounded-full transition-colors">💾 訪問記の設定を保存</button>';
      html += '<p id="visit-config-status" class="text-[11px] text-gray-500 mt-1"></p>';
      return html;
    }}

    function renderSettingsBody() {{
      const body = document.getElementById('settings-body');
      if (!body) return;
      let html = '';
      html += '<div id="visit-config-section" class="border-t border-gray-100 pt-3">' + visitSectionHtml() + '</div>';
      // 告知の一覧
      html += '<div class="border-t border-gray-100 pt-3">';
      html += '  <p class="text-xs font-bold text-brand-800 mb-2">📣 告知の登録・編集</p>';
      html += '  <div id="promo-edit-list" class="space-y-2">' + renderPromoEditList() + '</div>';
      html += '  <button onclick="addPromoDraft()" class="w-full mt-2 bg-gray-100 active:bg-gray-200 text-gray-700 text-xs font-bold py-2 rounded-full transition-colors">＋ 告知を追加</button>';
      html += '</div>';

      body.innerHTML = html;
    }}

    function onVisitEnabledChange() {{
      // チェックボックス変更は保存ボタンで確定するので、ここでは何もしない（見た目のみ）
    }}

    function toggleVisitHour(h) {{
      const cfg = _settingsConfig || {{ visit_hours: [] }};
      const hours = Array.isArray(cfg.visit_hours) ? cfg.visit_hours.map(Number) : [];
      const idx = hours.indexOf(h);
      if (idx === -1) {{ hours.push(h); }} else {{ hours.splice(idx, 1); }}
      hours.sort(function (a, b) {{ return a - b; }});
      cfg.visit_hours = hours;
      _settingsConfig = cfg;
      // 訪問設定部分のみ再描画（告知の未保存編集を巻き込まない）
      const section = document.getElementById('visit-config-section');
      if (section) section.innerHTML = visitSectionHtml();
    }}

    function saveVisitConfig() {{
      const status = document.getElementById('visit-config-status');
      const checkbox = document.getElementById('visit-enabled-checkbox');
      const cfg = _settingsConfig || {{}};
      cfg.visit_enabled = checkbox ? !!checkbox.checked : true;
      cfg.visit_hours = Array.isArray(cfg.visit_hours) ? cfg.visit_hours.map(Number).sort(function (a, b) {{ return a - b; }}) : [];
      if (status) status.textContent = '保存中...';
      ghPutFile(CONFIG_PATH, cfg, _settingsConfigSha, 'chore(pwa): update visit auto-post config')
        .then(function (sha) {{
          _settingsConfigSha = sha;
          if (status) status.textContent = '✓ 保存しました（次回の毎時実行から反映）';
        }})
        .catch(function (e) {{
          if (status) status.textContent = '❌ 保存失敗: ' + (e.message || e);
        }});
    }}

    // 曜日は複数選択可（毎日 or 個別曜日の組み合わせ）。
    // 「毎日」を選ぶと個別曜日は解除、個別曜日を選ぶと「毎日」は解除される。
    const WD_LABEL_OPTIONS = [
      {{ code: '*', label: '毎日' }}, {{ code: 'mon', label: '月' }}, {{ code: 'tue', label: '火' }},
      {{ code: 'wed', label: '水' }}, {{ code: 'thu', label: '木' }}, {{ code: 'fri', label: '金' }},
      {{ code: 'sat', label: '土' }}, {{ code: 'sun', label: '日' }},
    ];
    const WD_CHIP_ON = 'bg-brand-600 text-white border-brand-600';
    const WD_CHIP_OFF = 'bg-white text-gray-500 border-gray-300';

    // 旧形式（weekday: 単一文字列）にも対応した曜日リストを返す
    function itemWeekdaysForDisplay(item) {{
      if (Array.isArray(item.weekdays) && item.weekdays.length) return item.weekdays;
      const legacy = item.weekday;
      if (!legacy || legacy === '*') return ['*'];
      return [legacy];
    }}

    function _promoWeekdayChipsHtml(item) {{
      const wds = itemWeekdaysForDisplay(item);
      const isDaily = wds.length === 1 && wds[0] === '*';
      return '<div class="promo-weekday-row flex flex-wrap gap-1 mb-1.5">' +
        WD_LABEL_OPTIONS.map(function (o) {{
          const isActive = isDaily ? (o.code === '*') : (wds.indexOf(o.code) !== -1);
          return '<button type="button" data-wd="' + o.code + '" onclick="toggleWeekdayChip(this)" ' +
            'class="px-2 py-1 rounded-full text-[10px] font-bold border transition-colors ' +
            (isActive ? WD_CHIP_ON : WD_CHIP_OFF) + '">' + o.label + '</button>';
        }}).join('') +
        '</div>';
    }}

    function toggleWeekdayChip(btn) {{
      const row = btn.closest('.promo-weekday-row');
      if (!row) return;
      const code = btn.getAttribute('data-wd');
      const setActive = function (el, active) {{
        el.className = 'px-2 py-1 rounded-full text-[10px] font-bold border transition-colors ' + (active ? WD_CHIP_ON : WD_CHIP_OFF);
      }};
      if (code === '*') {{
        row.querySelectorAll('[data-wd]').forEach(function (b) {{ setActive(b, b === btn); }});
        return;
      }}
      const isActive = btn.className.indexOf('bg-brand-600') !== -1;
      setActive(btn, !isActive);
      const allBtn = row.querySelector('[data-wd="*"]');
      if (allBtn) setActive(allBtn, false);
    }}

    function renderPromoEditList() {{
      const list = _settingsSched || [];
      if (list.length === 0) {{
        return '<p class="text-[11px] text-gray-400 py-2">告知はまだ登録されていません。</p>';
      }}
      return list.map(function (item, i) {{
        const sid = item.id || ('idx' + i);
        return (
          '<div class="border border-gray-200 rounded-xl p-2.5" data-promo-id="' + escapeHtml(sid) + '">' +
            '<textarea data-role="text" rows="3" placeholder="告知本文" ' +
              'class="w-full text-xs border border-gray-300 rounded-lg p-2 mb-1.5 focus:outline-none focus:ring-2 focus:ring-brand-500">' +
              escapeHtml(item.text || '') +
            '</textarea>' +
            '<p class="text-[10px] text-gray-500 mb-1">曜日（複数選択可）</p>' +
            _promoWeekdayChipsHtml(item) +
            '<input data-role="time" type="time" value="' + escapeHtml(item.time || '09:00') + '" ' +
              'class="w-full text-xs border border-gray-300 rounded-lg px-1.5 py-1.5 mb-1.5">' +
            '<input data-role="image_url" type="text" placeholder="添付画像URL（任意）" value="' + escapeHtml(item.image_url || '') + '" ' +
              'class="w-full text-xs border border-gray-300 rounded-lg px-2 py-1.5 mb-1.5">' +
            '<label class="flex items-start gap-1.5 text-[11px] text-gray-600 mb-1.5">' +
              '<input type="checkbox" data-role="use_media_upload" class="mt-0.5" ' + (item.use_media_upload ? 'checked' : '') + '>' +
              '<span>🖼 画像をアップロードして添付する（自動投稿時）。' +
                'OFFなら本文の pic.twitter.com リンクなど、テキストのみで投稿します。' +
                'アップロードは追加のトークンを消費するため、本当に必要な告知だけONにしてください。</span>' +
            '</label>' +
            '<div class="flex gap-1.5">' +
              '<button onclick="savePromoItem(\\'' + escapeHtml(sid).replace(/\\'/g, "\\\\'") + '\\')" ' +
                'class="flex-1 bg-brand-600 active:bg-brand-700 text-white text-[11px] font-bold py-1.5 rounded-full">💾 保存</button>' +
              '<button onclick="deletePromoItem(\\'' + escapeHtml(sid).replace(/\\'/g, "\\\\'") + '\\')" ' +
                'class="flex-1 bg-red-50 active:bg-red-100 text-red-600 text-[11px] font-bold py-1.5 rounded-full">🗑 削除</button>' +
            '</div>' +
            '<p class="promo-item-status text-[11px] text-gray-500 mt-1"></p>' +
          '</div>'
        );
      }}).join('');
    }}

    function _readPromoWeekdays(card) {{
      const row = card.querySelector('.promo-weekday-row');
      if (!row) return ['*'];
      const active = Array.prototype.slice.call(row.querySelectorAll('[data-wd]'))
        .filter(function (b) {{ return b.className.indexOf('bg-brand-600') !== -1; }})
        .map(function (b) {{ return b.getAttribute('data-wd'); }});
      if (active.length === 0) return ['*']; // 何も選ばれていなければ毎日として扱う
      if (active.indexOf('*') !== -1) return ['*'];
      return active;
    }}

    function _readPromoCardFields(sid) {{
      const card = document.querySelector('[data-promo-id="' + CSS.escape(sid) + '"]');
      if (!card) return null;
      const get = function (role) {{ const el = card.querySelector('[data-role="' + role + '"]'); return el ? el.value : ''; }};
      const uploadCheckbox = card.querySelector('[data-role="use_media_upload"]');
      return {{
        card: card,
        text: (get('text') || '').trim(),
        weekdays: _readPromoWeekdays(card),
        time: get('time') || '09:00',
        image_url: (get('image_url') || '').trim(),
        use_media_upload: !!(uploadCheckbox && uploadCheckbox.checked),
      }};
    }}

    function addPromoDraft() {{
      const list = _settingsSched || (_settingsSched = []);
      list.unshift({{ id: '_new_' + Date.now(), text: '', weekdays: ['*'], time: '09:00', image_url: '', use_media_upload: false }});
      const container = document.getElementById('promo-edit-list');
      if (container) container.innerHTML = renderPromoEditList();
    }}

    function savePromoItem(sid) {{
      const fields = _readPromoCardFields(sid);
      if (!fields) return;
      const statusEl = fields.card.querySelector('.promo-item-status');
      if (!fields.text) {{
        if (statusEl) statusEl.textContent = '本文を入力してください';
        return;
      }}
      if (fields.use_media_upload && !fields.image_url) {{
        if (statusEl) statusEl.textContent = '画像アップロードをONにする場合は画像URLを入力してください';
        return;
      }}
      const list = _settingsSched || [];
      const isNew = sid.indexOf('_new_') === 0;
      const finalId = isNew ? Math.random().toString(16).slice(2, 14) : sid;
      const updated = {{
        id: finalId, text: fields.text, weekdays: fields.weekdays, time: fields.time,
        image_url: fields.image_url, use_media_upload: fields.use_media_upload,
      }};
      const idx = list.findIndex(function (x) {{ return (x.id || '') === sid; }});
      if (idx === -1) {{ list.push(updated); }} else {{ list[idx] = updated; }}
      _settingsSched = list;

      if (statusEl) statusEl.textContent = '保存中...';
      ghPutFile(SCHED_PATH, list, _settingsSchedSha, 'chore(pwa): update scheduled promo ' + finalId)
        .then(function (sha) {{
          _settingsSchedSha = sha;
          const container = document.getElementById('promo-edit-list');
          if (container) container.innerHTML = renderPromoEditList();
          scheduledData = null; // PWA上部のカード表示も次回反映されるようキャッシュを捨てる
        }})
        .catch(function (e) {{
          if (statusEl) statusEl.textContent = '❌ 保存失敗: ' + (e.message || e);
        }});
    }}

    function deletePromoItem(sid) {{
      const list = _settingsSched || [];
      const isNew = sid.indexOf('_new_') === 0;
      if (isNew) {{
        _settingsSched = list.filter(function (x) {{ return (x.id || '') !== sid; }});
        const container = document.getElementById('promo-edit-list');
        if (container) container.innerHTML = renderPromoEditList();
        return;
      }}
      const fields = _readPromoCardFields(sid);
      const statusEl = fields ? fields.card.querySelector('.promo-item-status') : null;
      const newList = list.filter(function (x) {{ return (x.id || '') !== sid; }});
      if (statusEl) statusEl.textContent = '削除中...';
      ghPutFile(SCHED_PATH, newList, _settingsSchedSha, 'chore(pwa): delete scheduled promo ' + sid)
        .then(function (sha) {{
          _settingsSched = newList;
          _settingsSchedSha = sha;
          const container = document.getElementById('promo-edit-list');
          if (container) container.innerHTML = renderPromoEditList();
          scheduledData = null;
        }})
        .catch(function (e) {{
          if (statusEl) statusEl.textContent = '❌ 削除失敗: ' + (e.message || e);
        }});
    }}

    function handlePostClick(event, itemId) {{
      const card = document.querySelector('[data-item-id="' + itemId + '"]');
      const btn = card ? card.querySelector('.post-btn') : null;
      const desc = btn ? (btn.getAttribute('data-description') || '') : '';

      // description がある場合はリンク遷移を止めて、紹介文入りの URL で開く
      if (desc && btn) {{
        event.preventDefault();
        try {{
          const u = new URL(btn.getAttribute('href') || '');
          const text = u.searchParams.get('text') || '';
          const url  = u.searchParams.get('url')  || '';
          // text を改行で分割し、description を hashtags の直前に挿入する
          const parts = text.split('\\n');
          parts.splice(parts.length - 1, 0, desc);
          const newText = parts.join('\\n');
          const newParams = new URLSearchParams({{ text: newText, url: url }});
          window.open('https://x.com/intent/post?' + newParams.toString(), '_blank', 'noopener');
        }} catch(e) {{
          window.open(btn.getAttribute('href') || '', '_blank', 'noopener');
        }}
      }}

      localStorage.setItem('posted_' + itemId, '1');
      if (card) {{
        card.classList.add('is-posted');
        if (btn) btn.innerHTML = '✓ 再投稿';
      }}
      if (currentFilter === 'unposted') {{
        setTimeout(() => {{
          if (card) card.style.display = 'none';
          applyFilter();
        }}, 800);
      }}
    }}

    // 特別公開タブのタイトル横コピーボタン用
    function copyText(btn) {{
      if (!btn) return;
      const text = btn.getAttribute('data-copy-text') || '';
      if (!text) return;
      const showDone = () => {{
        const orig = btn.innerHTML;
        btn.innerHTML = '<span class="text-[11px] font-bold text-brand-700">✓ コピー</span>';
        setTimeout(() => {{ btn.innerHTML = orig; }}, 1200);
      }};
      if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(text).then(showDone).catch(() => {{
          // 失敗時は textarea フォールバック
          fallbackCopy(text, showDone);
        }});
      }} else {{
        fallbackCopy(text, showDone);
      }}
    }}

    function fallbackCopy(text, onDone) {{
      try {{
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        if (onDone) onDone();
      }} catch(e) {{
        // 何もしない（ユーザーへの過剰な通知は避ける）
      }}
    }}

    function resetAll() {{
      if (!confirm('投稿済みの記録をすべてリセットしますか？')) return;
      document.querySelectorAll('[data-item-id]').forEach(card => {{
        localStorage.removeItem('posted_' + card.dataset.itemId);
      }});
      location.reload();
    }}

    function applyKeywords() {{
      const inc = (document.getElementById('kw-include').value || '').trim();
      const exc = (document.getElementById('kw-exclude').value || '').trim();
      localStorage.setItem('kw_include', inc);
      localStorage.setItem('kw_exclude', exc);
      updateKeywordStatus();
      if (currentTab === 'news') applyFilter();
    }}

    function clearKeywords() {{
      const incEl = document.getElementById('kw-include');
      const excEl = document.getElementById('kw-exclude');
      if (incEl) incEl.value = '';
      if (excEl) excEl.value = '';
      localStorage.removeItem('kw_include');
      localStorage.removeItem('kw_exclude');
      updateKeywordStatus();
      if (currentTab === 'news') applyFilter();
    }}

    function loadKeywords() {{
      const incEl = document.getElementById('kw-include');
      const excEl = document.getElementById('kw-exclude');
      if (incEl) incEl.value = localStorage.getItem('kw_include') || '';
      if (excEl) excEl.value = localStorage.getItem('kw_exclude') || '';
      updateKeywordStatus();
    }}

    // ── 横スワイプでタブ切替（モバイル用 / 両端ループ） ─────────
    const TAB_ORDER = ['news', 'replies', 'other', 'goods', 'archive'];
    function swipeToNextTab(dir) {{
      // dir: +1 で次のタブ（右→左スワイプ）、-1 で前のタブ（左→右スワイプ）
      const i = TAB_ORDER.indexOf(currentTab);
      if (i < 0) return;
      const n = TAB_ORDER.length;
      const next = TAB_ORDER[((i + dir) % n + n) % n];
      setTab(next);
    }}
    function attachSwipeHandler() {{
      // <main> 配下でのみ反応（ヘッダーのタブボタン/フィルターは除外）
      const surface = document.querySelector('main') || document.body;
      let sx = 0, sy = 0, st = 0, tracking = false;
      const THRESHOLD_X = 60;   // 最小水平移動量(px)
      const MAX_OFF_AXIS = 50;  // 縦方向のブレ許容量(px)
      const MAX_DURATION = 700; // スワイプ完了までの上限(ms)

      surface.addEventListener('touchstart', (e) => {{
        if (e.touches.length !== 1) {{ tracking = false; return; }}
        // 入力欄/テキストエリアではスワイプを無効化
        const t = e.target;
        if (t && t.closest && t.closest('input, textarea, select, [contenteditable="true"]')) {{
          tracking = false; return;
        }}
        const p = e.touches[0];
        sx = p.clientX; sy = p.clientY; st = Date.now();
        tracking = true;
      }}, {{ passive: true }});

      surface.addEventListener('touchend', (e) => {{
        if (!tracking) return;
        tracking = false;
        const p = (e.changedTouches && e.changedTouches[0]) || null;
        if (!p) return;
        const dx = p.clientX - sx;
        const dy = p.clientY - sy;
        const dt = Date.now() - st;
        if (dt > MAX_DURATION) return;
        if (Math.abs(dx) < THRESHOLD_X) return;     // 移動量不足
        if (Math.abs(dy) > MAX_OFF_AXIS) return;     // 縦スクロール扱い
        if (Math.abs(dx) < Math.abs(dy) * 1.5) return; // 角度が立ちすぎ
        // dx < 0 → 指が左へ移動 → 次のタブへ
        swipeToNextTab(dx < 0 ? +1 : -1);
      }}, {{ passive: true }});
    }}

    function init() {{
      // 外部リンクを Android PWA 環境では intent:// URI に書き換える
      rewriteExternalLinksForAndroidPWA();

      document.querySelectorAll('[data-item-id]').forEach(card => {{
        if (localStorage.getItem('posted_' + card.dataset.itemId) === '1') {{
          card.classList.add('is-posted');
          const btn = card.querySelector('.post-btn');
          if (btn) btn.innerHTML = '✓ 再投稿';
        }}
      }});
      loadKeywords();
      setTab(location.hash === '#replies' ? 'replies' : 'news');
      loadReplies();  // タブのバッジ（要返信の件数）表示のため
      setFilter('all');
      attachSwipeHandler();

      // Enter キーで適用
      ['kw-include', 'kw-exclude'].forEach(id => {{
        const el = document.getElementById(id);
        if (el) el.addEventListener('keydown', e => {{
          if (e.key === 'Enter') {{ e.preventDefault(); applyKeywords(); }}
        }});
      }});

      // ⚙️ 自動投稿の設定パネル：開いたときに読み込む
      const settingsPanel = document.getElementById('settings-panel');
      if (settingsPanel) {{
        const tokenStatus = document.getElementById('settings-token-status');
        if (tokenStatus && getGithubToken()) {{ tokenStatus.textContent = '✓ トークン設定済み'; }}
        settingsPanel.addEventListener('toggle', function () {{
          if (settingsPanel.open) loadSettingsPanel();
        }});
      }}
    }}

    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('./sw.js').catch(() => {{}});
    }}

    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# PWA アセット生成
# ---------------------------------------------------------------------------


MANIFEST = {
    "name": "仏像ニュース ダッシュボード",
    "short_name": "仏像ニュース",
    "description": "仏像関連ニュース・特別公開情報の投稿管理ダッシュボード",
    "start_url": "./",
    # standalone: ホーム画面アイコンから「アプリのように」全画面で起動。
    # iOS 17+ では PWA でも target="_blank" の外部リンクは自動で Safari の新規タブで開かれる。
    "display": "standalone",
    "background_color": "#00AE95",  # スプラッシュ背景もブランドカラーで埋める
    "theme_color": "#00AE95",
    "lang": "ja",
    "icons": [
        {"src": "./icons/favicon-16x16.png", "sizes": "16x16",   "type": "image/png"},
        {"src": "./icons/favicon-32x32.png", "sizes": "32x32",   "type": "image/png"},
        {"src": "./icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        {"src": "./icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "./icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "./icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SERVICE_WORKER = r"""const CACHE = 'butsuzo-v31';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // index.html / news.json / archives.json / scheduled_posts.json は常に network-first
  if (url.endsWith('/') || url.includes('/index.html')
      || url.includes('/data/news.json')
      || url.includes('/data/archives.json')
      || url.includes('/data/scheduled_posts.json')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // その他（アイコン・manifest 等）はキャッシュ優先
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    if NEWS_JSON_FILE.exists():
        with NEWS_JSON_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_updated": "", "items": []}

    items = data.get("items", [])
    last_updated = data.get("last_updated", "")

    print(f"ダッシュボード生成: {len(items)}件")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    icons_dir = DOCS_DIR / "icons"
    icons_dir.mkdir(exist_ok=True)

    (DOCS_DIR / "index.html").write_text(build_html(items, last_updated), encoding="utf-8")
    print("生成: docs/index.html")

    (DOCS_DIR / "manifest.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("生成: docs/manifest.json")

    (DOCS_DIR / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    print("生成: docs/sw.js")

    # data/archives.json / data/scheduled_posts.json を docs/data/ にコピー（PWA から fetch させるため）
    ARCHIVES_DST.parent.mkdir(parents=True, exist_ok=True)
    if ARCHIVES_SRC.exists():
        try:
            archives_data = json.loads(ARCHIVES_SRC.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ archives.json 読み込み失敗: {e} → 空配列を出力")
            archives_data = []
    else:
        archives_data = []
    ARCHIVES_DST.write_text(
        json.dumps(archives_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"生成: docs/data/archives.json ({len(archives_data) if isinstance(archives_data, list) else 0}件)")

    if SCHEDULED_SRC.exists():
        try:
            scheduled_data = json.loads(SCHEDULED_SRC.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ scheduled_posts.json 読み込み失敗: {e} → 空配列を出力")
            scheduled_data = []
    else:
        scheduled_data = []
    SCHEDULED_DST.write_text(
        json.dumps(scheduled_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"生成: docs/data/scheduled_posts.json ({len(scheduled_data) if isinstance(scheduled_data, list) else 0}件)")

    # 返信の仕分け結果（triage_replies.py）。PWA は GitHub raw を優先して読み、失敗時にこちらを使う
    replies_src = Path(__file__).parent / "data" / "replies.json"
    if replies_src.exists():
        (DOCS_DIR / "data" / "replies.json").write_text(
            replies_src.read_text(encoding="utf-8"), encoding="utf-8")

    # アイコンはロゴ入り PNG をリポジトリに同梱しているため、上書き生成しない。
    # （tools/generate_logo.py で再生成可能。GitHub Actions では更新しない）
    # 既存ファイルが無い場合のみフォールバックで単色 PNG を生成する
    for size in (192, 512):
        path = icons_dir / f"icon-{size}.png"
        if not path.exists():
            path.write_bytes(create_solid_png(size, ICON_COLOR))
            print(f"生成(フォールバック): docs/icons/icon-{size}.png")
        else:
            print(f"スキップ(既存): docs/icons/icon-{size}.png")

    print("ダッシュボード生成完了")


if __name__ == "__main__":
    main()
