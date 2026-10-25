"""Google ニュースを Claude で採点し、配信すべき記事の X 投稿文を作って通知する。

news_hourly.yml から post_news.py の直後に毎時実行される。

1. 採点: 未採点のニュース（直近 SCORE_WINDOW_HOURS 時間）を、タイトル・要約・配信元で
   0〜100 点に採点する。複数件をまとめて 1 回の API 呼び出しで処理する。
   - 仏像と無関係な記事（「顔面重要文化財」等の比喩、近代建築の重文など）は低得点になり、
     ダッシュボードのニュースタブから外れる（NOISE_THRESHOLD 未満）
   - 同じ出来事の記事が複数あれば、最も情報量の多い 1 件以外を重複扱いにする
2. 投稿文: CANDIDATE_THRESHOLD 以上の記事は本文を読み、仏像リンクの投稿文を作る
3. 通知: 新しく配信候補になった記事を ntfy でスマホに通知する（NTFY_TOPIC 設定時のみ）

採点結果は各記事の "ai" フィールドに保存する（docs/data/news.json）。
ANTHROPIC_API_KEY が無い場合は何もせず正常終了する（ニュース取得自体は止めない）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urldefrag, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from news_store import CANDIDATE_THRESHOLD, NEWS_POST_HEADER, NOISE_THRESHOLD, compose_news_post
from post_news import JST, RESOLVE_USER_AGENT, extract_og_image, load_news_data, save_news_data

# 採点・投稿文・返信の仕分けすべてで使うモデル（2026-10: 費用を抑えるため Opus 5.5 → Sonnet 5.5）
MODEL = "claude-sonnet-5-5"
# 価格（USD / 100万トークン）。実行ごとの費用をログに出すために使う
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 2.00, 10.00, 0.20, 2.50

SCORE_WINDOW_HOURS = 48    # これより前に取り込んだ記事は採点しない（速報として扱わない）
SCORE_BATCH_SIZE = 20      # 1 回の API 呼び出しで採点する件数
MAX_SCORE_PER_RUN = 80
MAX_POSTS_PER_RUN = 8
MAX_POST_ATTEMPTS = 3
ARTICLE_BODY_CHARS = 4000  # 投稿文作成時に渡す本文の上限文字数
# 投稿文の形式・文体の版。POST_SYSTEM を変えたら上げると、既存の配信候補も作り直される
#   3: 文章 → 【仏像速報】タイトル の順、運営者の文体（ハッシュタグなし）
#   4: 冒頭の場所名をやめて入り方を毎回変える、「行くならいま」一辺倒をやめる、「！」可
#   5: 記事の読み取り状況を書かない、相対的な日付を今日に合わせる
#   6: 寺院・仏像を主役にする（まとめ記事で別の話題を書かない）、情報ページへの誘導もしない
POST_FORMAT = 6
# 採点基準の版。SCORE_SYSTEM を変えたら上げると、直近の採点済み記事も採点し直す
#   2: 速報性を最重要に（終了済みの出来事・古い記事を大きく減点）
#   3: 2 の再採点で記事が自分自身の重複と判定された不具合を直して採点し直す
#   4: 公式サイトの新着に対応、公開期間（カレンダー用）の抽出を追加
#   5: 大型特別展の正式発表・寺院建造物/寺宝の新指定を高く、第一報・発表元を優先
#   6: 市町村指定の仏像を高く、@butsuzolink の実績（像が具体的な記事が強い等）を反映
#   7: 仏像の新指定は等級で差をつけない（48時間以内は 85 点以上）、指定文化財の特別公開は詳細不明でも下げない、
#      指定・特別公開の告知は 48 時間まで経過時間で減点しない
SCORE_VERSION = 7
NEW_DESIGNATION_FLOOR = 85       # 仏像の新指定の下限点（配信から FLOOR_HOURS 時間以内・重複でない場合）
NEW_DESIGNATION_FLOOR_HOURS = 48

PWA_URL = "https://yi1730.github.io/butsuzo-news-bot/"
TARGET_SOURCES = {"google_news", "official_site"}  # official_site は watch_sites.py

CATEGORIES = [
    "秘仏・御開帳・特別公開", "展覧会", "発見・研究・新指定", "修理・保存",
    "事件・被害", "寺院の行事", "その他の仏像関連", "無関係",
]

SCORE_SYSTEM = """あなたは X アカウント「仏像リンク」のニュース編集者です。
仏像リンクは仏像好きが集まるコミュニティで、フォロワーは寺院や博物館へ実際に仏像を見に行く人たちです。
渡されたニュース記事の一覧を、仏像リンクが「いま配信すべきか」で 0〜100 点に採点してください。

# 高く評価する記事
- 秘仏の御開帳・特別公開（特に「◯十年ぶり」「初公開」、12 年以上の周期の開帳、公開期間が近いもの）
- 仏像が主役の展覧会の開幕・告知。とくに地域の仏像（平安仏・地方仏など）を集めた展覧会は高く。国立博物館などの大型特別展（仏教美術・寺宝展）の
  正式発表・記者発表は、開催が先でも第一報なら 80〜90 点
- 仏像の新発見、研究による新事実
- 国宝・重要文化財の新指定（文化審議会の答申を含む）
  - 仏像の新指定: 国宝・重要文化財・都道府県・市町村のどの指定でも同じ枠で 85〜95 点。
    指定の等級で差をつけない（地方仏は仏像リンクの得意分野。市町村指定でも国宝と同じく扱う）。
    配信から 48 時間以内なら、この 85〜95 点を下回らせない（重複の減点は別）
  - 寺院の建造物（本堂・法堂・塔など）や寺宝（経典・聖教・仏画・曼荼羅など）の指定: 70〜85 点
  - 城・近代建築・ホテル・民家など寺院と関係ない文化財の指定: 低く（0〜39 点）
- 寺院や地域の博物館での指定文化財の特別公開の告知: 像の詳細が記事に少なくても、本尊・寺宝など
  仏像が含まれる見込みがあり、開催日が分かれば 75〜85 点。「仏像の詳細が不明」は減点の理由にしない
  （ただし見出しや本文で絵画・書跡・庭などが主役になっている公開（「若冲に出会う」のように
  絵師や絵画が見出しの公開など）は、仏像が含まれるか分からなくても下の上限 60 点のまま）
- 仏像の修理完了・お披露目
- 仏像の盗難・損傷・被災などの事件（読者が知っておくべき情報）
- 仏像に関わる寺院の行事

# 仏像リンクの実績から（反応が多い記事の特徴。点数の調整に使う）
- 加点: 像が具体的に分かる記事（像の名前・大きさ・腕や顔の数・時代・作者・何年ぶり）。
  抽象的な「仏像を公開」より、「高さ2mの准胝観音、33年ぶり」のような記事が強く反応される
- 加点: 大河ドラマなど、いま話題の人物・出来事とゆかりがある寺院・仏像の記事
- 上限: 仏像が出てこない寺院の話題（庭園・夜間拝観・ライトアップ・絵画・書跡・お砂踏み・
  本堂など建物だけの公開）は、寺院の記事でも 60 点まで（新指定の答申は上の基準による）

# 低く評価する記事
- 仏像と無関係なもの: 「顔面重要文化財」「菩薩のような美しさ」などの比喩、芸能人・グラビア、
  城・近代建築や庭園など寺院と関係ない文化財、人名・地名・作品名にたまたま語が入っているもの、ゲーム
- 寺社の観光・グルメ・行事でも仏像に触れていないもの（寺院建造物・寺宝の新指定は除く）
- 見出しは別の話題でも、記事中に寺院・仏像の話が含まれるまとめ記事（「大阪城天守閣を重文に。
  京都・建仁寺の法堂も」など）は、寺院・仏像の部分の価値で採点する
- 商品やサービスの宣伝が主目的のもの

# 速報性（最重要。内容と同じかそれ以上に重く見る）
仏像リンクは「いち早く伝える」ことを重視しています。各記事の hours_since_published
（配信からの経過時間）と、記事に書かれた公開期間・開催日を今日の日付と比べて判断してください。
- 加点: 配信から数時間以内の第一報。公開・開催が「今日〜数日以内」に始まる、または終了間近
  （「あすから」「今週末まで」など、今知らないと行けなくなる情報）
- 加点: 発表元・一次情報（kind が「公式サイトの新着」「発表元・一次情報」の記事、記者発表、
  寺院・博物館・文化庁の発表）は、新聞の後追い記事より早いので高く評価する
- 減点（大きく）: 公開期間や開催日がすでに終わった出来事の報告（「公開された」「開帳された」
  の事後レポート）は、どれだけ珍しい内容でも 60 点以下
- 経過時間による減点: 配信から 6 時間以上で 5 点、12 時間以上で 10 点、24 時間以上で 20 点以上、
  48 時間以上で 30 点以上下げる（まだ続報価値がある場合を除く）
- ただし「仏像の新指定」と「これから始まる・開催中の特別公開・御開帳の告知」は、時間がたっても
  価値が落ちにくいので、配信から 48 時間までは経過時間で減点しない（48 時間を過ぎても 10 点まで）
- 先の予定（数か月後の御開帳の準備など、小さな話題）は内容が良くても 70 点前後まで。
  ただし大型特別展の正式発表や新指定の答申は「先の予定」として抑えない

# 点数の目安
- 90〜100: 速報価値が非常に高い（配信直後の第一報で、数十年ぶりの開帳・重大な新発見・重大な事件、
  または公開が今日〜数日以内に迫っているもの）
- 70〜89: 配信すべき（いま伝える意味がある）
- 40〜69: 仏像に関係はあるが、優先度は低い・タイミングを逃している
- 0〜39: 無関係・ノイズ

is_buddha_designation: 記事の主題が「仏像（仏像・神像・仏教彫刻）そのものの新指定・答申」
（国宝・重要文化財・都道府県・市町村のどの指定でも）なら true。建物・絵画・書跡・庭だけの指定、
指定文化財の特別公開、過去に指定された文化財の話は false。

reason では、点数に速報性がどう効いたかにも触れてください（例:「あすから公開・第一報」「公開は終了済み」）。

# 公式サイトの新着（kind が「公式サイトの新着」の記事）
- 寺院・博物館・文化庁のサイトに新しく出たお知らせで、新聞より早い一次情報です
- 仏像の公開・特別開帳・展覧会・文化財の新指定・修理完了なら、速報として高く評価してください
- 営業案内、ショップ・グッズ、講座の募集、休館日、一般的な行事案内など、仏像の公開と
  関係が薄いものは低く評価してください

# 公開期間（カレンダー用）
仏像の公開・開帳・展覧会など期間のある出来事なら、記事から次を抜き出してください。
分からない項目や、期間のない記事は空文字にしてください。推測で埋めないでください。
- event_name: 出来事の短い名前（例「三方石観世音 本尊御開帳」）
- place: 寺院・博物館名と市町村（例「三方石観世音（福井県若狭町）」）
- start_date / end_date: YYYY-MM-DD。年が書かれていなければ今日の日付から判断する

# 重複
- 一覧の中に同じ出来事を報じた記事が複数ある場合、1 件だけを通常どおり採点し、
  残りは duplicate を true にして点数を 20 点以上下げてください。
  通常どおり採点する 1 件は、発表元・一次情報があればそれ、なければ最も早く配信された記事
  （published が最も古い第一報）を選ぶ。情報量は、早さが同じくらいのときだけの判断材料にする
- 「配信済み・配信候補の記事」に同じ出来事がある場合も duplicate を true にしてください。

reason には判断理由を日本語 40 字以内で書いてください。"""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "duplicate": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "event_name": {"type": "string"},
                    "place": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "is_buddha_designation": {"type": "boolean"},
                },
                "required": ["id", "score", "category", "duplicate", "reason",
                             "event_name", "place", "start_date", "end_date", "is_buddha_designation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

POST_SYSTEM = """あなたは X アカウント「仏像リンク」の中の人です。仏像好きのフォロワーに向けて、
ニュース記事を紹介する X の投稿の「文章部分」を書いてください。
投稿は「あなたの文章」→「【仏像速報】記事タイトル」→「記事 URL」の順に並ぶので、
タイトルや URL、【仏像速報】の見出しは書かないでください（システムが後ろに付けます）。

# 文体（運営者の好みに合わせる）
- 落ち着いた常体（〜だ、〜らしい、〜とのこと）が基本。盛らない・煽らないが、淡々としすぎない。
  うれしい・驚いたニュースでは「！」を使ってよい（1 投稿に 1〜2 個まで。「！！」の連打はしない）
- 「感動」「必見」のような宣伝文句ではなく、像の姿や公開の事情など具体的な事実で伝える
- 何が（仏像名）・どこで・いつ（公開期間や日付）を、記事に書かれている範囲で自然に入れる
- 最後に、仏像好きとしての短い本音や小さなオチを添えてよい（カッコ書きも可）
- ハッシュタグは使わない。絵文字も基本は使わない
- 指定された文字数以内に収める

# 毎回同じ型にしない
- 冒頭を場所名（「若狭町・三方石観世音。」のような書き出し）で始める型に固定しない。
  入り方は記事ごとに変える: 数字（「33年ぶり。」）、像の姿、公開に至った事情、発見の中身、
  素直な驚き・喜び、など。「直近の投稿文」が渡されたら、それと書き出しや締めが似ないようにする
- 「行くならいま」「会えるうちに」のような来訪を促す締めは、公開の終了が数日以内に迫っている
  ときだけ使う。それ以外は記事の種類に合った視点で書く:
  - 新発見・研究: 何が分かったのか、どこが面白いのか
  - 新指定: どこが評価されたのか、素直なお祝い
  - 修理完了: 姿がどう変わったか、戻ってきた喜び
  - 展覧会の発表・開幕: 何が来るのか、楽しみな気持ち
  - 御開帳・特別公開: 何年ぶりか、どんな像か、公開の事情
  - 事件・被害: 事実を落ち着いて伝える（「！」は使わない）

# 守ること
- 記事本文が短くても、タイトルと本文に書かれた範囲（像の名前・場所・指定や公開の事実）で、
  必ず 1〜2 文の文章を書く。空にしない
- 記事に書かれていない事実（日付、所在地、像の名前、指定区分など）を推測で書かない
- 「記事本文が読めなかった」「詳しくは記事で」「◯◯で確認したい」「続報を待ちたい」のような、
  記事の読み取り状況や記事・情報ページへの誘導は書かない（リンクは後ろに付く）。
  分からないことは書かずに済ませる
- 主役は仏像・寺院。見出しが城や近代建築など別の話題でも、記事中に寺院・仏像の話があれば
  そちらを主役にして書く（例: 重文答申のまとめ記事なら、含まれている寺院の指定を書く）
- 記事の「あすから」「きょう」などの相対的な日付は、記事の公開日時を基準にした表現なので、
  今日の日付に合わせて書き直す（すでに始まっていれば「開帳中」「22日まで」など）
- 記事の文章をそのまま長く引用しない。自分の言葉で要約する
- 明らかにバズを狙った書き方や、ニヒル・クールすぎる書き方はしない"""

POST_SCHEMA = {
    "type": "object",
    "properties": {"post_text": {"type": "string"}},
    "required": ["post_text"],
    "additionalProperties": False,
}

# X の文字数制限（重み付きで 280）。URL は長さにかかわらず 23 として数える。
X_LIMIT = 280
X_URL_WEIGHT = 23


def x_weighted_length(text: str) -> int:
    """X（twitter-text v3）の重み付き文字数。ASCII 等は 1、日本語や絵文字は 2。"""
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            n += 1
        else:
            n += 2
    return n


class Usage:
    def __init__(self) -> None:
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, u) -> None:
        self.input += u.input_tokens or 0
        self.output += u.output_tokens or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float:
        return (self.input * PRICE_INPUT + self.output * PRICE_OUTPUT
                + self.cache_read * PRICE_CACHE_READ + self.cache_write * PRICE_CACHE_WRITE) / 1e6


def call_json(client, usage: Usage, system: str, user: str, schema: dict, effort: str,
              model: str | None = None) -> dict | None:
    """構造化出力で 1 回呼び出し、JSON を返す。拒否や形式不備のときは None。
    model を指定すると別のモデル（軽いモデルなど）で呼ぶ。"""
    import anthropic

    try:
        resp = client.beta.messages.create(
            model=model or MODEL,
            max_tokens=16000,
            # 安全分類で拒否されたときに API 側で別モデルに切り替えて続行する
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.RateLimitError as e:
        print(f"::warning::Claude API レート制限: {e.message}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"::warning::Claude API エラー ({e.status_code}): {e.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"::warning::Claude API 接続エラー: {e}", file=sys.stderr)
        return None

    usage.add(resp.usage)
    if resp.stop_reason == "refusal":
        print("::warning::Claude が応答を拒否しました", file=sys.stderr)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"::warning::JSON 解析に失敗 (stop_reason={resp.stop_reason})", file=sys.stderr)
        return None


def domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def fetch_article_text(url: str) -> str:
    """記事本文を段落単位で抽出する（失敗時は空文字）。"""
    try:
        resp = requests.get(url, headers={"User-Agent": RESOLVE_USER_AGENT}, timeout=10)
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        paras = [" ".join(p.get_text(" ", strip=True).split()) for p in root.find_all("p")]
        text = "\n".join(p for p in paras if len(p) >= 20)
        return text[:ARTICLE_BODY_CHARS]
    except Exception:
        return ""


# ── 展覧会の公式ページ（投稿の URL を公式ページにして、X のリンクカードに公式の OG 画像を出す） ──
# 画像そのものは保存・添付しない（主催者の権利があるため）。公式ページの OG 画像を X がカードに出すだけ
OFFICIAL_CATEGORIES = {"展覧会"}
LINK_SKIP_DOMAINS = (
    "x.com", "twitter.com", "facebook.com", "instagram.com", "line.me", "youtube.com", "youtu.be",
    "google.com", "google.co.jp", "amazon.co.jp", "amazon.com", "rakuten.co.jp", "yahoo.co.jp",
    "hatena.ne.jp", "note.com", "tiktok.com", "apple.com", "goo.gl", "bit.ly",
)

OFFICIAL_SYSTEM = """ニュース記事の中にあるリンクの一覧から、記事で紹介している展覧会の「公式ページ」
（主催の博物館・美術館・寺院・自治体などの展覧会紹介ページ）の URL を 1 つ選んでください。
展覧会そのもののページが無ければ、主催館の公式サイトのトップでもよい。
ニュースサイト・チケット販売・SNS・広告・関係ない施設のページは選ばない。該当が無ければ空文字。"""

OFFICIAL_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "required": ["url"],
    "additionalProperties": False,
}


def article_links(url: str) -> list[tuple[str, str]]:
    """記事本文中の、外部サイトへのリンク (URL, 文字列)。"""
    try:
        resp = requests.get(url, headers={"User-Agent": RESOLVE_USER_AGENT}, timeout=10)
        soup = BeautifulSoup(resp.content, "html.parser")
    except Exception:
        return []
    own = domain(resp.url)
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    out, seen = [], set()
    for a in root.find_all("a", href=True):
        href = urldefrag(urljoin(resp.url, a["href"]))[0]
        d = domain(href)
        if not href.startswith("http") or not d or d == own or href in seen:
            continue
        if any(d == x or d.endswith("." + x) for x in LINK_SKIP_DOMAINS):
            continue
        seen.add(href)
        out.append((href, " ".join(a.get_text(" ", strip=True).split())[:60]))
    return out[:25]


SEARCH_MODEL = "claude-haiku-5-5"   # 公式ページ探しは軽いモデルで十分（検索結果の読み込みが費用の大半）
SEARCH_OFFICIAL_PROMPT = """次の展覧会の公式ページを Web 検索で探し、その URL だけを 1 行で答えてください。
優先順: ①主催の博物館・美術館・寺院の、この展覧会の紹介ページ ②主催者や自治体の公式発表
（記者発表・お知らせ）のページ。サイトのトップページ・催しの一覧ページ・ニュースサイト・
チケット販売・SNS・まとめサイトは不可。見つからなければ NONE とだけ答えてください。

記事タイトル: {title}
展覧会: {event} / 会場: {place}"""


def search_official_page(client, usage: "Usage", item: dict) -> str:
    """記事にリンクが無いとき、Claude の Web 検索で公式ページを探す（1 件 数セント）。"""
    import re

    ev = (item.get("ai") or {}).get("event") or {}
    prompt = SEARCH_OFFICIAL_PROMPT.format(title=item.get("title", ""), event=ev.get("event_name", ""),
                                           place=ev.get("place", ""))
    for tool_type in ("web_search_20260209", "web_search_20250305"):
        try:
            resp = client.messages.create(
                model=SEARCH_MODEL, max_tokens=1000,
                tools=[{"type": tool_type, "name": "web_search", "max_uses": 2}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            print(f"::warning::公式ページの検索に失敗（{tool_type}）: {type(e).__name__}", file=sys.stderr)
            continue
        usage.add(resp.usage)
        text = " ".join(b.text for b in resp.content if b.type == "text")
        if os.environ.get("DEBUG_OFFICIAL"):
            print(f"   [検索の回答] {text[:300]}")
        m = re.findall(r"https?://[^\s)」）>\]]+", text)
        return m[-1].rstrip(".,。、") if m else ""
    return ""


def find_official_page(client, usage: "Usage", item: dict) -> tuple[str, str]:
    """展覧会の公式ページの (URL, OG 画像)。見つからなければ空。
    まず記事中のリンクから選び、無ければ Web 検索で探す。"""
    links = article_links(item["url"])
    url = ""
    if links:
        ev = (item.get("ai") or {}).get("event") or {}
        user = (f"記事タイトル: {item.get('title', '')}\n"
                f"展覧会: {ev.get('event_name', '')} / 会場: {ev.get('place', '')}\n\n"
                "リンク一覧:\n" + "\n".join(f"- {u}  （{t}）" for u, t in links))
        res = call_json(client, usage, OFFICIAL_SYSTEM, user, OFFICIAL_SCHEMA, effort="low")
        url = ((res or {}).get("url") or "").strip()
        if url not in {u for u, _ in links}:
            url = ""
    if not url:
        url = search_official_page(client, usage, item)
    d = domain(url)
    if not url or any(d == x or d.endswith("." + x) for x in LINK_SKIP_DOMAINS) or d == domain(item["url"]):
        return "", ""
    try:
        r = requests.get(url, headers={"User-Agent": RESOLVE_USER_AGENT}, timeout=10)
        image = extract_og_image(r.content) if r.status_code == 200 else ""
    except Exception:
        image = ""
    # OG 画像が無いページだとリンクカードに画像が出ず、記事の URL より見劣りするので使わない
    return (url, image) if image else ("", "")


def hours_since(iso: str | None) -> int | None:
    """配信からの経過時間（時間単位、不明なら None）。"""
    try:
        return int((datetime.now(JST) - datetime.fromisoformat(iso)).total_seconds() // 3600)
    except (TypeError, ValueError):
        return None


# 記者発表・プレスリリースを出す発表元（新聞より先に出る一次情報）
PRIMARY_DOMAINS = (
    "prtimes.jp", "atpress.ne.jp", "digitalpr.jp", "kyodonewsprwire.jp", "value-press.com",
    "bunka.go.jp", "emuseum.nich.go.jp", "tnm.jp", "narahaku.go.jp", "kyohaku.go.jp", "kyuhaku.jp",
)


def article_kind(it: dict) -> str:
    if it.get("source") == "official_site":
        return "公式サイトの新着"
    d = domain(it.get("url", ""))
    if any(d == p or d.endswith("." + p) for p in PRIMARY_DOMAINS) or "記者発表" in it.get("title", ""):
        return "発表元・一次情報"
    return "ニュース記事"


def score_items(client, usage: Usage, items: list[dict], recent_candidates: list[dict]) -> int:
    """items を SCORE_BATCH_SIZE 件ずつ採点し、各記事の "ai" に書き込む。採点件数を返す。"""
    known = list(recent_candidates)  # 重複判定用。採点が進むたびに新しい候補を足していく
    scored = 0
    for i in range(0, len(items), SCORE_BATCH_SIZE):
        batch = items[i:i + SCORE_BATCH_SIZE]
        # 前のまとまりで候補になった記事も渡す（まとまりをまたいだ重複を見逃さないため）
        context = "\n".join(f"- {c['title']}" for c in known) or "（なし）"
        articles = [
            {
                "id": it["id"],
                "title": it.get("title", ""),
                "description": it.get("description", ""),
                "source": domain(it.get("url", "")),
                "kind": article_kind(it),
                "published": (it.get("published_at") or "")[:16],
                "hours_since_published": hours_since(it.get("published_at")),
            }
            for it in batch
        ]
        user = (
            f"## 今日\n{datetime.now(JST):%Y年%m月%d日}（{'月火水木金土日'[datetime.now(JST).weekday()]}）"
            f"{datetime.now(JST):%H}時\n\n"
            "## 配信済み・配信候補の記事（直近）\n" + context
            + "\n\n## 採点する記事\n" + json.dumps(articles, ensure_ascii=False, indent=1)
        )
        data = call_json(client, usage, SCORE_SYSTEM, user, SCORE_SCHEMA, effort="low")
        if not data:
            continue
        by_id = {it["id"]: it for it in batch}
        now = datetime.now(JST).isoformat(timespec="seconds")
        for r in data.get("results", []):
            it = by_id.get(r.get("id"))
            if not it:
                continue
            # 再採点のときも、作成済みの投稿文と通知済みフラグは引き継ぐ（再通知・再作成を防ぐ）
            kept = {k: v for k, v in it.get("ai", {}).items()
                    if k in ("post_text", "post_format", "notified", "post_attempts")}
            score = max(0, min(100, int(r["score"])))
            hours = hours_since(it.get("published_at"))
            if (r.get("is_buddha_designation") and not r["duplicate"] and hours is not None
                    and hours <= NEW_DESIGNATION_FLOOR_HOURS and score < NEW_DESIGNATION_FLOOR):
                score = NEW_DESIGNATION_FLOOR  # AI の点数のぶれで、仏像の新指定が下がりすぎないようにする
            it["ai"] = {
                **kept,
                "score": score,
                "category": r["category"],
                "duplicate": bool(r["duplicate"]),
                "reason": r["reason"],
                "event": {k: r[k].strip() for k in ("event_name", "place", "start_date", "end_date")},
                "buddha_designation": bool(r.get("is_buddha_designation")),
                "model": MODEL,
                "scored_at": now,
                "score_version": SCORE_VERSION,
            }
            scored += 1
            if it["ai"]["score"] >= CANDIDATE_THRESHOLD and not it["ai"]["duplicate"]:
                known.append(it)
    return scored


def write_post(client, usage: Usage, item: dict, header: str = NEWS_POST_HEADER,
               extra: str = "", recent_posts: list[str] | None = None) -> str | None:
    """記事を読んで投稿の文章部分を作る。extra は追加の指示（リマインド時の公開日など）。
    recent_posts は直近に作った投稿文（書き出し・締めが似ないようにするため）。"""
    body = fetch_article_text(item["url"]) or item.get("description", "")
    user = (
        f"今日: {datetime.now(JST):%Y-%m-%d %H時}\n"
        f"記事タイトル: {item.get('title', '')}\n"
        f"配信元: {domain(item['url'])}\n"
        f"公開日時: {(item.get('published_at') or '')[:16]}\n\n"
        f"記事本文:\n{body or '（本文を取得できませんでした。タイトルの範囲で書いてください）'}"
        + (f"\n\n## 追加の指示\n{extra}" if extra else "")
        + ("\n\n## 直近の投稿文（これと書き出し・締めが似ないように）\n"
           + "\n".join(f"- {t.replace(chr(10), ' ')}" for t in recent_posts[:6])
           if recent_posts else "")
    )
    # 後ろに付く「【仏像速報】タイトル」と URL の分を差し引いた残りが文章に使える
    fixed = x_weighted_length(compose_news_post("", item.get("title", ""), header)) + 1 + X_URL_WEIGHT
    budget_chars = max(40, (X_LIMIT - fixed) // 2 - 3)
    user += f"\n\n文章は全角 {budget_chars} 字以内（ハッシュタグ込み）で書いてください。"
    for _ in range(2):
        data = call_json(client, usage, POST_SYSTEM, user, POST_SCHEMA, effort="medium")
        if not data:
            return None
        text = data["post_text"].strip()
        if not text:
            user += "\n\n（前回の案は空でした。本文が短くても、タイトルと記事の範囲で必ず 1〜2 文書いてください）"
            continue
        total = x_weighted_length(compose_news_post(text, item.get("title", ""), header)) + 1 + X_URL_WEIGHT
        if total <= X_LIMIT:
            return text
        budget_chars = max(30, budget_chars - 20)
        user += (
            f"\n\n（前回の案は X の文字数制限を超えました: {total}/{X_LIMIT}。"
            f"全角 {budget_chars} 字以内で書き直してください）"
        )
    return None


def notify(topic: str, item: dict) -> None:
    ai = item["ai"]
    body = (compose_news_post(ai["post_text"], item.get("title", ""), ai.get("post_header", NEWS_POST_HEADER))
            if ai.get("post_text") else item.get("title", ""))
    # 通知の「Xに投稿」から、管理画面を開かずに投稿文入りの X を開けるようにする
    link = ai.get("official_url") or item.get("url", "")
    intent = "https://x.com/intent/post?" + urlencode({"text": body, "url": link})
    hour = datetime.now(JST).hour
    note = "" if 9 <= hour < 21 else "\n\n（投稿は9時以降がおすすめ。反応が多いのは9〜17時）"
    try:
        requests.post(
            "https://ntfy.sh/",
            json={
                "topic": topic,
                "title": f"⭐ 配信候補 {ai['score']}点（{ai['category']}）",
                "message": body + note,
                "click": PWA_URL,
                "actions": [
                    {"action": "view", "label": "Xに投稿", "url": intent},
                    {"action": "view", "label": "管理画面", "url": PWA_URL},
                ],
                "tags": ["star"],
                "priority": 5 if ai["score"] >= 90 else 4,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"::warning::通知の送信に失敗: {e}", file=sys.stderr)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("::warning::ANTHROPIC_API_KEY が未設定のため AI 採点をスキップ", file=sys.stderr)
        return 0
    import anthropic

    client = anthropic.Anthropic()
    usage = Usage()
    data = load_news_data()
    now = datetime.now(JST)
    window = now - timedelta(hours=SCORE_WINDOW_HOURS)

    def recent(it: dict) -> bool:
        try:
            return datetime.fromisoformat(it.get("fetched_at", "")) >= window
        except ValueError:
            return False

    news = [it for it in data["items"] if it.get("source") in TARGET_SOURCES and recent(it)]
    # 新しい記事から採点する（採点基準の更新で採点し直す記事が多いときも、新着を先に）
    unscored = sorted(
        (it for it in news
         if it.get("ai", {}).get("score_version", 1 if "ai" in it else 0) < SCORE_VERSION),
        key=lambda it: it.get("fetched_at", ""), reverse=True,
    )[:MAX_SCORE_PER_RUN]
    # 重複判定の材料（配信済み・配信候補）。いまから採点する記事自体は含めない
    # （含めると、再採点時に記事が「自分自身の重複」と判定されてしまう）
    scoring_ids = {it["id"] for it in unscored}
    recent_candidates = [
        it for it in news
        if it["id"] not in scoring_ids
        and it.get("ai", {}).get("score", 0) >= CANDIDATE_THRESHOLD and not it["ai"].get("duplicate")
    ]
    print(f"採点対象: {len(unscored)}件（直近{SCORE_WINDOW_HOURS}時間のニュース {len(news)}件中）")

    scored = score_items(client, usage, unscored, recent_candidates) if unscored else 0

    # 投稿文が未作成の配信候補（新しい順）
    candidates = [
        it for it in news
        if it.get("ai", {}).get("score", 0) >= CANDIDATE_THRESHOLD
        and not it["ai"].get("duplicate")
        # 投稿文が無いもの、または古い文体（POST_FORMAT 未満）のものを作り直す
        and (it["ai"].get("post_format", 0) < POST_FORMAT
             # 以前に作成に失敗して空のままのものも、最大 MAX_POST_ATTEMPTS 回までやり直す
             or (not it["ai"].get("post_text") and it["ai"].get("post_attempts", 1) < MAX_POST_ATTEMPTS))
    ][:MAX_POSTS_PER_RUN]
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    written = 0
    # 直近に作った投稿文（新しい順）。書き出し・締めの型が続かないように渡す
    recent_posts = [
        x["ai"]["post_text"] for x in sorted(
            (x for x in data["items"]
             if x.get("ai", {}).get("post_text") and x["ai"].get("post_format", 0) >= POST_FORMAT),
            key=lambda x: x.get("fetched_at", ""), reverse=True)
    ]
    for it in candidates:
        if it["ai"].get("category") in OFFICIAL_CATEGORIES and "official_url" not in it["ai"]:
            if it.get("source") == "official_site":
                off_url, off_img = it["url"], it.get("image_url", "")
            else:
                off_url, off_img = find_official_page(client, usage, it)
            it["ai"]["official_url"], it["ai"]["official_image"] = off_url, off_img
            if off_url:
                print(f"   公式ページ: {off_url}")
        text = write_post(client, usage, it, recent_posts=recent_posts)
        it["ai"]["post_text"] = text or ""
        it["ai"]["post_attempts"] = it["ai"].get("post_attempts", 0) + 1
        it["ai"]["post_format"] = POST_FORMAT
        if text:
            recent_posts.insert(0, text)
            written += 1
            print(f"⭐ {it['ai']['score']}点 {it['title'][:40]}\n   {text}")
        if topic and not it["ai"].get("notified"):
            notify(topic, it)
            it["ai"]["notified"] = True

    save_news_data(data)
    noise = sum(1 for it in news if it.get("ai", {}).get("score", 100) < NOISE_THRESHOLD)
    print(
        f"採点 {scored}件 / 配信候補の投稿文 {written}件 / ノイズ判定（直近）{noise}件\n"
        f"トークン: 入力 {usage.input:,} / 出力 {usage.output:,} / "
        f"キャッシュ読込 {usage.cache_read:,} / キャッシュ書込 {usage.cache_write:,} "
        f"→ 約 ${usage.cost():.4f}"
    )
    print(f"::notice::AI採点 {scored}件・投稿文 {written}件・費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
