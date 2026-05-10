#!/usr/bin/env python3
"""ゲスト魚切り抜き (紙面 crop → 切り抜き) のチューニング用 CLI。pi-main 上で実行。

realtime_loop.py を立ち上げずに、撮影 → 紙面検出 → 切り抜き を単体で回して詰める。
依存 (numpy / Pillow / scipy / opencv) は fish_ai_realtime の venv 経由で起動:
  ~/Documents/fish_ai_realtime/.venv/bin/python ~/Documents/aquarium/web/tune_guest_fish.py --help

処理は 2 段:
  (1) 紙面検出 (detect_paper_bbox): 撮影画像の周囲の暗いブース枠/景色を捨てて
      白い紙の bbox に crop。margins は paper_detect.margins で確定済 ([-73,-80,-32,-80])。
  (2) 切り抜き (remove_white_background, bg_method で 2 方式):
      "shape" (既定): 背景差分でインク検出 → 輪郭の隙間を close_px で橋渡し → 中を満たす
        (色は写真のまま) → 輪郭を smooth で整える。適正露出 (--ev 0) 前提、色かぶりに強い。
      "hsv" (旧): HSV しきい値で「縁から繋がる白」を透明化。fill_body=True なら中身も埋める。

典型ワークフロー (shape):
  1. ブースに絵を置いて撮る + 結果を HDMI 確認 (撮影時 LED 自動点灯 → warmup → 撮影 → 消灯):
       ... --capture --show --show-target result
     → tune_out/{paper_detect.png, paper_crop.jpg, result.png, result_on_checker.png, mask_overlay.png}
  2. 輪郭の閉じ具合を比較: ... --sweep-close --show --show-target sweep   (close_px を振る)
  3. 詳しく見る: ... --close-px 22 --smooth 1.5 --show --show-target mask
     (mask_overlay = 青:検出インク / 赤:透明化される魚の外 / 境界:最終の輪郭)
  4. 良い値を反映: env (GUEST_FISH_INK_THRESH / _CLOSE_PX / _SMOOTH / _BG_METHOD ...) か
     web/config.json の shape_detect.* / bg_method / output.long_edge を編集。

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
    compute_silhouette,
    crop_to_paper,
    fish_mask,
    ink_mask,
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


def _mask_overlay(src_rgb: np.ndarray, removed: np.ndarray) -> Image.Image:
    """除去されるピクセル (removed=True) を赤、残る範囲の bbox を緑枠で重ねた可視化。"""
    overlay = src_rgb.copy()
    red = np.array([255, 40, 40], dtype=np.float32)
    overlay[removed] = (overlay[removed].astype(np.float32) * 0.35 + red * 0.65).astype(np.uint8)
    ov = Image.fromarray(overlay, "RGB")
    keep = ~removed
    if keep.any():
        ys, xs = np.where(keep)
        ImageDraw.Draw(ov).rectangle([int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
                                     outline=(0, 230, 0), width=4)
    return ov


def _params_brief(p: dict) -> str:
    if p["bg_method"] == "shape":
        return f"bg_method=shape ink_thresh={p['ink_thresh']} bg_blur={p['bg_blur']} close_px={p['close_px']} smooth={p['smooth']} long_edge={p['long_edge']}"
    return f"bg_method=hsv v_thresh={p['v_thresh']} s_thresh={p['s_thresh']} fill_body={p['fill_body']} fill_close={p['fill_close']} long_edge={p['long_edge']}"


def _shape_mask_overlay(src_rgb: np.ndarray, ink: np.ndarray, fm: np.ndarray) -> Image.Image:
    """青=検出インク, 赤=魚の外(透明化される), それ以外=元のまま。境界が最終の輪郭。"""
    ov = src_rgb.astype(np.float32).copy()
    ov[ink] = ov[ink] * 0.35 + np.array([40, 120, 255], np.float32) * 0.65
    ov[~fm] = ov[~fm] * 0.30 + np.array([255, 45, 45], np.float32) * 0.70
    return Image.fromarray(np.clip(ov, 0, 255).astype(np.uint8), "RGB")


def single(src: Image.Image, out_dir: Path, **kw) -> None:
    p = resolve_params(**kw)
    print(f"==> 使用パラメータ: {_params_brief(p)}")
    src_rgb = np.array(src.convert("RGB"))

    result = remove_white_background(src, **p)
    res_path = out_dir / "result.png"
    result.save(res_path)
    _on_checker(result).convert("RGB").save(out_dir / "result_on_checker.png")
    print(f"==> 結果 {result.size[0]}x{result.size[1]} -> {res_path} (+ result_on_checker.png)")

    if p["bg_method"] == "shape":
        ink = ink_mask(src_rgb, thresh=p["ink_thresh"], blur=p["bg_blur"])
        fm = fish_mask(src_rgb, ink_thresh=p["ink_thresh"], bg_blur=p["bg_blur"], close_px=p["close_px"], smooth=p["smooth"])
        _shape_mask_overlay(src_rgb, ink, fm).save(out_dir / "mask_overlay.png")
        print(f"==> マスク可視化 (青=検出インク / 赤=透明化(魚の外) / 境界=最終の輪郭) -> {out_dir / 'mask_overlay.png'}")
    else:
        if p["fill_body"]:
            removed = ~compute_silhouette(src_rgb, v_thresh=p["v_thresh"], s_thresh=p["s_thresh"], close_px=p["fill_close"])
            note = f"赤=透明化される範囲 / 緑枠=crop (fill_body, close={p['fill_close']})"
        else:
            removed = compute_bg_mask(src_rgb, v_thresh=p["v_thresh"], s_thresh=p["s_thresh"])
            note = "赤=除去 / 緑枠=crop"
        _mask_overlay(src_rgb, removed).save(out_dir / "mask_overlay.png")
        print(f"==> マスク可視化 ({note}) -> {out_dir / 'mask_overlay.png'}")


def _save_sheet(sheet: Image.Image, out_dir: Path) -> None:
    sheet_path = out_dir / "sweep.png"
    sheet.save(sheet_path)
    print(f"==> モンタージュ -> {sheet_path}")
    print("    Pi デスクトップで:  xdg-open " + str(sheet_path))
    print("    ブラウザで:        http://raspberrypi.local:8080/tune_out/sweep.png")


def _montage(cells: list[tuple[str, Image.Image]], out_dir: Path, *, cols: int, tile: int = 320) -> None:
    label_h = 22
    pad = 8
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tile + pad) + pad, rows * (tile + label_h + pad) + pad), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    font = _font(13)
    for i, (label, im) in enumerate(cells):
        thumb = _fit(_on_checker(im).convert("RGB"), (tile, tile))
        ci, ri = i % cols, i // cols
        x0 = pad + ci * (tile + pad)
        y0 = pad + ri * (tile + label_h + pad)
        tw, th = thumb.size
        sheet.paste(thumb, (x0 + (tile - tw) // 2, y0 + label_h + (tile - th) // 2))
        d.rectangle([x0 + (tile - tw) // 2, y0 + label_h + (tile - th) // 2,
                     x0 + (tile - tw) // 2 + tw - 1, y0 + label_h + (tile - th) // 2 + th - 1], outline=(170, 170, 170))
        d.text((x0 + 2, y0 + 3), label, fill=(0, 0, 0), font=font)
    _save_sheet(sheet, out_dir)


def sweep_vs(src: Image.Image, out_dir: Path, v_list: list[int], s_list: list[int], **kw) -> None:
    """bg_method=hsv: v×s グリッド。fill_body はそのまま使う。"""
    p = resolve_params(**kw)
    cells = []
    print(f"==> v×s スイープ (hsv, fill_body={p['fill_body']})")
    for s in s_list:
        for v in v_list:
            res = remove_white_background(src, bg_method="hsv", v_thresh=v, s_thresh=s,
                                          fill_body=p["fill_body"], fill_close=p["fill_close"], long_edge=p["long_edge"])
            cells.append((f"v={v} s={s}  {res.size[0]}x{res.size[1]}", res))
            print(f"  v={v:>3} s={s:>3} -> {res.size[0]}x{res.size[1]}")
    _montage(cells, out_dir, cols=len(v_list))


def sweep_close(src: Image.Image, out_dir: Path, close_list: list[int], **kw) -> None:
    """shape: close_px を振る。hsv: fill_body=True 固定で fill_close を振る。"""
    p = resolve_params(**kw)
    is_shape = p["bg_method"] == "shape"
    cells = []
    print(f"==> {'close_px (shape)' if is_shape else 'fill_close (hsv)'} スイープ")
    for ck in close_list:
        if is_shape:
            res = remove_white_background(src, bg_method="shape", ink_thresh=p["ink_thresh"], bg_blur=p["bg_blur"],
                                          close_px=ck, smooth=p["smooth"], long_edge=p["long_edge"])
            label = f"close_px={ck}  {res.size[0]}x{res.size[1]}"
        else:
            res = remove_white_background(src, bg_method="hsv", v_thresh=p["v_thresh"], s_thresh=p["s_thresh"],
                                          fill_body=True, fill_close=ck, long_edge=p["long_edge"])
            label = f"fill_close={ck}  {res.size[0]}x{res.size[1]}"
        cells.append((label, res))
        print(f"  {ck:>3} -> {res.size[0]}x{res.size[1]}")
    _montage(cells, out_dir, cols=min(len(close_list), 3))


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
    ap.add_argument("--ev", type=float, default=0.0, help="撮影時の露出補正 EV (既定 0 = 適正露出。色を飛ばさないため)")
    ap.add_argument("--rotation", type=int, default=180, help="撮影時の回転角 (既定 180、本番と同値)")
    ap.add_argument("--rpicam-extra", default="", help="rpicam-still に渡す追加引数 (例: '--awb tungsten --shutter 8000')")
    ap.add_argument("--no-led", action="store_true", help="撮影時にブース LED を点灯しない (既定は点灯する)")
    ap.add_argument("--led-warmup", type=float, default=LED_WARMUP_DEFAULT, help=f"LED 点灯から撮影までの待ち秒数 (既定 {LED_WARMUP_DEFAULT})")
    ap.add_argument("--input", type=Path, help="処理する画像 (既定: tune_out/latest.jpg)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"出力先 (既定 {DEFAULT_OUT_DIR})")
    # 紙面検出 (撮影画像から白い紙のシートだけを切り出す)
    ap.add_argument("--paper-v", type=int, help="紙面検出の value 閾値 (省略時は env/config/既定 150)")
    ap.add_argument("--paper-s", type=int, help="紙面検出の saturation 閾値 (省略時は env/config/既定 80)")
    ap.add_argument("--paper-pad", help="紙面 bbox を 4 辺一律に広げる px。負で内側に縮める (例: -100)")
    ap.add_argument("--paper-margins", help="紙面 bbox を辺ごとに広げる px 'L,T,R,B' (例: '-90,-90,-40,-90')。負で内側。--paper-pad より優先")
    ap.add_argument("--no-paper-crop", action="store_true", help="紙面トリミングをせず、撮影画像そのまま白除去にかける")
    # 切り抜き方式
    ap.add_argument("--bg-method", choices=["shape", "hsv"], dest="bg_method", default=None,
                    help="shape=形ベース(背景差分→輪郭を閉じる→中を満たす→輪郭整え) / hsv=旧 白除去。省略時は config/既定(shape)")
    # shape 用
    ap.add_argument("--ink-thresh", type=int, dest="ink_thresh", help="背景差分でインクとみなす残差しきい値 (省略時 config/既定 28)。下げると薄いインクも拾う")
    ap.add_argument("--bg-blur", type=int, dest="bg_blur", help="紙の面を推定するメディアンぼかし ksize (省略時 0=自動)")
    ap.add_argument("--close-px", type=int, dest="close_px", help="輪郭の隙間を橋渡しする膨張量 px (省略時 config/既定 18)")
    ap.add_argument("--smooth", type=float, help="輪郭整え: approxPolyDP epsilon を周長の何% にするか (0=整えない、省略時 config/既定 1.0)")
    # hsv 用 (旧)
    ap.add_argument("--v-thresh", type=int, dest="v_thresh", help="[hsv] value 閾値 (省略時 env/config/既定)")
    ap.add_argument("--s-thresh", type=int, dest="s_thresh", help="[hsv] saturation 閾値 (省略時 env/config/既定)")
    ap.add_argument("--fill-body", dest="fill_body", action="store_const", const=True, default=None,
                    help="[hsv] 魚の中身まで不透明にする (省略時 env/config/既定)")
    ap.add_argument("--no-fill-body", dest="fill_body", action="store_const", const=False, help="[hsv] 魚の中身を埋めない")
    ap.add_argument("--fill-close", type=int, dest="fill_close", help="[hsv] fill_body の輪郭隙間を埋める強さ px (省略時 env/config/既定 25)")
    # 共通
    ap.add_argument("--long-edge", type=int, dest="long_edge", help="出力長辺 px (省略時 env/config/既定 600)")
    # スイープ
    ap.add_argument("--sweep", action="store_true", help="[hsv] v×s グリッドのモンタージュを出す")
    ap.add_argument("--v-list", type=_parse_int_list, default=_parse_int_list("180,190,200,210,220"), help="--sweep の value リスト")
    ap.add_argument("--s-list", type=_parse_int_list, default=_parse_int_list("20,30,40,50"), help="--sweep の saturation リスト")
    ap.add_argument("--sweep-close", action="store_true", help="--close-list を振ったモンタージュ (shape=close_px / hsv=fill_close)")
    ap.add_argument("--close-list", type=_parse_int_list, default=_parse_int_list("8,12,18,25,35,50"), help="--sweep-close のリスト")
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
    crop_img, bbox = crop_to_paper(src, v_thresh=args.paper_v, s_thresh=args.paper_s,
                                   margins=args.paper_margins, pad=args.paper_pad)
    pp = resolve_paper_params(args.paper_v, args.paper_s, args.paper_margins, args.paper_pad)
    if bbox is None:
        print(f"==> 紙面検出: 失敗 (params={pp})。撮影画像そのままを使います。")
    else:
        l, t, r, b = bbox
        print(f"==> 紙面検出: bbox=({l},{t})-({r},{b}) size {r - l}x{b - t} / 元 {src.size[0]}x{src.size[1]} (margins L,T,R,B={pp['margins']}, v={pp['v_thresh']} s={pp['s_thresh']})")
    detect_path = out_dir / "paper_detect.png"
    _paper_detect_visual(src, bbox).save(detect_path)
    crop_path = out_dir / "paper_crop.jpg"
    crop_img.convert("RGB").save(crop_path, quality=92)
    print(f"==> 紙面確認画像 -> {detect_path}")
    print(f"==> 切り出した紙面 -> {crop_path}")

    # 2) 切り抜きフェーズ (紙面を切り出した後の画像に対して)
    work = src if (args.no_paper_crop or bbox is None) else crop_img
    proc_kw = dict(bg_method=args.bg_method, ink_thresh=args.ink_thresh, bg_blur=args.bg_blur,
                   close_px=args.close_px, smooth=args.smooth, v_thresh=args.v_thresh, s_thresh=args.s_thresh,
                   fill_body=args.fill_body, fill_close=args.fill_close, long_edge=args.long_edge)
    if args.sweep_close:
        sweep_close(work, out_dir, args.close_list, **proc_kw)
    elif args.sweep:
        sweep_vs(work, out_dir, args.v_list, args.s_list, **proc_kw)
    else:
        single(work, out_dir, **proc_kw)

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
