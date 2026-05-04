"""うおちゃんパーツのスタブ PNG 生成スクリプト

12 パーツを 800×800 透過 PNG として `assets/uochan/` 以下に書き出す。
本番イラストが届くまでの開発用ダミー。各 PNG のキャンバス内位置は
本番アーティスト向けの「位置参考」を兼ねる。

使い方:
  python3 generate_stubs.py

魚は **左向き** (head が左、tail が右、手足が体の上下から生える) を想定。
"""

from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 800
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "uochan"
ASSETS.mkdir(parents=True, exist_ok=True)


def new_canvas():
    return Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))


def save(img, name):
    path = ASSETS / f"{name}.png"
    img.save(path, "PNG")
    print(f"  -> {path.relative_to(ASSETS.parent.parent)}")


def draw_body():
    """胴体: 中央に淡いブルーの大きな楕円。縦スライス変形の母体になる"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    d.ellipse((100, 280, 700, 520), fill=(140, 180, 210, 255), outline=(60, 100, 140, 255), width=4)
    return img


def draw_head():
    """頭: 体の左前側に重なる、やや明るい楕円"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    d.ellipse((110, 270, 380, 480), fill=(170, 200, 220, 255), outline=(60, 100, 140, 255), width=4)
    return img


def draw_mouth(state):
    """口: 頭の左前先端あたり。closed/half/open の 3 フレーム"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = 150, 420
    if state == "closed":
        d.line((cx - 18, cy, cx + 18, cy), fill=(30, 30, 30, 255), width=4)
    elif state == "half":
        d.ellipse((cx - 18, cy - 8, cx + 18, cy + 8), fill=(30, 30, 30, 255))
    elif state == "open":
        d.ellipse((cx - 22, cy - 18, cx + 22, cy + 18), fill=(30, 30, 30, 255))
        d.ellipse((cx - 14, cy - 10, cx + 14, cy + 10), fill=(180, 80, 80, 255))
    return img


def draw_eye(state):
    """目: 頭部上側。open/half/closed の 3 フレーム"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    cx, cy = 270, 340
    # 白目ベース
    d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(255, 255, 255, 255), outline=(60, 60, 60, 255), width=2)
    if state == "open":
        d.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=(20, 20, 20, 255))
        d.ellipse((cx - 5, cy - 7, cx + 1, cy - 1), fill=(255, 255, 255, 255))  # ハイライト
    elif state == "half":
        d.rectangle((cx - 22, cy - 22, cx + 22, cy - 5), fill=(0, 0, 0, 0))  # 透明（消す）
        d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(0, 0, 0, 0))   # 一旦消す
        # 半開き: 下半分だけ瞳が見える
        d.chord((cx - 22, cy - 22, cx + 22, cy + 22), 0, 180, fill=(255, 255, 255, 255), outline=(60, 60, 60, 255), width=2)
        d.chord((cx - 12, cy - 12, cx + 12, cy + 12), 0, 180, fill=(20, 20, 20, 255))
    elif state == "closed":
        # 上書きで線一本
        d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(0, 0, 0, 0))   # 一旦消す
        d.line((cx - 20, cy, cx + 20, cy), fill=(60, 60, 60, 255), width=4)
    return img


def draw_arm(side):
    """腕: 体の側面から伸びる細長い楕円。中立 (やや下向き) ポーズ。
    side='l' は手前側 (画面下、z 順で前)、'r' は奥側 (画面上、z 順で奥)。
    """
    img = new_canvas()
    d = ImageDraw.Draw(img)
    if side == "l":
        # 肩 pivot: (380, 520) → 下に伸びる中立ポーズ
        d.ellipse((360, 510, 410, 660), fill=(150, 190, 215, 255), outline=(60, 100, 140, 255), width=3)
        # 手
        d.ellipse((355, 645, 415, 700), fill=(170, 200, 220, 255), outline=(60, 100, 140, 255), width=3)
    else:  # 'r'
        # 肩 pivot: (380, 280) → 上に伸びる中立ポーズ
        d.ellipse((360, 140, 410, 290), fill=(120, 160, 195, 255), outline=(60, 100, 140, 255), width=3)
        d.ellipse((355, 100, 415, 155), fill=(150, 190, 215, 255), outline=(60, 100, 140, 255), width=3)
    return img


def draw_leg(side):
    """足: 体の後方側面から伸びる細長い楕円。立ち姿勢の中立ポーズ。"""
    img = new_canvas()
    d = ImageDraw.Draw(img)
    if side == "l":
        # 股 pivot: (560, 500)
        d.ellipse((545, 495, 595, 670), fill=(150, 190, 215, 255), outline=(60, 100, 140, 255), width=3)
        # 足
        d.ellipse((535, 660, 605, 705), fill=(170, 200, 220, 255), outline=(60, 100, 140, 255), width=3)
    else:  # 'r'
        # 股 pivot: (560, 300)
        d.ellipse((545, 130, 595, 305), fill=(120, 160, 195, 255), outline=(60, 100, 140, 255), width=3)
        d.ellipse((535, 95, 605, 140), fill=(150, 190, 215, 255), outline=(60, 100, 140, 255), width=3)
    return img


def main():
    print("Generating stub parts at", ASSETS)
    save(draw_body(), "body")
    save(draw_head(), "head")
    save(draw_mouth("closed"), "mouth_closed")
    save(draw_mouth("half"), "mouth_half")
    save(draw_mouth("open"), "mouth_open")
    save(draw_eye("open"), "eye_open")
    save(draw_eye("half"), "eye_half")
    save(draw_eye("closed"), "eye_closed")
    save(draw_arm("l"), "arm_l")
    save(draw_arm("r"), "arm_r")
    save(draw_leg("l"), "leg_l")
    save(draw_leg("r"), "leg_r")
    print("Done. 12 parts generated.")


if __name__ == "__main__":
    main()
