"""ロゴ入りファビコン／アプリアイコンを生成する（ローカル実行用）。

依存:
    pip install Pillow

使い方:
    # フォント TTF のパスを引数 / 環境変数で指定。デフォルトは butsuzo_app/assets/font.ttf を探す。
    python tools/generate_logo.py
    python tools/generate_logo.py /path/to/font.ttf

生成物:
    docs/favicon.ico
    docs/icons/favicon-16x16.png
    docs/icons/favicon-32x32.png
    docs/icons/apple-touch-icon.png    (180x180)
    docs/icons/icon-192.png            (PWA)
    docs/icons/icon-512.png            (PWA)
    docs/icons/maskable-512.png        (Android maskable / 角丸無し)

ロゴ仕様:
    - 背景:   #00AE95（ブランドカラー）
    - 装飾:   内側に薄リング
    - 文字:   「仏」（白）／フォントは指定 TTF
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
ICONS_DIR = DOCS_DIR / "icons"

BG = (0, 174, 149)          # #00AE95
FG = (255, 255, 255)
RING = (0, 142, 120)

DEFAULT_FONT_CANDIDATES = [
    ROOT.parent / "仏像リンクブログ記事作成" / "butsuzo_app" / "assets" / "font.ttf",
    Path.home() / "仏像リンクブログ記事作成" / "butsuzo_app" / "assets" / "font.ttf",
]


def find_font(path_arg: str | None) -> Path:
    if path_arg:
        p = Path(path_arg).expanduser()
        if not p.exists():
            raise SystemExit(f"フォントが見つかりません: {p}")
        return p
    env = os.environ.get("BUTSUZO_FONT")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    for c in DEFAULT_FONT_CANDIDATES:
        if c.exists():
            return c
    raise SystemExit(
        "フォントファイルを特定できません。引数で TTF パスを指定するか、"
        "BUTSUZO_FONT 環境変数を設定してください。"
    )


def make_icon(size: int, font_path: Path, text: str = "仏", rounded: bool = True) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        radius = int(size * 0.22)
        draw.rounded_rectangle([(0, 0), (size, size)], radius=radius, fill=BG)
    else:
        draw.rectangle([(0, 0), (size, size)], fill=BG)

    pad = int(size * 0.09)
    draw.rounded_rectangle(
        [(pad, pad), (size - pad, size - pad)],
        radius=int(size * 0.16),
        outline=RING,
        width=max(2, int(size * 0.012)),
    )

    font = ImageFont.truetype(str(font_path), int(size * 0.68))
    cx, cy = size / 2, size / 2 - size * 0.02  # 漢字の視覚中央
    draw.text((cx, cy), text, font=font, fill=FG, anchor="mm")
    return img


def main(argv: list[str]) -> int:
    font_path = find_font(argv[1] if len(argv) > 1 else None)
    print(f"フォント: {font_path}")

    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    specs = [
        ("icon-192.png", 192, True),
        ("icon-512.png", 512, True),
        ("apple-touch-icon.png", 180, True),
        ("favicon-32x32.png", 32, True),
        ("favicon-16x16.png", 16, True),
        ("maskable-512.png", 512, False),
    ]
    for name, sz, rounded in specs:
        img = make_icon(sz, font_path, rounded=rounded)
        out = ICONS_DIR / name
        img.save(out, format="PNG", optimize=True)
        print(f"  → {out.relative_to(ROOT)} ({sz}x{sz}, {out.stat().st_size:,} bytes)")

    ico_sizes = [16, 32, 48]
    ico_imgs = [make_icon(s, font_path, rounded=True).convert("RGBA") for s in ico_sizes]
    ico = DOCS_DIR / "favicon.ico"
    ico_imgs[0].save(
        ico, format="ICO",
        sizes=[(s, s) for s in ico_sizes], append_images=ico_imgs[1:],
    )
    print(f"  → {ico.relative_to(ROOT)} ({ico.stat().st_size:,} bytes)")
    print("✅ 完了")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
