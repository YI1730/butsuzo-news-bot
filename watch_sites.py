"""寺院・博物館・文化庁の公式サイトを監視し、新着のお知らせをニュースとして取り込む。

新聞に載る前の一次情報（御開帳・特別公開・展覧会・文化財の新指定など）を拾うため、
config/watch_sites.json のページを毎時取得し、前回なかったリンク＝新着のお知らせとして
docs/data/news.json に source "official_site" で追加する。
仏像に関係するかどうかは、ニュースと同じく curate_news.py の AI 採点で判定する。

- 「過去に一度でも見たことのあるリンク」は新着にしない。表示するリンクを毎回入れ替える
  サイト（東京国立博物館など）で、同じリンクが何度も新着扱いになるのを防ぐ
- 初回、または一度に大量のリンクが新しく現れたサイト（リニューアル等）は記録だけする
- 見たリンクの履歴は data/watch_state.json に保存する（サイトごとに最大 SEEN_MAX 件）
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from news_store import trim_per_source
from post_news import (
    JST, RESOLVE_USER_AGENT, extract_og_description, extract_og_image,
    item_id, load_news_data, save_news_data,
)

BASE_DIR = Path(__file__).parent
SITES_FILE = BASE_DIR / "config" / "watch_sites.json"
STATE_FILE = BASE_DIR / "data" / "watch_state.json"
SOURCE = "official_site"

MIN_LINK_TEXT = 6          # これより短いリンク文字列（「詳細」「こちら」等）は対象外
MAX_NEW_PER_SITE = 8       # 1サイト・1回で取り込む上限
REDESIGN_RATIO = 0.5       # 表示中リンクの半分以上が未知ならリニューアルとみなし記録だけする
SEEN_MAX = 1500            # サイトごとに覚えておくリンクの上限
# 言語を指定しないと英語版を返すサイトがある（東京国立博物館など）ので日本語を明示する
HEADERS = {"User-Agent": RESOLVE_USER_AGENT, "Accept-Language": "ja,en;q=0.5"}
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4")
SKIP_WORDS = ("採用", "求人", "プライバシー", "サイトマップ", "お問い合わせ", "アクセス",
              "English", "Instagram", "Facebook", "YouTube", "ログイン", "会員")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def page_links(url: str) -> list[tuple[str, str]] | None:
    """ページ本文中のリンク (URL, 文字列) を返す。取得失敗時は None。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"  取得失敗: {e}", file=sys.stderr)
        return None
    soup = BeautifulSoup(r.content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(r.url, a["href"]))[0]
        text = " ".join(a.get_text(" ", strip=True).split())
        if not href.startswith("http") or href in seen:
            continue
        if len(text) < MIN_LINK_TEXT or any(w in text for w in SKIP_WORDS):
            continue
        if urlparse(href).path.lower().endswith(SKIP_EXT):
            continue
        seen.add(href)
        links.append((href, text[:120]))
    return links


def link_meta(url: str) -> tuple[str, str]:
    """リンク先の (画像, 要約)。PDF などは空。"""
    if urlparse(url).path.lower().endswith(".pdf"):
        return "", ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if "html" not in r.headers.get("content-type", ""):
            return "", ""
        return extract_og_image(r.content), extract_og_description(r.content)
    except Exception:
        return "", ""


def main() -> int:
    sites = load_json(SITES_FILE, {}).get("sites", [])
    state = load_json(STATE_FILE, {})
    data = load_news_data()
    existing = {it["id"] for it in data["items"]}
    now = datetime.now(JST).isoformat(timespec="seconds")
    added = baseline = failed = 0

    for site in sites:
        url, name = site["url"], site["name"]
        links = page_links(url)
        time.sleep(1)
        if links is None:
            failed += 1
            continue
        seen = state.get(url, {}).get("seen", [])
        seen_set = set(seen)
        unknown = [(h, t) for h, t in links if h not in seen_set]
        first = not seen
        redesign = len(links) >= 10 and len(unknown) >= len(links) * REDESIGN_RATIO
        # 新しく見たリンクを履歴に足す（古いものから捨てる）
        seen = (seen + [h for h, _ in unknown])[-SEEN_MAX:]
        state[url] = {"seen": seen, "checked_at": now}
        if first or redesign:
            baseline += 1
            print(f"[記録] {name}: リンク {len(links)}件（{'初回' if first else 'リニューアル疑い'}）")
            continue

        new = unknown[:MAX_NEW_PER_SITE]
        for href, text in new:
            uid = item_id(href)
            if uid in existing:
                continue
            image, desc = link_meta(href)
            data["items"].insert(0, {
                "id": uid,
                "title": f"{name}｜{text}",
                "url": href,
                "source": SOURCE,
                "site_kind": site.get("kind", ""),
                "header": "【仏像速報】",
                "hashtags": "",
                # 公式サイトは公開日が取れないことが多いので、検出した時刻を公開日とみなす
                "fetched_at": now,
                "published_at": now,
                "image_url": image,
                "description": desc,
            })
            existing.add(uid)
            added += 1
            print(f"[新着] {name}: {text}")

    data["items"] = trim_per_source(data["items"])
    save_news_data(data)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"::notice::公式サイト監視 {len(sites)}件: 新着 {added}件 / 初回記録 {baseline}件 / 取得失敗 {failed}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
