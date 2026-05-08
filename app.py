"""仏像ニュース管理 Streamlit アプリ

サイドバーから機能を選択する構成。

機能:
  - 📰 ニュースキーワード設定
      config/news_keywords.json を編集 → ローカル保存 →
      自動で git add / commit / push してリモートに反映する。
      これにより次回の GitHub Actions 実行から新しいキーワードが
      使われる。

ローカル実行例:
  streamlit run app.py
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
KEYWORDS_FILE = CONFIG_DIR / "news_keywords.json"

# config/news_keywords.json と同じ既定値
DEFAULT_INCLUDE = [
    "仏像", "如来", "開帳", "開扉", "菩薩", "秘仏",
    "明王", "神像", "重要文化財", "木造", "木像",
]
DEFAULT_EXCLUDE = [
    "グラビア", "返還", "ストリップ", "ヌード", "ギャンブル",
    "みほとけ", "クラブツーリズム", "賭博", "ゲーム",
]


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def load_keywords() -> dict:
    """JSON 設定ファイルを読み込む。存在しなければ既定値を返す。"""
    if KEYWORDS_FILE.exists():
        try:
            with KEYWORDS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "include": [k for k in data.get("include", []) if isinstance(k, str) and k.strip()],
                "exclude": [k for k in data.get("exclude", []) if isinstance(k, str) and k.strip()],
            }
        except Exception as e:
            st.warning(f"設定ファイルの読み込みに失敗（既定値を表示）: {e}")
    return {"include": list(DEFAULT_INCLUDE), "exclude": list(DEFAULT_EXCLUDE)}


def save_keywords(include: list[str], exclude: list[str]) -> None:
    """JSON 設定ファイルを書き出す。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"include": include, "exclude": exclude}
    with KEYWORDS_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def parse_keywords(text: str) -> list[str]:
    """改行・カンマ・空白で区切られた文字列をキーワード配列に変換。

    重複は最初の出現位置を維持して除去する。
    """
    raw = re.split(r"[,、\s\n\r]+", text or "")
    seen = set()
    out: list[str] = []
    for k in raw:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def build_query_preview(include: list[str], exclude: list[str]) -> str:
    """Google News RSS 用のクエリプレビュー文字列を組み立てる。"""
    inc = " OR ".join(include) if include else "<空>"
    exc = " ".join(f"-{w}" for w in exclude)
    if exclude:
        return f"({inc}) {exc}"
    return f"({inc})"


def run_git(args: list[str]) -> subprocess.CompletedProcess:
    """git コマンドを BASE_DIR 上で実行（テキスト出力 + check はしない）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        check=False,
    )


def commit_and_push() -> tuple[bool, str]:
    """config/news_keywords.json のみを add → commit → push する。

    Returns:
        (success, message)
    """
    # 1. 対象ファイルだけを staging に追加
    rel = str(KEYWORDS_FILE.relative_to(BASE_DIR))
    r = run_git(["add", rel])
    if r.returncode != 0:
        return False, f"git add 失敗:\n{r.stderr or r.stdout}"

    # 2. staged 差分の有無を確認
    diff = run_git(["diff", "--cached", "--quiet", "--", rel])
    if diff.returncode == 0:
        return True, "（差分なし：リモートは既に最新です）"

    # 3. commit
    msg = f"Update news keywords ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    r = run_git(["commit", "-m", msg, "--", rel])
    if r.returncode != 0:
        return False, f"git commit 失敗:\n{r.stderr or r.stdout}"

    # 4. push
    r = run_git(["push"])
    if r.returncode != 0:
        return False, (
            f"git push 失敗（ローカルのコミットは成功）:\n{r.stderr or r.stdout}\n"
            "→ コンフリクトの可能性があります。ターミナルから `git pull --rebase && git push` を試してください。"
        )

    return True, f"✅ コミット & プッシュ成功: {msg}"


def get_git_status() -> dict:
    """現在の HEAD コミットと remote 同期状況を取得（表示用）。"""
    info: dict = {}
    r = run_git(["log", "-1", "--format=%h %s (%ai)", "--", str(KEYWORDS_FILE.relative_to(BASE_DIR))])
    info["last_commit"] = r.stdout.strip() if r.returncode == 0 else "—"
    r = run_git(["status", "--porcelain", "--", str(KEYWORDS_FILE.relative_to(BASE_DIR))])
    info["dirty"] = bool(r.stdout.strip())
    return info


# ---------------------------------------------------------------------------
# 画面: ニュースキーワード設定
# ---------------------------------------------------------------------------


def render_keywords_page() -> None:
    st.header("📰 ニュースキーワード設定")
    st.caption(
        "ここで保存したキーワードは `config/news_keywords.json` に書き出され、"
        "GitHub にプッシュされます。次回の GitHub Actions 実行（JST 6:30 / 10:30 / 15:30 / 17:30 / 20:30 / 23:30）"
        "から `post_news.py` が新しいキーワードでニュースを取得します。"
    )

    cfg = load_keywords()
    git_info = get_git_status()

    with st.expander("📊 現在のリポジトリ状況", expanded=False):
        st.text(f"対象ファイル : config/news_keywords.json")
        st.text(f"最終コミット : {git_info['last_commit']}")
        if git_info["dirty"]:
            st.warning("⚠ ローカルに未コミットの変更があります。保存ボタンで一緒にプッシュされます。")
        else:
            st.success("✓ ローカルとリモートは同期済み")

    st.subheader("含むキーワード（OR 条件）")
    st.caption("いずれか1つでも記事タイトルに含まれていれば取得対象になります。改行・カンマ・空白のいずれで区切っても OK。")
    inc_text = st.text_area(
        "含むキーワード",
        value="\n".join(cfg["include"]),
        height=220,
        key="kw_inc_text",
        label_visibility="collapsed",
    )

    st.subheader("除外キーワード（NOT 条件）")
    st.caption("これらのいずれかが記事タイトルに含まれていれば除外されます。")
    exc_text = st.text_area(
        "除外キーワード",
        value="\n".join(cfg["exclude"]),
        height=160,
        key="kw_exc_text",
        label_visibility="collapsed",
    )

    inc_list = parse_keywords(inc_text)
    exc_list = parse_keywords(exc_text)

    col_inc, col_exc = st.columns(2)
    with col_inc:
        st.metric("含むキーワード数", len(inc_list))
    with col_exc:
        st.metric("除外キーワード数", len(exc_list))

    st.subheader("🔎 検索クエリ プレビュー")
    st.code(build_query_preview(inc_list, exc_list), language="text")

    if not inc_list:
        st.error("❌ 含むキーワードが空です。1つ以上指定してください（空のままだとフォールバック値が使われます）。")

    st.divider()

    save_col, push_col = st.columns([1, 1])
    with save_col:
        save_only = st.button(
            "💾 ローカルに保存のみ",
            use_container_width=True,
            disabled=not inc_list,
            help="config/news_keywords.json をローカルに書き込みます（プッシュなし）",
        )
    with push_col:
        save_and_push = st.button(
            "🚀 保存 & GitHub に反映",
            use_container_width=True,
            type="primary",
            disabled=not inc_list,
            help="ローカル保存後に自動で git add / commit / push を実行します",
        )

    if save_only or save_and_push:
        try:
            save_keywords(inc_list, exc_list)
            st.success(f"✅ ローカル保存完了: {KEYWORDS_FILE.relative_to(BASE_DIR)}")
        except Exception as e:
            st.error(f"❌ 保存に失敗: {e}")
            return

        if save_and_push:
            with st.spinner("git add → commit → push を実行中..."):
                ok, msg = commit_and_push()
            if ok:
                st.success(msg)
                st.info(
                    "次回の GitHub Actions 実行（最大 ~5 時間以内）で新キーワードが反映されます。"
                    "すぐ試したい場合は GitHub 画面で `Update Buddhist News Dashboard` ワークフローを手動実行してください。"
                )
            else:
                st.error(msg)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="仏像ニュース管理", page_icon="🛕", layout="wide")

    with st.sidebar:
        st.title("🛕 仏像ニュース管理")
        page = st.radio(
            "メニュー",
            ["📰 ニュースキーワード設定"],
            index=0,
        )
        st.divider()
        st.caption("ダッシュボード本体は GitHub Pages で公開中です。")

    if page == "📰 ニュースキーワード設定":
        render_keywords_page()


if __name__ == "__main__":
    main()
