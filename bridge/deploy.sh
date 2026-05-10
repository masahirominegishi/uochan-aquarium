#!/usr/bin/env bash
# Mac の ~/Program/aquarium/ を pi-main の ~/Documents/aquarium/ に一方向同期する。
#
# 使い方:
#   bash ~/Program/aquarium/bridge/deploy.sh           # 通常の同期
#   bash ~/Program/aquarium/bridge/deploy.sh --dry     # rsync の予行 (実変更なし)
#   bash ~/Program/aquarium/bridge/deploy.sh --restart # 同期後に bridge と web service を再起動
set -euo pipefail

SRC="$HOME/Program/aquarium/"
DEST="pi-main:/home/mine/Documents/aquarium/"

DRY=""
RESTART=""
for arg in "$@"; do
  case "$arg" in
    --dry) DRY="--dry-run" ;;
    --restart) RESTART="1" ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

echo "==> rsync $SRC -> $DEST ${DRY:+(dry run)}"
rsync -av --delete $DRY \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude '__pycache__' \
  --exclude '.git' \
  --exclude '/guest_fish/' \
  --exclude '/guest_fish.json' \
  --exclude '/guest_fish.json.tmp' \
  --exclude '/tune_out/' \
  "$SRC" "$DEST"

if [ -n "$RESTART" ] && [ -z "$DRY" ]; then
  echo "==> restart aquarium bridge on pi-main"
  ssh pi-main 'sudo -n systemctl restart uochan-aquarium.service 2>/dev/null || (pkill -f aquarium_bridge.py; sleep 1; cd ~/Documents/aquarium/bridge && setsid -f /home/mine/Documents/fish_ai_realtime/.venv/bin/python aquarium_bridge.py < /dev/null > /tmp/aquarium_bridge.log 2>&1)'
  echo "==> restart aquarium web (upload_server) on pi-main"
  ssh pi-main 'sudo -n systemctl restart uochan-aquarium-web.service 2>/dev/null || (pkill -f upload_server.py; pkill -f "python3 -m http.server 8080"; sleep 1; cd ~/Documents/aquarium/web && setsid -f /home/mine/Documents/fish_ai_realtime/.venv/bin/python upload_server.py < /dev/null > /tmp/aquarium_web.log 2>&1)'
fi

echo "==> done"
