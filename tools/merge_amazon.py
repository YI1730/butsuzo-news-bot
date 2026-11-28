"""Mac で Amazon から取れた結果（引数の news.json）を、いまの docs/data/news.json に合流させる。
push がほかの更新とぶつかったときに、Amazon を検索し直さないために使う（tools/run_amazon_local.sh）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_store import trim_per_source  # noqa: E402
from scrape_amazon import load_news_data, save_news_data  # noqa: E402
from scrape_magazines import magazine_key  # noqa: E402

ours = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
data = load_news_data()
items = data["items"]
by_id = {it["id"]: it for it in items}
asins = {it.get("asin") for it in items if it.get("source") == "amazon_goods" and it.get("asin")}

added = []
for it in ours["items"]:
    if it.get("source") != "amazon_goods":
        continue
    cur = by_id.get(it["id"])
    if cur is None:
        if it.get("asin") and it["asin"] in asins:
            continue
        added.append(it)
    elif it.get("book_kind") == "magazine":
        # 雑誌の Amazon の商品ページへの差し替えと、探した回数
        if it.get("asin") and not cur.get("asin"):
            for k in ("asin", "url", "image_url"):
                cur[k] = it.get(k, "")
        if it.get("mag_amazon_tries"):
            cur["mag_amazon_tries"] = max(it["mag_amazon_tries"], cur.get("mag_amazon_tries", 0))
data["items"] = trim_per_source(added + items)

pending = data.get("mag_amazon_pending", [])
known = {e["id"] for e in pending}
judged = set(data.get("mag_judged", []))
for e in ours.get("mag_amazon_pending", []):
    if e["id"] not in known and magazine_key(e["magazine"], e["issue_label"]) not in judged:
        pending.append(e)
data["mag_amazon_pending"] = pending[-50:]
seen = data.get("mag_az_seen", [])
data["mag_az_seen"] = (seen + [a for a in ours.get("mag_az_seen", []) if a not in seen])[-400:]
if "mag_az_order" in ours:
    data["mag_az_order"] = ours["mag_az_order"]
save_news_data(data)
print(f"合流: {len(added)} 件を追加")
