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

git fetch -q origin main && git reset -q --hard origin/main || { echo "最新を取得できませんでした"; exit 1; }

ok=1   # 1: 2 日おきの記録（STAMP）を更新しない
if [ "$MODE" = upgrade ]; then
  waiting=$($PY -c "import json, scrape_magazines as m; print(m.missing_asin_count(json.load(open('docs/data/news.json'))['items']))" 2>/dev/null || echo 0)
  if [ "$waiting" = 0 ]; then echo "前回から 2 日たっておらず、Amazon のリンク待ちの雑誌も無いので見送り"; exit 0; fi
  echo "Amazon のリンク待ちの雑誌 $waiting 件を探し直す"
  $PY scrape_magazines.py --upgrade-only
else
  # 書籍を先に（検索の回数が少ないので、雑誌の検索で断られても書籍は取れるようにする）。間に休みを入れる
  $PY scrape_amazon.py; book=$?
  if [ $book -eq 0 ]; then
    sleep 120
    $PY scrape_magazines.py --amazon-only; mag=$?
    [ $mag -eq 0 ] && ok=0 || echo "雑誌は途中まで（終了コード $mag）。あすもう一度動かす"
  else
    echo "書籍を取れませんでした（終了コード $book）。雑誌も含めて、あすもう一度動かす"
  fi
fi

# 送信がほかの更新とぶつかったら、Amazon を検索し直さず、取れた分を最新の news.json に合流させる
RESULT=$(mktemp)
cp docs/data/news.json "$RESULT"
trap 'rm -f "$RESULT"' EXIT
changed=1; git diff --quiet -- docs/data/news.json && changed=0
if [ $changed -eq 0 ]; then
  echo "変更なし"; [ $ok -eq 0 ] && date +%s > "$STAMP"; exit 0
fi
for attempt in 1 2 3 4 5; do
  git add docs/data/news.json
  git commit -q -m "chore: Amazon の新刊・雑誌を取り込み（Mac）" || { echo "追加分は最新に入っていました"; [ $ok -eq 0 ] && date +%s > "$STAMP"; exit 0; }
  if git push -q origin HEAD:main; then
    echo "push しました"; [ $ok -eq 0 ] && date +%s > "$STAMP"; exit 0
  fi
  echo "push に失敗（ほかの更新と重なった）。最新に合流させます ($attempt)"; sleep 30
  git fetch -q origin main && git reset -q --hard origin/main || continue
  $PY tools/merge_amazon.py "$RESULT" || exit 1
done
echo "push できませんでした"; exit 1
