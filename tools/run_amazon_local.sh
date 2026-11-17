#!/bin/bash
# Amazon の取り込み（書籍・グッズの新刊と、雑誌の新しい号）を運営者の Mac で動かす。
# GitHub Actions からの Amazon の検索は 503 で断られるため（2026-11）。
# launchd（~/Library/LaunchAgents/link.butsuzo.amazon.plist）が毎日起こし、前回の成功から
# 2 日たっていれば動く。合間の日は、Amazon のリンクがまだ無い雑誌だけを探し直す（PWA はそれまで雑誌を出さないため）。
# 作業用の別のクローン（~/.butsuzo-amazon-runner）で取り込み、news.json だけを push する。
# 雑誌の判定（AI）は次の GitHub Actions の実行が行うので、この Mac に API キーは要らない。
set -u
REPO_URL="https://github.com/YI1730/butsuzo-news-bot.git"
WORK="$HOME/.butsuzo-amazon-runner"
STAMP="$WORK/.last_success"
LOG_DIR="$HOME/Library/Logs/butsuzo"
INTERVAL_SEC=$((2 * 24 * 3600 - 3600))   # 2 日（起動時刻のずれを見て 1 時間の余裕）
PY=/usr/bin/python3
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/amazon.log" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S')"

if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ]; then
  last=$(cat "$STAMP"); now=$(date +%s)
  [ $((now - last)) -lt $INTERVAL_SEC ] && MODE=upgrade
fi
MODE=${MODE:-full}

if [ ! -d "$WORK/.git" ]; then
  git clone --depth 50 "$REPO_URL" "$WORK" || exit 1
fi
cd "$WORK" || exit 1
git config user.name "butsuzo-amazon (Mac)"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3; do
  git fetch -q origin main && git reset -q --hard origin/main || { sleep 30; continue; }
  if [ "$MODE" = upgrade ]; then
    waiting=$($PY -c "import json, scrape_magazines as m; print(m.missing_asin_count(json.load(open('docs/data/news.json'))['items']))" 2>/dev/null || echo 0)
    if [ "$waiting" = 0 ]; then echo "前回から 2 日たっておらず、Amazon のリンク待ちの雑誌も無いので見送り"; exit 0; fi
    echo "Amazon のリンク待ちの雑誌 $waiting 件を探し直す"
    $PY scrape_magazines.py --upgrade-only; mag=1   # 合間の実行なので、2 日おきの記録（STAMP）は更新しない
  else
  # 雑誌を先に（書籍の検索のあとだと、続けて検索したことで確認ページを返されやすい）。間に休みを入れる
  $PY scrape_magazines.py --amazon-only; mag=$?
  [ $mag -ne 0 ] && echo "雑誌は途中まで（終了コード $mag）。あすもう一度動かす"
  sleep 120
  $PY scrape_amazon.py || echo "scrape_amazon.py が失敗（続行）"
  fi
  if git diff --quiet -- docs/data/news.json; then
    echo "変更なし"; [ $mag -eq 0 ] && date +%s > "$STAMP"; exit 0
  fi
  git add docs/data/news.json
  git commit -q -m "chore: Amazon の新刊・雑誌を取り込み（Mac）"
  if git push -q origin HEAD:main; then
    echo "push しました"; [ $mag -eq 0 ] && date +%s > "$STAMP"; exit 0
  fi
  echo "push に失敗（ほかの更新と重なった）。取り直します ($attempt)"; sleep 60
done
echo "3 回とも push できませんでした"; exit 1
