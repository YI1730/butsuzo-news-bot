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
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
NEWS_TAB_SOURCES = {"google_news", "bangumi_tv"}
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


def build_card_html(item: dict) -> str:
    uid = item.get("id", "")
    title = item.get("title", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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

    return f"""    <div class="card bg-white rounded-2xl shadow-sm p-4 border border-brand-100 transition-opacity duration-300" data-item-id="{uid}">
{image_html}{meta_html}
      <p class="text-sm font-semibold text-gray-800 leading-relaxed mb-3">{title}</p>
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


def build_separator_html(dt: datetime) -> str:
    """取り込み時刻を表す区切り線（カード間に挿入）を生成。"""
    label = dt.strftime("%-m月%-d日 %H:%M")
    return f"""    <div class="separator flex items-center gap-3 my-2">
      <div class="flex-1 h-px bg-brand-300"></div>
      <span class="text-[11px] font-semibold text-brand-700 bg-brand-50 px-3 py-0.5 rounded-full whitespace-nowrap shadow-sm border border-brand-200">📥 {label} 取込</span>
      <div class="flex-1 h-px bg-brand-300"></div>
    </div>"""


def build_cards_with_separators(items: list[dict]) -> str:
    """fetched_at が一定以上離れた境界に区切り線を挟みつつカードを並べる。

    items は fetched_at の降順でソート済みである前提。
    各セッション（同じ取り込み実行で追加されたグループ）の頭に区切り線を挿入する。
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

        output.append(build_card_html(item))

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

    news_cards  = build_cards_with_separators(news_items)
    goods_cards = build_cards_with_separators(goods_items)
    other_cards = build_cards_with_separators(other_items)
    news_count  = len(news_items)
    goods_count = len(goods_items)
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

    <!-- タブ切替（セグメント形式・4タブ） -->
    <div class="max-w-xl mx-auto bg-brand-900 p-1 rounded-xl flex gap-1">
      <button onclick="setTab('news')" id="tab-btn-news"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        📰 ニュース<span class="opacity-60 ml-0.5">{news_count}</span>
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
{goods_cards}
    </div>
    <!-- 訪問記タブ：過去の訪問記ランダム＋予約投稿 -->
    <div id="tab-archive" class="tab-pane space-y-3 hidden">
      <!-- 過去の訪問記（ランダム） -->
      <div class="flex items-center justify-between bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm">
        <div>
          <p class="text-sm font-bold text-brand-800">📜 過去の訪問記</p>
          <p class="text-xs text-gray-500 mt-0.5">ランダムに数件ピックアップ・Streamlit 保存後 ~5分で反映</p>
        </div>
        <button onclick="shuffleArchive()"
          class="bg-brand-600 active:bg-brand-700 text-white text-xs font-bold px-3 py-2 rounded-full transition-colors">
          🔀 別の候補
        </button>
      </div>
      <div id="archive-cards" class="space-y-3">
        <p class="text-center text-sm text-gray-400 py-6">読み込み中...</p>
      </div>

      <!-- 予約投稿 -->
      <div class="bg-white rounded-xl px-4 py-3 border border-brand-100 shadow-sm mt-4">
        <p class="text-sm font-bold text-brand-800">📅 予約投稿</p>
        <p class="text-xs text-gray-500 mt-0.5">Streamlit で登録した予約投稿の一覧です</p>
      </div>
      <div id="scheduled-cards" class="space-y-3">
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
    let currentFilter = 'unposted';
    let archiveData = null;    // data/archives.json をキャッシュ
    let scheduledData = null;  // data/scheduled_posts.json をキャッシュ

    function setTab(tab) {{
      currentTab = tab;
      ['news','other','goods','archive'].forEach(t => {{
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
    function escapeHtml(s) {{
      return (s == null ? '' : String(s))
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }}

    function pickRandom(arr, count) {{
      const a = arr.slice();
      for (let i = a.length - 1; i > 0; i--) {{
        const j = Math.floor(Math.random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
      }}
      return a.slice(0, Math.min(count, a.length));
    }}

    function buildArchiveCardHtml(item) {{
      const text = item.text || '';
      const aid = item.id || '';
      const imgUrl = item.image_url || '';
      const intentUrl = 'https://x.com/intent/post?text=' + encodeURIComponent(text);
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
          '<a href="' + intentUrl + '" target="_blank" rel="noopener noreferrer" ' +
              'class="post-btn-archive flex items-center justify-center gap-1.5 bg-black text-white text-sm font-bold py-2.5 px-4 rounded-full active:bg-gray-700 transition-colors">' +
            '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.253 5.622 5.911-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>' +
            'にポスト' +
          '</a>' +
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
      // 3〜5 件のランダム選択（件数が少ない場合は全件）
      const k = Math.min(archiveData.length, 3 + Math.floor(Math.random() * 3));
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

    function buildScheduledCardHtml(item) {{
      const text = item.text || '';
      const sid = item.id || '';
      const wd = item.weekday || '*';
      const tm = item.time || '';
      const wdLabel = (wd === '*') ? '毎日' : (WEEKDAY_LABEL_JA[wd] || wd);
      const intentUrl = 'https://x.com/intent/post?text=' + encodeURIComponent(text);
      const safeText = escapeHtml(text).replace(/\\n/g, '<br>');
      return (
        '<div class="scheduled-card bg-white rounded-2xl shadow-sm p-4 border border-brand-100" data-scheduled-id="' + escapeHtml(sid) + '">' +
          '<div class="flex items-center gap-2 mb-2">' +
            '<span class="text-xs font-bold text-brand-800 bg-brand-50 px-2 py-0.5 rounded-full">🗓 ' + escapeHtml(wdLabel) + ' ' + escapeHtml(tm) + '</span>' +
          '</div>' +
          '<p class="text-sm text-gray-800 leading-relaxed mb-3 whitespace-pre-wrap break-words">' + safeText + '</p>' +
          '<a href="' + intentUrl + '" target="_blank" rel="noopener noreferrer" ' +
              'class="post-btn-scheduled flex items-center justify-center gap-1.5 bg-black text-white text-sm font-bold py-2.5 px-4 rounded-full active:bg-gray-700 transition-colors">' +
            '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.253 5.622 5.911-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>' +
            'にポスト' +
          '</a>' +
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
      setTab('news');
      setFilter('unposted');

      // Enter キーで適用
      ['kw-include', 'kw-exclude'].forEach(id => {{
        const el = document.getElementById(id);
        if (el) el.addEventListener('keydown', e => {{
          if (e.key === 'Enter') {{ e.preventDefault(); applyKeywords(); }}
        }});
      }});
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

SERVICE_WORKER = r"""const CACHE = 'butsuzo-v15';

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
