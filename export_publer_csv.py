"""訪問記（data/archives.json）を Publer 一括インポート用の CSV に書き出す。

Publer の Bulk Import にアップロードして、Auto-Schedule / Recycle で
evergreen 配信するための CSV を生成する。

訪問記は本文に pic.twitter.com/xxx（既存ツイートのメディアリンク）を含むため、
テキストのみで投稿すれば X 側が画像を展開する。よって media 列は空でよい。

CSV 列:
    text       … 投稿本文（pic リンク込み）
    media_url  … 画像URL（訪問記は空。告知等で使う場合に利用）
    category   … 分類（visit 固定）
    id         … archives.json の id（参照用）

使い方:
    python export_publer_csv.py                    # publer_visit.csv を出力
    python export_publer_csv.py -o out.csv         # 出力先を指定
    python export_publer_csv.py --max-len 280      # 文字数上限（既定280）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
ARCHIVES_FILE = BASE_DIR / "data" / "archives.json"

# 標準アカウントのツイート上限。超過はスキップ（X 投稿でエラーになるため）。
DEFAULT_MAX_LEN = 280


def load_archives() -> list[dict]:
    if not ARCHIVES_FILE.exists():
        print(f"訪問記ファイルがありません: {ARCHIVES_FILE}", file=sys.stderr)
        return []
    with ARCHIVES_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("items", [])
    return [x for x in items if isinstance(x, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Publer 用 CSV エクスポート")
    parser.add_argument("-o", "--output", default="publer_visit.csv",
                        help="出力CSVパス（既定: publer_visit.csv）")
    parser.add_argument("--max-len", type=int, default=DEFAULT_MAX_LEN,
                        help=f"本文の最大文字数（既定: {DEFAULT_MAX_LEN}）")
    args = parser.parse_args()

    archives = load_archives()
    if not archives:
        print("訪問記が0件のため終了", file=sys.stderr)
        return 1

    rows: list[dict] = []
    skipped_long = 0
    skipped_empty = 0
    for a in archives:
        aid = a.get("id", "")
        text = (a.get("text") or "").strip()
        media = (a.get("image_url") or "").strip()
        if not text:
            skipped_empty += 1
            continue
        if len(text) > args.max_len:
            skipped_long += 1
            continue
        rows.append({
            "text": text,
            "media_url": media,
            "category": "visit",
            "id": aid,
        })

    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        # utf-8-sig（BOM付き）で Excel / Publer の文字化けを防ぐ
        writer = csv.DictWriter(f, fieldnames=["text", "media_url", "category", "id"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"訪問記 全{len(archives)}件")
    print(f"  出力: {len(rows)}件 → {out_path}")
    if skipped_long:
        print(f"  スキップ(280字超): {skipped_long}件")
    if skipped_empty:
        print(f"  スキップ(空テキスト): {skipped_empty}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
