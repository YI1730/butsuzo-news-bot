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
NEWS_TAB_SOURCES       = {"google_news", "bangumi_tv"}
EXHIBITION_TAB_SOURCES = {"exhibition", "exhibition_rss"}
GOODS_TAB_SOURCES      = {"amazon_goods"}

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

    # X Web Intent URL
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
        <a href="{intent_url}" target="_blank" rel="noopener"
           onclick="handlePostClick(event,'{uid}')"
           class="post-btn flex-1 flex items-center justify-center gap-1.5 bg-black text-white text-sm font-bold py-2.5 px-4 rounded-full active:bg-gray-700 transition-colors">
          <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.744l7.737-8.835L1.254 2.25H8.08l4.253 5.622 5.911-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          にポスト
        </a>
        <a href="{url}" target="_blank" rel="noopener"
           class="text-xs text-gray-400 underline underline-offset-2 shrink-0">記事を読む</a>
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

    # 4タブに分割: ニュース / 特別展 / 特別公開 / 書籍・グッズ
    classified = NEWS_TAB_SOURCES | EXHIBITION_TAB_SOURCES | GOODS_TAB_SOURCES
    news_items  = [x for x in items_sorted if x.get("source") in NEWS_TAB_SOURCES]
    exhib_items = [x for x in items_sorted if x.get("source") in EXHIBITION_TAB_SOURCES]
    goods_items = [x for x in items_sorted if x.get("source") in GOODS_TAB_SOURCES]
    other_items = [x for x in items_sorted if x.get("source") not in classified]

    news_cards  = build_cards_with_separators(news_items)
    exhib_cards = build_cards_with_separators(exhib_items)
    goods_cards = build_cards_with_separators(goods_items)
    other_cards = build_cards_with_separators(other_items)
    news_count  = len(news_items)
    exhib_count = len(exhib_items)
    goods_count = len(goods_items)
    other_count = len(other_items)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#00AE95">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="仏像ニュース">
  <link rel="manifest" href="./manifest.json">
  <link rel="apple-touch-icon" href="./icons/icon-192.png">
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
      pointer-events: none;
    }}
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
      <button onclick="setTab('exhibition')" id="tab-btn-exhibition"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        🏛 特別展<span class="opacity-60 ml-0.5">{exhib_count}</span>
      </button>
      <button onclick="setTab('other')" id="tab-btn-other"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        🛕 特別公開<span class="opacity-60 ml-0.5">{other_count}</span>
      </button>
      <button onclick="setTab('goods')" id="tab-btn-goods"
        class="tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight">
        🛒 書籍・グッズ<span class="opacity-60 ml-0.5">{goods_count}</span>
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
    <div id="tab-exhibition" class="tab-pane space-y-3 hidden">
{exhib_cards}
    </div>
    <div id="tab-other" class="tab-pane space-y-3 hidden">
{other_cards}
    </div>
    <div id="tab-goods" class="tab-pane space-y-3 hidden">
{goods_cards}
    </div>
  </main>

  <script>
    let currentTab = 'news';
    let currentFilter = 'unposted';

    function setTab(tab) {{
      currentTab = tab;
      ['news','exhibition','other','goods'].forEach(t => {{
        const btn = document.getElementById('tab-btn-' + t);
        if (btn) {{
          btn.className = 'tab-btn flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors leading-tight ' +
            (t === tab ? 'bg-white text-brand-800 shadow' : 'text-brand-200');
        }}
        const pane = document.getElementById('tab-' + t);
        if (pane) pane.classList.toggle('hidden', t !== tab);
      }});
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

    function applyFilter() {{
      const activePane = document.getElementById('tab-' + currentTab);
      if (!activePane) return;
      let shown = 0;
      activePane.querySelectorAll('[data-item-id]').forEach(card => {{
        const posted = card.classList.contains('is-posted');
        let show = true;
        if (currentFilter === 'unposted') show = !posted;
        if (currentFilter === 'posted')   show = posted;
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

    function handlePostClick(event, itemId) {{
      localStorage.setItem('posted_' + itemId, '1');
      const card = document.querySelector('[data-item-id="' + itemId + '"]');
      if (card) {{
        card.classList.add('is-posted');
        const btn = card.querySelector('.post-btn');
        if (btn) btn.innerHTML = '投稿済み ✓';
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

    function init() {{
      document.querySelectorAll('[data-item-id]').forEach(card => {{
        if (localStorage.getItem('posted_' + card.dataset.itemId) === '1') {{
          card.classList.add('is-posted');
          const btn = card.querySelector('.post-btn');
          if (btn) btn.innerHTML = '投稿済み ✓';
        }}
      }});
      setTab('news');
      setFilter('unposted');
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
    "display": "standalone",
    "background_color": "#e6f9f7",
    "theme_color": "#00AE95",
    "lang": "ja",
    "icons": [
        {"src": "./icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "./icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}

SERVICE_WORKER = r"""const CACHE = 'butsuzo-v3';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // index.html と news.json は常に network-first（最新コンテンツを優先）
  if (url.endsWith('/') || url.includes('/index.html') || url.includes('/data/news.json')) {
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

    for size in (192, 512):
        path = icons_dir / f"icon-{size}.png"
        path.write_bytes(create_solid_png(size, ICON_COLOR))
        print(f"生成: docs/icons/icon-{size}.png")

    print("ダッシュボード生成完了")


if __name__ == "__main__":
    main()
