"""採点ルールの見直し案（tools/proposed_score.py）で、指定した記事を試し採点する。news.json は変えない。"""
import copy, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anthropic  # noqa: E402
import curate_news  # noqa: E402
import proposed_score  # noqa: E402
from post_news import load_news_data  # noqa: E402

words = [w for w in os.environ.get("TITLE_WORDS", "").split(",") if w]
items = [it for it in load_news_data()["items"]
         if it.get("source") in curate_news.TARGET_SOURCES and it.get("ai")
         and any(w in it["title"] for w in words)]
before = {it["id"]: (it["ai"].get("score"), it["ai"].get("reason", "")) for it in items}
trial = [copy.deepcopy(it) for it in items]
curate_news.SCORE_SYSTEM = proposed_score.apply(curate_news.SCORE_SYSTEM)
usage = curate_news.Usage()
curate_news.score_items(anthropic.Anthropic(), usage, trial, [])
for it in trial:
    b = before[it["id"]]
    print(f"{b[0]:>3} → {it['ai']['score']:>3}{' (重複)' if it['ai'].get('duplicate') else ''} | {it['title'][:50]}")
    print(f"      新しい理由: {it['ai'].get('reason', '')}")
print(f"費用 約${usage.cost():.4f}")
