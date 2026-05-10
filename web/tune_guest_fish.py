#!/usr/bin/env python3
"""ゲスト魚トリミング (背景除去 + crop) のチューニング用 CLI。

realtime_loop.py を立ち上げずに、撮影 → 背景除去 → トリミング を単体で回して
しきい値 (v_thresh / s_thresh) を詰めるためのツール。pi-main 上で実行する。

依存 (numpy / Pillow) は fish_ai_realtime の venv に入っているので、そちらの
python で起動する:

  ~/Documents/fish_ai_realtime/.venv/bin/python ~/Documents/aquarium/web/tune_guest_fish.py --help

典型ワークフロー:
  1. ブースに代表的な絵を置いて撮る (本番と同じ rpicam-still 引数 + --rotation 180、
     撮影時はブース LED を自動点灯 → ウォームアップ → 撮影 → 消灯。--no-led で抑止):
       ... tune_guest_fish.py --capture
     → tune_out/capture_<ts>.jpg と tune_out/latest.jpg に保存
  2. しきい値スイープ → モンタージュ画像で見比べる:
       ... tune_guest_fish.py --sweep
     → tune_out/sweep.png  (Pi デスクトップで xdg-open するか、ブラウザで
        http://raspberrypi.local:8080/tune_out/sweep.png を開く)
  3. 単発で詳しく見る (マスク可視化付き):
       ... tune_guest_fish.py --v-thresh 210 --s-thresh 35
     → tune_out/result.png (透明部分をチェッカー柄で表示) と
        tune_out/mask_overlay.png (除去されたピクセルを赤、bbox を緑枠で表示)
  4. 良い値が決まったら反映:
       - 即時 & realtime_loop も含めて効かせる: 起動前に環境変数
           GUEST_FISH_V_THRESH=210 GUEST_FISH_S_THRESH=35
       - 恒久化: web/config.json の background_removal.value_threshold 等を編集
"""

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guest_fish_pipeline import compute_bg_mask, remove_white_background, resolve_params  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_OUT_DIR = REPO_ROOT / "tune_out"

# realtime_loop.py の _capture_and_register_guest_fish と揃えた撮影設定
CAPTURE_WIDTH = 1640
CAPTURE_HEIGHT = 1232

# 撮影ブースの LED リレー (realtime_loop.py の Phase 3 設定と同じ env を尊重)
RELAY_GPIO = int(os.environ.get("PHASE3_RELAY_GPIO", "18"))            # BCM
RELAY_ACTIVE_LOW = os.environ.get("PHASE3_RELAY_ACTIVE_LOW", "false").lower() == "true"
LED_WARMUP_DEFAULT = float(os.environ.get("PHASE3_LIGHT_WARMUP_DELAY", "1.5"))  # 秒


def _parse_int_list(s: str) -> list[int]:
    return [int(x) for x in s.replace(" ", "").split(",") if x != ""]


def _set_led(on: bool) -> bool:
    """ブース LED を pinctrl で ON/OFF する。成功したら True。

    realtime_loop.py が走行中だと gpiod 経由でリレーを保持しているので衝突
    する。チューニング時 (realtime_loop は停止しているはず) 専用。
    """
    drive = "dh" if (on != RELAY_ACTIVE_LOW) else "dl"
    try:
        subprocess.run(["pinctrl", "set", str(RELAY_GPIO), "op", drive], check=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[LED] pinctrl 失敗 (LED 制御をスキップ): {e}", file=sys.stderr)
        return False


def _realtime_loop_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "realtime_loop.py"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def capture(out_dir: Path, *, camera: int, ev: float, rotation: int, extra: str,
            use_led: bool, warmup: float) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = out_dir / f"capture_{ts}.jpg"
    cmd = [
        "rpicam-still",
        "--camera", str(camera),
        "--rotation", str(rotation),
        "-t", "200",
        "--width", str(CAPTURE_WIDTH),
        "--height", str(CAPTURE_HEIGHT),
        "--ev", str(ev),
        "-o", str(dst),
        "--immediate",
        "-n",
    ]
    if extra:
        cmd += shlex.split(extra)

    led_on = False
    try:
        if use_led:
            if _realtime_loop_running():
                print("[LED] 警告: realtime_loop.py が走行中です。リレーが衝突する可能性があります。", file=sys.stderr)
            led_on = _set_led(True)
            if led_on:
                print(f"[LED] ON (BCM{RELAY_GPIO})、ウォームアップ {warmup}s")
                time.sleep(warmup)
        print("==> " + " ".join(shlex.quote(c) for c in cmd))
        subprocess.run(cmd, check=True, timeout=20)
    finally:
        if led_on:
            _set_led(False)
            print(f"[LED] OFF (BCM{RELAY_GPIO})")

    latest = out_dir / "latest.jpg"
    latest.write_bytes(dst.read_bytes())
    print(f"==> saved {dst}  (も {latest})")
    return latest


def _checkerboard(size: tuple[int, int], cell: int = 12) -> Image.Image:
    w, h = size
    bg = Image.new("RGBA", size, (210, 210, 210, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, h, cell):
        for x in range(0, w, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(245, 245, 245, 255))
    return bg


def _on_checker(im: Image.Image) -> Image.Image:
    base = _checkerboard(im.size)
    base.alpha_composite(im.convert("RGBA"))
    return base


def _font(size: int = 14):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _fit(im: Image.Image, box: tuple[int, int]) -> Image.Image:
    bw, bh = box
    w, h = im.size
    scale = min(bw / w, bh / h, 1.0)
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return im


def single(src: Image.Image, out_dir: Path, v_thresh, s_thresh, long_edge) -> None:
    params = resolve_params(v_thresh, s_thresh, long_edge)
    print(f"==> 使用パラメータ: {params}")
    src_rgb = np.array(src.convert("RGB"))

    result = remove_white_background(src, v_thresh=params["v_thresh"], s_thresh=params["s_thresh"], long_edge=params["long_edge"])
    res_path = out_dir / "result.png"
    result.save(res_path)
    _on_checker(result).convert("RGB").save(out_dir / "result_on_checker.png")
    print(f"==> 結果 {result.size[0]}x{result.size[1]} -> {res_path} (+ result_on_checker.png)")

    # マスク可視化: 除去されるピクセルを赤、トリミング bbox を緑枠で重ねる
    mask = compute_bg_mask(src_rgb, v_thresh=params["v_thresh"], s_thresh=params["s_thresh"])
    overlay = src_rgb.copy()
    red = np.array([255, 40, 40], dtype=np.float32)
    overlay[mask] = (overlay[mask].astype(np.float32) * 0.35 + red * 0.65).astype(np.uint8)
    ov = Image.fromarray(overlay, "RGB")
    keep = ~mask
    if keep.any():
        ys, xs = np.where(keep)
        d = ImageDraw.Draw(ov)
        d.rectangle([int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())], outline=(0, 230, 0), width=4)
    ov_path = out_dir / "mask_overlay.png"
    ov.save(ov_path)
    print(f"==> マスク可視化 (赤=除去 / 緑枠=crop 範囲) -> {ov_path}")


def sweep(src: Image.Image, out_dir: Path, v_list: list[int], s_list: list[int], long_edge) -> None:
    le = resolve_params(None, None, long_edge)["long_edge"]
    tile = 300
    label_h = 22
    pad = 8
    cell_w = tile + pad
    cell_h = tile + label_h + pad
    cols, rows = len(v_list), len(s_list)
    sheet = Image.new("RGB", (cols * cell_w + pad, rows * cell_h + pad), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    font = _font(13)

    for ri, s in enumerate(s_list):
        for ci, v in enumerate(v_list):
            res = remove_white_background(src, v_thresh=v, s_thresh=s, long_edge=le)
            thumb = _fit(_on_checker(res).convert("RGB"), (tile, tile))
            x0 = pad + ci * cell_w
            y0 = pad + ri * cell_h
            tw, th = thumb.size
            tx = x0 + (tile - tw) // 2
            ty = y0 + label_h + (tile - th) // 2
            sheet.paste(thumb, (tx, ty))
            d.rectangle([tx, ty, tx + tw - 1, ty + th - 1], outline=(170, 170, 170))
            d.text((x0 + 2, y0 + 3), f"v={v} s={s}  {res.size[0]}x{res.size[1]}", fill=(0, 0, 0), font=font)
            print(f"  v={v:>3} s={s:>3} -> {res.size[0]}x{res.size[1]}")

    sheet_path = out_dir / "sweep.png"
    sheet.save(sheet_path)
    print(f"==> モンタージュ -> {sheet_path}")
    print("    Pi デスクトップで:  xdg-open " + str(sheet_path))
    print("    ブラウザで:        http://raspberrypi.local:8080/tune_out/sweep.png")


def main() -> int:
    ap = argparse.ArgumentParser(description="ゲスト魚トリミングのチューニング CLI", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--capture", action="store_true", help="rpicam-still で 1 枚撮って tune_out/latest.jpg に保存してから処理する")
    ap.add_argument("--camera", type=int, default=0, help="撮影に使うカメラ index (既定 0 = IMX219)")
    ap.add_argument("--ev", type=float, default=0.5, help="撮影時の露出補正 EV (既定 0.5、本番と同値)")
    ap.add_argument("--rotation", type=int, default=180, help="撮影時の回転角 (既定 180、本番と同値)")
    ap.add_argument("--rpicam-extra", default="", help="rpicam-still に渡す追加引数 (例: '--awb tungsten --shutter 8000')")
    ap.add_argument("--no-led", action="store_true", help="撮影時にブース LED を点灯しない (既定は点灯する)")
    ap.add_argument("--led-warmup", type=float, default=LED_WARMUP_DEFAULT, help=f"LED 点灯から撮影までの待ち秒数 (既定 {LED_WARMUP_DEFAULT})")
    ap.add_argument("--input", type=Path, help="処理する画像 (既定: tune_out/latest.jpg)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"出力先 (既定 {DEFAULT_OUT_DIR})")
    ap.add_argument("--v-thresh", type=int, help="単発実行時の value 閾値 (省略時は env/config/既定値)")
    ap.add_argument("--s-thresh", type=int, help="単発実行時の saturation 閾値 (省略時は env/config/既定値)")
    ap.add_argument("--long-edge", type=int, help="出力長辺 px (省略時は env/config/既定値)")
    ap.add_argument("--sweep", action="store_true", help="v×s グリッドを総当たりして sweep.png を出す")
    ap.add_argument("--v-list", type=_parse_int_list, default=_parse_int_list("180,190,200,210,220"), help="--sweep の value 閾値リスト (カンマ区切り)")
    ap.add_argument("--s-list", type=_parse_int_list, default=_parse_int_list("20,30,40,50"), help="--sweep の saturation 閾値リスト (カンマ区切り)")
    args = ap.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.capture:
        src_path = capture(out_dir, camera=args.camera, ev=args.ev, rotation=args.rotation,
                           extra=args.rpicam_extra, use_led=not args.no_led, warmup=args.led_warmup)
    else:
        src_path = args.input or (out_dir / "latest.jpg")
        if not src_path.exists():
            print(f"入力画像が無い: {src_path}\n  --capture で撮るか、--input で既存画像を指定してください。", file=sys.stderr)
            return 1

    print(f"==> 入力: {src_path}")
    with Image.open(src_path) as im:
        im.load()
        src = im.copy()

    if args.sweep:
        sweep(src, out_dir, args.v_list, args.s_list, args.long_edge)
    else:
        single(src, out_dir, args.v_thresh, args.s_thresh, args.long_edge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
