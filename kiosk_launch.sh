#!/bin/sh
# うおちゃん kiosk 起動 + 白画面ウォッチドッグ (2026-07-26 / 同日v2で常駐化)
#
# 経緯: 電源ON直後、chromium がネットワークサービスをクラッシュさせて
# 両画面とも真っ白のまま固まる事故が発生 (12:56 boot で実発生)。
# 従来 autostart はただ起動するだけで、描画できたかを確認していなかった。
# v2: 起動直後チェック通過の20秒後にクラッシュして白画面を取りこぼした実例
# (16:02 boot) を受け、チェックを1回きりから常駐 (60秒おき) に変更。
#
# このスクリプトは 起動 → 描画確認 (grim スクリーンショットの白率) → NG なら
# kill して再起動、その後も60秒おきに白画面を監視し続ける。手動でも実行できる
# (実行すると既存 kiosk と旧ウォッチドッグを落として起動し直す):
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
PIDFILE=/tmp/kiosk_launch.pid
MAX_TRIES=3

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 常駐化に伴う一人っ子制御: 旧インスタンス (常駐ウォッチドッグ) が居たら退場させる
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
    kill "$old" 2>/dev/null
    sleep 1
  fi
fi
echo "$$" > "$PIDFILE"

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

# ブリッジ (ポート 8765) への WS 接続数チェック。
# 「絵は描けているがイベント配線が死んでいる」状態の検出用。chromium の
# ネットワークサービスだけがクラッシュすると、canvas は青い水槽を描き続ける
# (= 白率チェックは素通り) のに WS が繋がらず、うおちゃんが一切動かない事故が
# 起きる (2026-07-26 16:47 boot で実発生)。
# 返り値: "ok" = 2 本以上 / "bad" = 2 本未満 / "skip" = ブリッジ自体が
# 落ちている (chromium のせいではないので kiosk 再起動しても直らない)
check_ws() {
  if [ -z "$(ss -Htln 'sport = :8765' 2>/dev/null)" ]; then
    echo skip
    return
  fi
  n_ws=$(ss -Htn state established 'sport = :8765' 2>/dev/null | wc -l)
  [ "$n_ws" -ge 2 ] && echo ok || echo bad
}

# 起動 → 描画+WS確認 を最大 MAX_TRIES 回。成功で 0
start_and_verify() {
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
    w=$(check_ws)
    log "  ws: $w"
    [ "$w" = "bad" ] && bad=1
    if [ "$bad" -eq 0 ]; then
      log "描画+WS OK"
      return 0
    fi
    log "異常検知 → 再起動する"
    n=$((n + 1))
  done
  log "異常のまま ($MAX_TRIES 回失敗)。60秒後の定期チェックで再挑戦する"
  return 1
}

# Web サーバー待ち (最大60秒)
i=0
while [ "$i" -lt 60 ]; do
  curl -sf -o /dev/null "$URL_BASE/" && break
  sleep 1
  i=$((i + 1))
done

start_and_verify

# 常駐ウォッチドッグ: 起動直後のチェック通過後にクラッシュするケースを拾う。
# 正常時はログを書かない (60秒おきのスパム防止)。
# WS 未接続は 2 回連続 (約2分) で再起動: ws-client は 5 秒間隔で再接続を
# 試みるので、一瞬の切断は 1 回目のチェックまでに自然回復する。連続で
# 落ちたままなら chromium 側が壊れていると判断する。
ws_bad_streak=0
while :; do
  sleep 60
  bad=0
  for out in HDMI-A-1 HDMI-A-2; do
    [ "$(check_output "$out")" = "white" ] && bad=1
  done
  if [ "$bad" -eq 1 ]; then
    log "定期チェックで白画面検知 → 再起動する"
    ws_bad_streak=0
    start_and_verify
    continue
  fi
  if [ "$(check_ws)" = "bad" ]; then
    ws_bad_streak=$((ws_bad_streak + 1))
    log "定期チェックで WS 未接続 ($ws_bad_streak/2)"
    if [ "$ws_bad_streak" -ge 2 ]; then
      log "WS 未接続が継続 → 再起動する"
      ws_bad_streak=0
      start_and_verify
    fi
  else
    ws_bad_streak=0
  fi
done
