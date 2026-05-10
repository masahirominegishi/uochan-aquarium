#!/usr/bin/env python3
"""ライブ: IMX219 の映像に「色補正 (固定 awbgains) + レベル補正」だけかけて HDMI に全画面表示する。切り抜きはしない。

pi-main 上で実行 (DISPLAY=:0 が要る):
  DISPLAY=:0 XAUTHORITY=/home/mine/.Xauthority \
    ~/Documents/fish_ai_realtime/.venv/bin/python ~/Documents/aquarium/web/live_levels.py --white 178 --black 10
終了: プレビュー窓で ESC か q、または別シェルで  pkill -f live_levels.py

--white を下げるほど「薄いグレー (= グレーの白)」が白に飛ぶ。--black を上げるほど暗部が締まる。--gamma <1 で中間調が暗く。
awbgains は color cast 中和済みの 1.65,1.20 を既定で固定 (--rg / --bg で変更可)。
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from picamera2 import Picamera2
from libcamera import Transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guest_fish_pipeline import levels  # noqa: E402  (pure numpy, no cv2/scipy)

CAM = 0
SENSOR_W, SENSOR_H = 1640, 1232


def _led(on: bool) -> None:
    try:
        subprocess.run(["pinctrl", "set", "18", "op", "dh" if on else "dl"], check=False, timeout=4)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--black", type=int, default=10, help="レベル補正の黒点 (これ以下が黒に)")
    ap.add_argument("--white", type=int, default=178, help="レベル補正の白点 (これ以上が白に)。下げるほどグレーが白に飛ぶ")
    ap.add_argument("--gamma", type=float, default=1.0, help="中間調 gamma (<1 で暗く)")
    ap.add_argument("--rg", type=float, default=1.65, help="赤ゲイン (固定 AWB)")
    ap.add_argument("--bg", type=float, default=1.20, help="青ゲイン (固定 AWB)")
    ap.add_argument("--no-led", action="store_true")
    args = ap.parse_args()

    led = not args.no_led
    if led:
        _led(True)
        time.sleep(0.8)

    picam2 = Picamera2(CAM)
    cfg = picam2.create_preview_configuration(
        main={"size": (SENSOR_W, SENSOR_H), "format": "RGB888"},
        transform=Transform(hflip=1, vflip=1), buffer_count=4,
        controls={"AwbEnable": False, "ColourGains": (float(args.rg), float(args.bg))})
    picam2.configure(cfg)
    picam2.start()
    time.sleep(0.6)

    win = "LIVE  awb+levels  (ESC / q to quit)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    label = f"awbgains {args.rg},{args.bg}  levels black={args.black} white={args.white} gamma={args.gamma}"
    print(label + "   -- ESC/q で終了")
    try:
        while True:
            t0 = time.time()
            arr = picam2.capture_array("main")  # "RGB888" は numpy では BGR (per-channel 処理なので順序は不問、表示は cv2=BGR で一致)
            adj = levels(arr, black=args.black, white=args.white, gamma=args.gamma)
            txt = f"{label}   {1.0 / max(time.time() - t0, 1e-3):4.0f} fps"
            cv2.putText(adj, txt, (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.95, (0, 0, 0), 5)
            cv2.putText(adj, txt, (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.95, (0, 220, 0), 2)
            cv2.imshow(win, adj)
            if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
                break
    finally:
        picam2.stop()
        cv2.destroyAllWindows()
        if led:
            _led(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
