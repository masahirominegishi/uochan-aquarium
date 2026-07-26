#!/bin/sh
# うおちゃん kiosk 起動 + 白画面ウォッチドッグ (2026-07-26)
#
# 経緯: 電源ON直後、chromium がネットワークサービスをクラッシュさせて
# 両画面とも真っ白のまま固まる事故が発生 (12:56 boot で実発生)。
# 従来 autostart はただ起動するだけで、描画できたかを確認していなかった。
#
# このスクリプトは 起動 → 描画確認 (grim スクリーンショットの白率) → NG なら
# kill して再起動、を最大 MAX_TRIES 回繰り返す。手動でも実行できる
# (実行すると既存 kiosk を落として起動し直す):
#   ~/Documents/aquarium/kiosk_launch.sh
#
# 白率判定: 水槽ページは常に青系なので、ほぼ全ピクセル白 = 描画死と断定できる。
# grim が撮れない出力 (TV電源off等) は判定スキップ (起動はしてあるので無害)。
#
# デプロイ: Mac ~/Program/aquarium が権威。rsync で pi-main ~/Documents/aquarium へ。
# 呼び出し元: ~/.config/labwc/autostart (wlr-randr / CEC はそちらに残している)

export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

CHROMIUM=/usr/lib/chromium/chromium
URL_BASE="http://localhost:8080"
LOG=/tmp/kiosk_watchdog.log
MAX_TRIES=3

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

kill_kiosk() {
  # 自プロセスの cmdline はスクリプトパスだけなので自爆しない
  pkill -f "chromium.*tank=" 2>/dev/null
  sleep 3
}

launch_kiosk() {
  # LG = 空の水槽 (うおちゃんが話しに来る側)。音は鳴らさない。
  "$CHROMIUM" --ozone-platform=wayland \
    --kiosk --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    --password-store=basic \
    --mute-audio \
    "$URL_BASE/?tank=sub" >/tmp/kiosk_lg.log 2>&1 &
  sleep 3
  # 65吋 = 大水槽。ゲスト魚と着水音はこちら。app_id=tank65 を rc.xml windowRule が拾う。
  "$CHROMIUM" --ozone-platform=wayland \
    --kiosk --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    --password-store=basic \
    --class=tank65 --user-data-dir="$HOME/.config/chromium-tank65" \
    --autoplay-policy=no-user-gesture-required \
    "$URL_BASE/?tank=main" >/tmp/kiosk_tv.log 2>&1 &
}

# 出力1面が真っ白なら "white"、正常なら "ok"、撮れなければ "skip" を返す
check_output() {
  out="$1"
  shot="/tmp/kiosk_check_$out.png"
  if ! grim -o "$out" "$shot" 2>/dev/null; then
    echo skip
    return
  fi
  python3 - "$shot" <<'EOF'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("L").resize((64, 36))
px = list(im.getdata())
white = sum(1 for v in px if v > 245) / len(px)
print("white" if white > 0.98 else "ok")
EOF
}

# Web サーバー待ち (最大60秒)
i=0
while [ "$i" -lt 60 ]; do
  curl -sf -o /dev/null "$URL_BASE/" && break
  sleep 1
  i=$((i + 1))
done

n=1
while [ "$n" -le "$MAX_TRIES" ]; do
  kill_kiosk
  launch_kiosk
  log "起動 (試行 $n/$MAX_TRIES)"
  sleep 20  # ページ読み込み待ち
  bad=0
  for out in HDMI-A-1 HDMI-A-2; do
    r=$(check_output "$out")
    log "  $out: $r"
    [ "$r" = "white" ] && bad=1
  done
  if [ "$bad" -eq 0 ]; then
    log "描画OK"
    exit 0
  fi
  log "白画面検知 → 再起動する"
  n=$((n + 1))
done
log "白画面のまま諦め ($MAX_TRIES 回失敗)。手動確認が必要"
exit 1
