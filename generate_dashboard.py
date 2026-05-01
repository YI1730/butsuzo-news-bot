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

# アイコン背景色（和風ブラウン #92400e = RGB 146, 64, 14）
ICON_COLOR = (146, 64, 14)

SOURCE_LABELS: dict[str, str] = {
    "google_news":         "📰 ニュース",
    "kanbutsu":            "🛕 仏像公開",
    "東京国立博物館":       "🏛 東京国博",
    "奈良国立博物館":       "🏛 奈良国博",
    "京都国立博物館":       "🏛 京都国博",
    "九州国立博物館":       "🏛 九州国博",
    "京都非公開文化財特別公開": "⛩ 京都特別公開",
    "祈りの回廊":          "🙏 奈良秘仏",
}


# ---------------------------------------------------------------------------
# PNG 生成（Pillow 不要）
# ---------------------------------------------------------------------------


def create_solid_png(size: int, color: tuple[int, int, int]) -> bytes:
    """指定サイズ・単色の PNG バイト列を純 Python で生成する。"""
    r, g, b = color

    def chunk(name: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)

    # 各スキャンライン: filter_byte(0x00) + R G B × width
    scanline = b"\x00" + bytes([r, g, b]) * size
    raw = scanline * size
    compressed = zlib.compress(raw, 9)

    png = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit_depth=8, color_type=2(RGB), compression=0, filter=0, interlace=0
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


def build_card_html(item: dict) -> str:
    uid = item.get("id", "")
    title = item.get("title", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    url = item.get("url", "")
    source = item.get("source", "")
    header = item.get("header", "【仏像速報】")
    hashtags = item.get("hashtags", "#仏像")
    fetched_at = format_fetched_at(item.get("fetched_at", ""))
    source_label = SOURCE_LABELS.get(source, f"📌 {source}")

    # X Web Intent URL（テキスト＋URL を URL エンコード）
    post_text = f"{header}\n{item.get('title', '')}\n{hashtags}"
    intent_params = urllib.parse.urlencode({"text": post_text, "url": url})
    intent_url = f"https://x.com/intent/post?{intent_params}"

    return f"""    <div class="card bg-white rounded-2xl shadow-sm p-4 border border-amber-100 transition-opacity duration-300" data-item-id="{uid}">
      <div class="flex items-center justify-between mb-2 gap-2">
        <span class="text-xs font-medium text-amber-800 bg-amber-50 px-2 py-0.5 rounded-full whitespace-nowrap">{source_label}</span>
        <span class="text-xs text-gray-400 shrink-0">{fetched_at}</span>
      </div>
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


def build_html(items: list[dict], last_updated: str) -> str:
    lu_display = format_fetched_at(last_updated) if last_updated else "—"
    total = len(items)
    cards_html = "\n".join(build_card_html(item) for item in items)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#92400e">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="仏像ニュース">
  <link rel="manifest" href="./manifest.json">
  <link rel="apple-touch-icon" href="./icons/icon-192.png">
  <title>仏像ニュース ダッシュボード</title>
  <script src="https://cdn.tailwindcss.com"></script>
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
<body class="bg-amber-50 min-h-screen pb-8">

  <!-- ヘッダー -->
  <header class="bg-amber-900 text-white px-4 pt-safe-top sticky top-0 z-20 shadow-lg">
    <div class="flex items-center justify-between py-3 max-w-xl mx-auto">
      <div>
        <h1 class="text-base font-bold leading-tight">🛕 仏像ニュース</h1>
        <p class="text-xs text-amber-300 mt-0.5">更新: {lu_display}</p>
      </div>
      <span id="count" class="text-xs bg-amber-700 text-amber-100 px-2 py-1 rounded-full font-medium">{total}件</span>
    </div>
    <!-- フィルターバー -->
    <div class="flex gap-2 pb-3 max-w-xl mx-auto">
      <button onclick="setFilter('unposted')" id="btn-unposted"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">未投稿</button>
      <button onclick="setFilter('all')" id="btn-all"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">すべて</button>
      <button onclick="setFilter('posted')" id="btn-posted"
        class="filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors">投稿済み</button>
      <button onclick="resetAll()" class="ml-auto text-xs text-amber-400 underline">リセット</button>
    </div>
  </header>

  <!-- カードリスト -->
  <main id="card-list" class="px-4 py-4 space-y-3 max-w-xl mx-auto">
{cards_html}
  </main>

  <script>
    let currentFilter = 'unposted';

    function setFilter(mode) {{
      currentFilter = mode;
      const labels = {{ unposted: '未投稿', all: 'すべて', posted: '投稿済み' }};
      ['unposted','all','posted'].forEach(m => {{
        const btn = document.getElementById('btn-' + m);
        if (!btn) return;
        btn.className = 'filter-btn px-3 py-1 rounded-full text-xs font-medium transition-colors ' +
          (m === mode ? 'bg-white text-amber-900' : 'bg-amber-800 text-amber-200');
      }});
      let shown = 0;
      document.querySelectorAll('[data-item-id]').forEach(card => {{
        const posted = card.classList.contains('is-posted');
        let show = true;
        if (mode === 'unposted') show = !posted;
        if (mode === 'posted')   show = posted;
        card.style.display = show ? '' : 'none';
        if (show) shown++;
      }});
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
      // 未投稿フィルター中なら少し遅らせてカードを非表示
      if (currentFilter === 'unposted') {{
        setTimeout(() => {{
          if (card) card.style.display = 'none';
          setFilter(currentFilter);
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
    "background_color": "#fffbeb",
    "theme_color": "#92400e",
    "lang": "ja",
    "icons": [
        {"src": "./icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "./icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}

SERVICE_WORKER = r"""const CACHE = 'butsuzo-v1';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // news.json はネットワーク優先（最新データを取得）、失敗時はキャッシュ
  if (url.includes('/data/news.json')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // その他はキャッシュ優先
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    # news.json 読み込み
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

    # index.html
    html = build_html(items, last_updated)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print("生成: docs/index.html")

    # manifest.json
    (DOCS_DIR / "manifest.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("生成: docs/manifest.json")

    # sw.js
    (DOCS_DIR / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    print("生成: docs/sw.js")

    # PWA アイコン（単色 PNG）
    for size in (192, 512):
        path = icons_dir / f"icon-{size}.png"
        path.write_bytes(create_solid_png(size, ICON_COLOR))
        print(f"生成: docs/icons/icon-{size}.png")

    print("ダッシュボード生成完了")


if __name__ == "__main__":
    main()
