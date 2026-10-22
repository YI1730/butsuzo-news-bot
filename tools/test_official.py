"""展覧会の公式ページ検索（curate_news.find_official_page）を、指定した記事で試す（確認用・手動実行）。"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import anthropic  # noqa: E402
from curate_news import Usage, find_official_page  # noqa: E402
from post_news import load_news_data  # noqa: E402

words = [w for w in os.environ.get("TITLE_WORDS", "").split(",") if w]
client, usage = anthropic.Anthropic(), Usage()
for it in load_news_data()["items"]:
    if it.get("ai") and any(w in it["title"] for w in words):
        print(it["title"][:50], "→", find_official_page(client, usage, it))
        words = [w for w in words if w not in it["title"]]
print(f"費用 約${usage.cost():.4f}（Web 検索料は別途 1 回 $0.01）")
