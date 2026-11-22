#!/usr/bin/env python3
"""65吋テレビの画面を閉店後に暗くする (2026-08-21)。

閉店 (18:30) を過ぎてお客様がいなくなったら画面を暗くし、深夜 23:00 は誰が
居ようと暗くする。朝は uochan-nightly-reboot.timer の再起動で labwc autostart
が戻す。

**「電源を切る」ことはできない。** このテレビ (Xiaomi/Google TV) は CEC の
電源オフを一切受け付けなかった (tv_off のコメントに実測を記載)。できるのは
HDMI の信号を止めて画面を暗くすることまで。本当に電源を落としたいなら
テレビ側の CEC 設定を入れるか、スマートプラグを足す (どちらも現地作業)。

人がいるかの材料は 2 つ。どちらも「動いているか」を先に確かめてから使う:

  zone_state.json  … pi-main の cam_0 (camera_zone_check が書く)
  world_state.json … pi-aicam = 左脳の在店人数 (leftbrain_rx が書く)

**両方とも止まっていたら消さない。** 2026-08-21 に左脳が 12 日・カメラが 8 日
止まっていた実績があり、「無人」と「見えていない」を取り違えると営業中に
テレビを消してしまう。23:00 の強制消灯だけは材料を見ずに実行する。

左脳の鮮度は received_at で測ってはいけない。leftbrain_rx はパケットが 1 つも
来なくても received_at を今の時刻で書き直すので、止まっていても新しく見える。
左脳自身が刻む state.ts / state.frame が進んだかどうかで判定する
(pi-aicam と pi-main の時計がずれていても効く)。
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

# ── 設定 ────────────────────────────────────────────────
CLOSE_AT      = dtime(18, 30)   # これ以降、無人なら消す
FORCE_OFF_AT  = dtime(23, 0)    # これ以降は無条件で消す (朝の再起動まで点けない)
MORNING_AT    = dtime(8, 0)     # 再起動でテレビが点く時刻 (uochan-nightly-reboot.timer)

EMPTY_MINUTES = 10.0            # 何分続けて無人なら消すか (検出のちらつき対策)
CAM_FRESH_SEC = 120.0           # zone_state.json がこれより古ければカメラは止まっている

# 消灯後、閉店〜23:00 に人が来たら点け直すか。
# 2026-08-21: **False で運用する。** テレビの電源の入切はスタッフがリモコンで
# 行うことになったため、機械が勝手に点け直すとスタッフの操作と喧嘩する
# (tv_on は CEC の image-view-on を送るので、スタッフが切った TV を起こせてしまう)。
# 一往復の手順自体は同日に実機検証済みなので、方針が変われば True に戻せる。
WAKE_ON_PRESENCE = False

CEC_DEV   = "/dev/cec1"         # 65吋 (HDMI-A-2, phys 3.0.0.0)。/dev/cec0 は LG
TV_OUTPUT = "HDMI-A-2"
TV_PHYS   = "3.0.0.0"

FISH_DIR   = Path.home() / "Documents" / "fish_ai_realtime"
ZONE_FILE  = FISH_DIR / "zone_state.json"
WORLD_FILE = FISH_DIR / "world_state.json"
KIOSK_SH   = Path.home() / "Documents" / "aquarium" / "kiosk_launch.sh"
STATE_FILE = Path.home() / ".cache" / "uochan_tv_guard.json"


def log(msg):
    print(f"[tv_guard] {msg}", flush=True)


# ── 状態の持ち越し ──────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log(f"状態の保存に失敗: {e}")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── 人がいるか ──────────────────────────────────────────
def camera_view(now):
    """(動いているか, 人が映っているか) を返す。pi-main 自身が書くので時刻比較でよい。"""
    d = read_json(ZONE_FILE)
    if not d:
        return False, False
    alive = (now - float(d.get("updated_at") or 0)) <= CAM_FRESH_SEC
    if not alive:
        return False, False
    person = False
    for node in (d.get("nodes") or {}).values():
        # 退役した cam_1 は updated_at が古いまま残るので、node ごとに鮮度を見る
        if (now - float(node.get("updated_at") or 0)) <= CAM_FRESH_SEC and node.get("person"):
            person = True
    return True, person


def leftbrain_view(prev):
    """(動いているか, 人がいるか, 次回に持ち越す印) を返す。

    印 = 左脳が刻む ts と frame。前回から進んでいれば生きている。時計に依存しない。
    """
    d = read_json(WORLD_FILE)
    state = (d or {}).get("state") or {}
    mark = f"{state.get('ts')}/{state.get('frame')}"
    if not d or not state:
        return False, False, mark
    if prev is None:          # 初回は判断材料がないので「不明」に倒す
        return False, False, mark
    alive = (mark != prev)
    count = state.get("count") or 0
    return alive, (alive and count > 0), mark


# ── テレビ ──────────────────────────────────────────────
def cec(args, timeout=10):
    try:
        return subprocess.run(["cec-ctl", "-d", CEC_DEV] + args,
                              capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log(f"cec-ctl 失敗: {e}")
        return None


def cec_power():
    """TV が CEC で答える電源状態。参考値。

    このテレビは電源オフを受け付けないので、消していても常に "on" と答える。
    画面が出ているかの判断には使えない (screen_on を使うこと)。
    """
    r = cec(["--to", "0", "--give-device-power-status"])
    if not r:
        return None
    m = re.search(r"pwr-state:\s*(\S+)", r.stdout)
    return ("on" if m.group(1) == "on" else "standby") if m else None


def screen_on():
    """65吋に映像が出ているか。True / False / None(分からない)。

    判断材料は HDMI 出力が有効かどうか。CEC の pwr-state は当てにならない。
    """
    r = wlr([])
    if not r or not r.stdout:
        return None
    in_block = False
    for line in r.stdout.splitlines():
        if not line[:1].isspace():                     # 出力名の行 (インデントなし)
            in_block = line.startswith(TV_OUTPUT)
            continue
        if in_block and "Enabled:" in line:
            return line.split("Enabled:")[1].strip().lower() in ("yes", "true")
    return None                                        # TV が繋がっていない


def tv_off():
    """画面を暗くする。

    2026-08-21 実測: **この Google TV は CEC の電源オフを一切受け付けない。**
    STANDBY(0x36) / ui-cmd=power-off-function / power-toggle-function の3通りとも
    送信は成功する (Transmit ... STANDBY と出る) のに pwr-state は on のまま。
    一方 CEC の「起こす」(image-view-on) と操作キーは効く。テレビ側の設定で
    CEC の電源連動が切れているためと思われる。

    そこで HDMI の信号を止めて画面を暗くする。俯瞰カメラ実測で 65吋エリアの
    輝度が 121 → 34 に落ちる (暗い青の無信号画面)。バックライトは消えないので
    完全な消灯ではない。本当に電源を切るなら TV 側の CEC 設定を入れるか
    スマートプラグが要る (現地作業)。
    """
    cec(["--to", "0", "--standby"])          # 将来 TV 側の設定が入れば効く。無害
    wlr(["--output", TV_OUTPUT, "--off"])
    log("テレビの画面を暗くしました (HDMI信号停止)")


def wlr(args):
    try:
        return subprocess.run(["wlr-randr"] + args, capture_output=True,
                              text=True, timeout=15)
    except Exception as e:
        log(f"wlr-randr 失敗: {e}")
        return None


def tv_on():
    """画面を戻す。2026-08-21 に実機で一往復を検証済みの手順。

    CEC で起こす (TV が無信号で自分から寝た場合に要る) → HDMI-A-2 を有効化して
    1080p に固定 → **kiosk を上げ直す**。

    最後の kiosk 上げ直しは省略できない。実測すると、出力を戻しただけでは
    65吋にデスクトップの壁紙が出て**大水槽は LG 側に残ったまま**だった
    (tank65 の配置ルールは窓が map される時にしか効かないため)。上げ直したら
    水槽が正しく 65吋に戻ることを俯瞰カメラで確認済み。
    """
    cec(["--playback", "-S"])
    time.sleep(2)
    cec(["--to", "0", "--image-view-on"])
    time.sleep(1)
    cec(["--active-source", f"phys-addr={TV_PHYS}"])

    for _ in range(30):                       # HDMI-A-2 が出てくるのを待つ
        r = wlr([])
        if r and re.search(rf"^{TV_OUTPUT}", r.stdout, re.M):
            time.sleep(2)                     # EDID/モード列挙が落ち着くのを待つ
            # --on を必ず付ける (信号停止で消したときは Enabled:no のまま残るため)
            wlr(["--output", TV_OUTPUT, "--on",
                 "--pos", "1920,0", "--mode", "1920x1080@60"])
            break
        time.sleep(1)
    else:
        log("テレビを起こしたが HDMI-A-2 が現れなかった")
        return

    # kiosk_launch.sh は再実行で旧常駐を退場させる作り (一人っ子制御)
    try:
        subprocess.Popen([str(KIOSK_SH)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log(f"kiosk の上げ直しに失敗: {e}")
    log("テレビを点けました (1080p 固定 + kiosk 上げ直し)")


# ── 本体 ────────────────────────────────────────────────
def main():
    now_dt = datetime.now()
    now = time.time()
    t = now_dt.time()
    st = load_state()

    # 23:00〜08:00 は無条件で消灯。材料は見ない
    if t >= FORCE_OFF_AT or t < MORNING_AT:
        if screen_on():
            log("深夜のため画面を暗くします")
            tv_off()
        st["empty_since"] = None
        save_state(st)
        return

    # 08:00〜18:30 は営業時間。何もしない (朝の再起動で点いている)
    if t < CLOSE_AT:
        st["empty_since"] = None
        save_state(st)
        return

    # 18:30〜23:00
    cam_alive, cam_person = camera_view(now)
    lb_alive, lb_person, mark = leftbrain_view(st.get("lb_mark"))
    st["lb_mark"] = mark

    if not (cam_alive or lb_alive):
        # 目が両方とも死んでいる。無人なのか見えていないだけなのか分からないので消さない
        log("カメラも左脳も止まっています。判断できないので消しません")
        st["empty_since"] = None
        save_state(st)
        return

    present = cam_person or lb_person
    lit = screen_on()

    if present:
        st["empty_since"] = None
        if lit is False and WAKE_ON_PRESENCE:
            log("閉店後にお客様を検知したので画面を戻します")
            tv_on()
        save_state(st)
        return

    since = st.get("empty_since")
    if not since:
        st["empty_since"] = now
        save_state(st)
        return

    empty_min = (now - float(since)) / 60.0
    if empty_min >= EMPTY_MINUTES and lit:
        log(f"{empty_min:.0f}分間だれもいないので消します "
            f"(カメラ={'生' if cam_alive else '停'} 左脳={'生' if lb_alive else '停'})")
        tv_off()
    save_state(st)


def status():
    """いま何が見えているかを表示するだけ。テレビには触らない (人手の確認用)。"""
    now = time.time()
    st = load_state()
    cam_alive, cam_person = camera_view(now)
    lb_alive, lb_person, mark = leftbrain_view(st.get("lb_mark"))
    print(f"いまの時刻      : {datetime.now():%H:%M:%S}")
    print(f"カメラ (cam_0)  : {'動いている' if cam_alive else '止まっている'} / "
          f"人は{'いる' if cam_person else 'いない'}")
    print(f"左脳 (pi-aicam) : {'動いている' if lb_alive else '止まっている/判定待ち'} / "
          f"人は{'いる' if lb_person else 'いない'}")
    lit = screen_on()
    print(f"65吋の画面      : {'映っている' if lit else ('暗い' if lit is False else '繋がっていない')}"
          f"  (CECの申告={cec_power() or '返事なし'})")
    since = st.get("empty_since")
    if since:
        print(f"無人が続いて    : {(now - float(since)) / 60.0:.1f} 分")
    # 左脳の印は次回比較のために更新しておく
    st["lb_mark"] = mark
    save_state(st)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if arg == "--status":
            status()
        elif arg == "--off":
            tv_off()
        elif arg == "--on":
            tv_on()
        else:
            main()
    except Exception as e:
        log(f"想定外の失敗: {e}")
        sys.exit(1)
