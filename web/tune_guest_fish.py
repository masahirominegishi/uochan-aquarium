#!/usr/bin/env python3
"""ゲスト魚トリミング (紙面切り出し → 背景除去 → crop) のチューニング用 CLI。

realtime_loop.py を立ち上げずに、撮影 → 紙面検出 → 白除去 → トリミング を
単体で回して詰めるためのツール。pi-main 上で実行する。

処理は 2 段:
  (1) 紙面検出 (detect_paper_bbox): 撮影画像の周囲に写り込んだ暗いブース枠や
      画面下部の景色を捨て、白い紙のシートの bbox に crop する。
  (2) 白除去 (remove_white_background): (1) の中で、絵の線画だけ残して白い
      紙の部分を透明化し、絵に tight crop + 長辺リサイズ。

依存 (numpy / Pillow / scipy) は fish_ai_realtime の venv に入っているので、
そちらの python で起動する:

  ~/Documents/fish_ai_realtime/.venv/bin/python ~/Documents/aquarium/web/tune_guest_fish.py --help

典型ワークフロー:
  1. ブースに絵を置いて撮る + 紙面検出結果を HDMI に映して目視確認
     (撮影時はブース LED を自動点灯 → warmup → 撮影 → 消灯。--no-led で抑止):
       ... tune_guest_fish.py --capture --show
     → tune_out/paper_detect.png (元画像に紙面 bbox を緑枠・外側を暗赤で表示) を
       HDMI にフルスクリーン表示。tune_out/paper_crop.jpg = 切り出した紙面。
     紙面の取れ方がずれてたら --paper-v / --paper-s / --paper-pad を調整して再実行。
     (例: 枠が残る → --paper-v を下げる / --paper-pad を負に。絵の端が切れる → --paper-pad を正に)
  2. 紙面が決まったら白除去のスイープ → モンタージュ:
       ... tune_guest_fish.py --sweep --show --show-target sweep
     → tune_out/sweep.png (HDMI 表示)。v×s は --v-list / --s-list で変更可。
  3. 単発で白除去マスクを詳しく見る:
       ... tune_guest_fish.py --v-thresh 210 --s-thresh 35 --show --show-target mask
     → tune_out/result.png / result_on_checker.png / mask_overlay.png (赤=除去 / 緑枠=crop)
  4. 良い値が決まったら反映:
       - 即時 & realtime_loop も含めて効かせる: 起動前に環境変数
           GUEST_FISH_PAPER_V=160 GUEST_FISH_PAPER_S=90 GUEST_FISH_V_THRESH=210 ...
       - 恒久化: web/config.json の paper_detect.* / background_removal.* / output.long_edge を編集

--show は chromium --kiosk で pi-main の HDMI に出す。閉じるのは pkill -f 'chromium.*--kiosk'。
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
from guest_fish_pipeline import (  # noqa: E402
    compute_bg_mask,
    crop_to_paper,
    remove_white_background,
    resolve_paper_params,
    resolve_params,
)

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


def _paper_detect_visual(src: Image.Image, bbox, *, fit_to=(1900, 1060)) -> Image.Image:
    """元画像を画面サイズに収めて、検出した紙面 bbox を緑枠で、外側を暗く塗った確認用画像。"""
    base = src.convert("RGB").copy()
    if bbox is not None:
        ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        l, t, r, b = bbox
        # bbox の外側を暗赤でほぼ塗りつぶす (= 捨てられる範囲。下の画像はうっすら透ける程度)
        kill = (110, 0, 0, 215)
        d.rectangle([0, 0, base.size[0], t], fill=kill)
        d.rectangle([0, b, base.size[0], base.size[1]], fill=kill)
        d.rectangle([0, t, l, b], fill=kill)
        d.rectangle([r, t, base.size[0], b], fill=kill)
        base = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")
        d2 = ImageDraw.Draw(base)
        d2.rectangle([l, t, r - 1, b - 1], outline=(0, 235, 0), width=max(3, base.size[0] // 350))
        label = f"paper bbox = ({l},{t})-({r},{b})  size {r - l}x{b - t}"
    else:
        label = "紙面を検出できませんでした (--paper-v / --paper-s を緩めてみてください)"
    out = _fit(base, fit_to)
    d3 = ImageDraw.Draw(out)
    f = _font(20)
    d3.rectangle([0, 0, out.size[0], 30], fill=(0, 0, 0))
    d3.text((8, 5), label, fill=(255, 255, 255), font=f)
    return out


def _show_on_hdmi(path: Path) -> None:
    """pi-main の HDMI 画面に画像をフルスクリーン表示する (chromium --kiosk、Xwayland 経由)。"""
    env = dict(os.environ)
    env["DISPLAY"] = ":0"
    env["XAUTHORITY"] = "/home/mine/.Xauthority"
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    subprocess.run(["pkill", "-f", "chromium.*--kiosk"], capture_output=True)
    time.sleep(0.5)
    cmd = [
        "chromium", "--kiosk", "--noerrdialogs", "--disable-infobars", "--no-first-run",
        "--disable-session-crashed-bubble", "--check-for-update-interval=31536000",
        f"file://{path}",
    ]
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print(f"==> HDMI に表示中: {path}  (閉じるには: pkill -f 'chromium.*--kiosk')")


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
    # 紙面検出 (撮影画像から白い紙のシートだけを切り出す)
    ap.add_argument("--paper-v", type=int, help="紙面検出の value 閾値 (省略時は env/config/既定 170)")
    ap.add_argument("--paper-s", type=int, help="紙面検出の saturation 閾値 (省略時は env/config/既定 80)")
    ap.add_argument("--paper-pad", type=int, help="紙面 bbox を広げる px。負で内側に縮める (省略時は env/config/既定 0)")
    ap.add_argument("--no-paper-crop", action="store_true", help="紙面トリミングをせず、撮影画像そのまま白除去にかける")
    # 白除去 (紙面を切り出した後、絵だけ残して背景を透明化)
    ap.add_argument("--v-thresh", type=int, help="白除去の value 閾値 (省略時は env/config/既定値)")
    ap.add_argument("--s-thresh", type=int, help="白除去の saturation 閾値 (省略時は env/config/既定値)")
    ap.add_argument("--long-edge", type=int, help="出力長辺 px (省略時は env/config/既定値)")
    ap.add_argument("--sweep", action="store_true", help="白除去の v×s グリッドを総当たりして sweep.png を出す")
    ap.add_argument("--v-list", type=_parse_int_list, default=_parse_int_list("180,190,200,210,220"), help="--sweep の value 閾値リスト (カンマ区切り)")
    ap.add_argument("--s-list", type=_parse_int_list, default=_parse_int_list("20,30,40,50"), help="--sweep の saturation 閾値リスト (カンマ区切り)")
    # HDMI 表示
    ap.add_argument("--show", action="store_true", help="処理後に結果を pi-main の HDMI 画面にフルスクリーン表示する")
    ap.add_argument("--show-target", choices=["detect", "crop", "result", "sweep", "mask"], default="detect",
                    help="--show で映すもの: detect=紙面検出の確認画像(既定) / crop=切り出した紙面 / result=白除去後 / sweep=モンタージュ / mask=白除去マスク")
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

    # 1) 紙面検出 + crop
    crop_img, bbox = crop_to_paper(src, v_thresh=args.paper_v, s_thresh=args.paper_s, pad=args.paper_pad)
    pp = resolve_paper_params(args.paper_v, args.paper_s, args.paper_pad)
    if bbox is None:
        print(f"==> 紙面検出: 失敗 (params={pp})。撮影画像そのままを使います。")
    else:
        l, t, r, b = bbox
        print(f"==> 紙面検出: bbox=({l},{t})-({r},{b}) size {r - l}x{b - t} / 元 {src.size[0]}x{src.size[1]} (params={pp})")
    detect_path = out_dir / "paper_detect.png"
    _paper_detect_visual(src, bbox).save(detect_path)
    crop_path = out_dir / "paper_crop.jpg"
    crop_img.convert("RGB").save(crop_path, quality=92)
    print(f"==> 紙面確認画像 -> {detect_path}")
    print(f"==> 切り出した紙面 -> {crop_path}")

    # 2) 白除去フェーズ (紙面を切り出した後の画像に対して)
    work = src if (args.no_paper_crop or bbox is None) else crop_img
    if args.sweep:
        sweep(work, out_dir, args.v_list, args.s_list, args.long_edge)
    else:
        single(work, out_dir, args.v_thresh, args.s_thresh, args.long_edge)

    # 3) HDMI 表示
    if args.show:
        target = {
            "detect": detect_path,
            "crop": crop_path,
            "result": out_dir / "result_on_checker.png",
            "sweep": out_dir / "sweep.png",
            "mask": out_dir / "mask_overlay.png",
        }[args.show_target]
        if target.exists():
            _show_on_hdmi(target)
        else:
            print(f"--show-target {args.show_target} の出力 ({target}) が無いので表示をスキップ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
