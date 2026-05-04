#!/usr/bin/env bash
# Mac の ~/Program/aquarium/ を pi-main の ~/Documents/aquarium/ に一方向同期する。
#
# 使い方:
#   bash ~/Program/aquarium/bridge/deploy.sh           # 通常の同期
#   bash ~/Program/aquarium/bridge/deploy.sh --dry     # rsync の予行 (実変更なし)
#   bash ~/Program/aquarium/bridge/deploy.sh --restart # 同期後に bridge を再起動
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
  "$SRC" "$DEST"

if [ -n "$RESTART" ] && [ -z "$DRY" ]; then
  echo "==> restart aquarium bridge on pi-main"
  ssh pi-main 'sudo systemctl restart uochan-aquarium.service 2>/dev/null || (pkill -f aquarium_bridge.py; sleep 1; cd ~/Documents/aquarium/bridge && setsid -f /home/mine/Documents/fish_ai_realtime/.venv/bin/python aquarium_bridge.py < /dev/null > /tmp/aquarium_bridge.log 2>&1)'
fi

echo "==> done"
