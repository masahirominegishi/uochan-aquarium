#!/bin/sh
# うおちゃん kiosk 起動 + 白画面ウォッチドッグ (2026-07-26 / 同日v2で常駐化)
#
# 経緯: 電源ON直後、chromium がネットワークサービスをクラッシュさせて
# 両画面とも真っ白のまま固まる事故が発生 (12:56 boot で実発生)。
# 従来 autostart はただ起動するだけで、描画できたかを確認していなかった。
# v2: 起動直後チェック通過の20秒後にクラッシュして白画面を取りこぼした実例
# (16:02 boot) を受け、チェックを1回きりから常駐 (60秒おき) に変更。
# v4 (2026-07-27): LG の canvas 縮小症状 (小さくトリミング+周囲黒) を受け、
# 白率に加えて黒率チェックを追加 (check_output / output_is_bad 参照)。
#
# このスクリプトは 起動 → 描画確認 (grim スクリーンショットの白率) → NG なら
# kill して再起動、その後も60秒おきに白画面を監視し続ける。手動でも実行できる
# (実行すると既存 kiosk と旧ウォッチドッグを落として起動し直す):
#   ~/Documents/aquarium/kiosk_launch.sh
#
# 白率判定: 水槽ページは常に青系なので、ほぼ全ピクセル白 = 描画死と断定できる。
# grim が撮れない出力 (TV電源off等) は判定スキップ (起動はしてあるので無害)。
#
# 証拠保全 (2026-07-26 v3.1): ログは /tmp でなく ~/kiosk_logs に永続保存する。
# 以前は chromium stderr を /tmp に truncate 上書きしていたため、ウォッチドッグが
# 再起動で回復するたびに直前の事故の証拠 (クラッシュメッセージ) が消えていた。
# 白画面の真因特定には「壊れたインスタンスの stderr」が必須なので、
# ランチごとにタイムスタンプ付きファイルへ分け、直近20世代を残す。
#
# デプロイ: Mac ~/Program/aquarium が権威。rsync で pi-main ~/Documents/aquarium へ。
# 呼び出し元: ~/.config/labwc/autostart (wlr-randr / CEC はそちらに残している)

export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

CHROMIUM=/usr/lib/chromium/chromium
URL_BASE="http://localhost:8080"
LOGDIR="$HOME/kiosk_logs"
LOG="$LOGDIR/watchdog.log"
PIDFILE=/tmp/kiosk_launch.pid
MAX_TRIES=3

mkdir -p "$LOGDIR"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# watchdog.log はブートをまたいで追記し続けるので、1MB 超で1世代ローテ
if [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv "$LOG" "$LOG.old"
fi
log "==== スクリプト開始 (boot=$(uptime -s)) ===="

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
  # SingletonLock 残骸の削除 (2026-07-26 v3.2)。シャットダウンで強制終了された
  # chromium のロックが残ると、次ブートで「raspberrypi-<旧PID>」の PID を偶然
  # 別プロセスが使っていて「別インスタンス生存」と誤認 → --noerrdialogs のため
  # ダイアログも出さず黙って終了する (stderr 0バイトの正体、ブート初回に毎回発生)。
  # ここは必ず pkill 直後なので、消して困る生きたロックは存在しない。
  rm -f "$HOME/.config/chromium/Singleton"* "$HOME/.config/chromium-tank65/Singleton"*
}

launch_kiosk() {
  # stderr はランチごとに新ファイルへ (証拠保全)。直近20世代 (LG/TV 各) を残す
  ts=$(date '+%Y%m%d_%H%M%S')
  for pfx in lg tv; do
    ls -1t "$LOGDIR"/${pfx}_*.log 2>/dev/null | tail -n +20 | xargs -r rm --
  done
  log "chromium ログ: ${ts} (lg_/tv_)"
  # --enable-logging=stderr --v=1: ブート初回の沈黙死 (stderr 0バイトのまま
  # 起動極初期で停止) の現場特定用トレース (2026-07-26 v3.3)。沈黙死の瞬間に
  # 「最後にどこまで進んだか」が残る。解決したら外してよい。
  # LG = 空の水槽 (うおちゃんが話しに来る側)。音は鳴らさない。
  "$CHROMIUM" --ozone-platform=wayland \
    --enable-logging=stderr --v=1 \
    --kiosk --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    --password-store=basic \
    --mute-audio \
    "$URL_BASE/?tank=sub" >"$LOGDIR/lg_$ts.log" 2>&1 &
  sleep 3
  # 65吋 = 大水槽。ゲスト魚と着水音はこちら。app_id=tank65 を rc.xml windowRule が拾う。
  "$CHROMIUM" --ozone-platform=wayland \
    --enable-logging=stderr --v=1 \
    --kiosk --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    --password-store=basic \
    --class=tank65 --user-data-dir="$HOME/.config/chromium-tank65" \
    --autoplay-policy=no-user-gesture-required \
    "$URL_BASE/?tank=main" >"$LOGDIR/tv_$ts.log" 2>&1 &
}

# 出力1面の描画判定。返り値:
#   "white"    = ほぼ全面白 (描画死)
#   "black"    = 黒が3割以上 (canvas 縮小症状。2026-07-27 boot で実発生:
#                窓は全画面・WS ok なのに p5 canvas だけ起動初期サイズ 945x640 の
#                まま左上に残り、周囲が黒。白率も WS も素通りする穴だった。
#                水槽は常に青系 (実測: 正常時の黒率 0.000〜0.002 / 症状時 0.694)
#                なので黒3割 = 異常と断定できる)
#   "allblack" = ほぼ全面黒。65吋は HDCP で grim が正常でも全面黒になることが
#                あるため、呼び出し側は HDMI-A-2 の allblack を異常扱いしない
#   "ok" / "skip" (撮れない)
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
n = len(px)
white = sum(1 for v in px if v > 245) / n
black = sum(1 for v in px if v < 16) / n
if white > 0.98:
    print("white")
elif black >= 0.985:
    print("allblack")
elif black >= 0.30:
    print("black")
else:
    print("ok")
EOF
}

# check_output の結果が異常 (再起動に値する) なら 0 を返す。
# allblack は LG (HDMI-A-1) のみ異常扱い (65吋は HDCP の偽陰性がありうるため)
output_is_bad() {
  case "$2" in
    white|black) return 0 ;;
    allblack) [ "$1" = "HDMI-A-1" ] && return 0 ;;
  esac
  return 1
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
      output_is_bad "$out" "$r" && bad=1
    done
    w=$(check_ws)
    # procs = 生きている chromium 本体プロセス数 (正常=2)。沈黙死の
    # 「死んでいるのかハングしているのか」の切り分け用
    np=$(pgrep -fc "chromium.*tank=" 2>/dev/null || echo 0)
    log "  ws: $w (chromium procs: $np)"
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
  reason=""
  for out in HDMI-A-1 HDMI-A-2; do
    r=$(check_output "$out")
    if output_is_bad "$out" "$r"; then
      bad=1
      reason="$out: $r"
    fi
  done
  if [ "$bad" -eq 1 ]; then
    log "定期チェックで描画異常検知 ($reason) → 再起動する"
    ws_bad_streak=0
    start_and_verify
    continue
  fi
  if [ "$(check_ws)" = "bad" ]; then
    ws_bad_streak=$((ws_bad_streak + 1))
    np=$(pgrep -fc "chromium.*tank=" 2>/dev/null || echo 0)
    log "定期チェックで WS 未接続 ($ws_bad_streak/2, chromium procs: $np)"
    if [ "$ws_bad_streak" -ge 2 ]; then
      log "WS 未接続が継続 → 再起動する"
      ws_bad_streak=0
      start_and_verify
    fi
  else
    ws_bad_streak=0
  fi
done
