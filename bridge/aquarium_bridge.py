"""Aquarium WebSocket bridge.

fish_ai_realtime と upload_server が書く 3 ファイルを inotify で見張り、
水槽 UI 向けのイベントに翻訳して WebSocket で配信する。

監視対象:
  zone_state.json      : カメラ由来 (active_zone, recognized_person_id)
  aquarium_event.json  : realtime_loop 由来 (ai_state)
  guest_fish.json      : upload_server 由来 (お客さんがアップロードした魚)

イベント設計:
  [zone 由来]
    active_zone == "zone_003" 進入       → approach (owner フラグ付き)
    zone_003 に SPEAK_DWELL_SEC 滞在     → speak (1 訪問につき 1 回)
    zone_003 → 別ゾーン or null          → leave、その後 idle に収束
    それ以外                             → idle
  [aquarium_event 由来]
    ai_state: idle → speaking            → ai_speak_start
    ai_state: speaking → idle            → ai_speak_end
  [guest_fish 由来]
    fishes 配列に新規 ID 出現            → fish_added (id, image_url)
    既存 ID の owner_person_id 変化      → fish_owner_updated (id, owner_person_id)
    既存 ID が消失                       → fish_removed (id)
    クライアント接続時の welcome         → 既存全ての fish_added を送信

ブラウザ側 (sketch.js) で zone と AI の状態を分離して扱い、
AI 発話中は zone 状態に関わらず口パクが優先される。
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import websockets
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# ─── 設定読み込み ─────────────────────────────────────────
# 同梱の config.json を初期値に、環境変数 (AQUARIUM_*) で個別キーを上書き可能。
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _load_config():
    try:
        with CONFIG_PATH.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_cfg = _load_config()

ZONE_STATE_FILE = Path(
    os.environ.get("AQUARIUM_ZONE_STATE", _cfg.get("zone_state_path", ""))
)
AQUARIUM_EVENT_FILE = Path(
    os.environ.get("AQUARIUM_EVENT_PATH", _cfg.get("aquarium_event_path", ""))
)
_guest_fish_path_str = os.environ.get(
    "AQUARIUM_GUEST_FISH_PATH", _cfg.get("guest_fish_path", "")
)
GUEST_FISH_FILE = Path(_guest_fish_path_str) if _guest_fish_path_str else None
GUEST_FISH_URL_PREFIX = os.environ.get(
    "AQUARIUM_GUEST_FISH_URL_PREFIX", _cfg.get("guest_fish_url_prefix", "/guest_fish")
)
WS_HOST = os.environ.get("AQUARIUM_WS_HOST", _cfg.get("ws", {}).get("host", "0.0.0.0"))
WS_PORT = int(os.environ.get("AQUARIUM_WS_PORT", _cfg.get("ws", {}).get("port", 8765)))
AQUARIUM_ZONE_ID = os.environ.get(
    "AQUARIUM_ZONE_ID", _cfg.get("aquarium", {}).get("zone_id", "zone_003")
)
OWNER_PERSON_ID = os.environ.get(
    "AQUARIUM_OWNER_ID", _cfg.get("aquarium", {}).get("owner_person_id", "001")
)
SPEAK_DWELL_SEC = float(
    os.environ.get(
        "AQUARIUM_SPEAK_DWELL_SEC", _cfg.get("aquarium", {}).get("speak_dwell_sec", 5.0)
    )
)
LEAVE_LINGER_SEC = float(
    os.environ.get(
        "AQUARIUM_LEAVE_LINGER_SEC",
        _cfg.get("aquarium", {}).get("leave_linger_sec", 1.0),
    )
)

# ─── ロギング ────────────────────────────────────────────
_log_level = os.environ.get(
    "AQUARIUM_LOG_LEVEL", _cfg.get("logging", {}).get("level", "INFO")
).upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# zone-based 状態 (カメラ由来イベントの追跡)
zone_tracker = {
    "current_event": "idle",
    "in_zone_since": None,
    "speak_emitted": False,
    "owner_latched": False,
    "left_at": None,
}

# AI 発話状態 (aquarium_event.json 由来)
ai_tracker = {
    "is_speaking": False,
}

# ゲスト魚追跡 (guest_fish.json 由来、broadcast 済み ID と owner を覚える)
guest_fish_tracker = {
    "known_ids": set(),
    "owner_by_id": {},   # fish_id -> owner_person_id (or None)
    "image_by_id": {},   # fish_id -> image ファイル名 (差し替え検知用)
}

# 顔認識 owner present 追跡 (Phase 1.5 再来訪演出)
# zone_state.json の recognized_person_id が変化したら、
# その person_id に紐付く fish_ids 一覧を fish_owner_present として配信する。
owner_present_tracker = {
    "current_person_id": None,
}

clients: set = set()


def _load_json(path):
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compute_zone_event(zs):
    """zone_state.json の内容から zone 由来イベントを返す。"""
    now = time.time()
    active_zone = (zs or {}).get("active_zone")
    person_id = (zs or {}).get("recognized_person_id")
    is_owner = person_id == OWNER_PERSON_ID
    in_aquarium = active_zone == AQUARIUM_ZONE_ID

    t = zone_tracker
    if in_aquarium:
        if t["in_zone_since"] is None:
            t["in_zone_since"] = now
            t["speak_emitted"] = False
            t["owner_latched"] = is_owner
            t["left_at"] = None
            return ("approach", {"owner": is_owner})
        if is_owner and not t["owner_latched"]:
            t["owner_latched"] = True
        dwell = now - t["in_zone_since"]
        if dwell >= SPEAK_DWELL_SEC and not t["speak_emitted"]:
            t["speak_emitted"] = True
            return ("speak", {"owner": t["owner_latched"]})
        return (None, None)

    if t["in_zone_since"] is not None:
        owner = t["owner_latched"]
        t["in_zone_since"] = None
        t["speak_emitted"] = False
        t["owner_latched"] = False
        t["left_at"] = now
        return ("leave", {"owner": owner})

    if t["left_at"] is not None and (now - t["left_at"]) >= LEAVE_LINGER_SEC:
        t["left_at"] = None
        if t["current_event"] != "idle":
            return ("idle", {})
        return (None, None)

    if t["left_at"] is None and t["current_event"] != "idle":
        return ("idle", {})

    return (None, None)


def compute_ai_event(ae):
    """aquarium_event.json の内容から AI 由来イベントを返す。"""
    is_speaking = (ae or {}).get("ai_state") == "speaking"
    if is_speaking != ai_tracker["is_speaking"]:
        ai_tracker["is_speaking"] = is_speaking
        return ("ai_speak_start" if is_speaking else "ai_speak_end", {})
    return (None, None)


def _fish_to_payload(fish):
    """guest_fish.json の 1 エントリを WebSocket 配信用 payload に変換。"""
    return {
        "id": fish["id"],
        "image_url": f"{GUEST_FISH_URL_PREFIX}/{fish['image']}",
        "owner_person_id": fish.get("owner_person_id"),
    }


def compute_guest_fish_events(gf):
    """guest_fish.json から (added, updated, removed, replaced) を返す。

    - added:    新規 fish_id (fish_added 配信用 payload)
    - updated:  既存 fish_id の owner_person_id 変更 (fish_owner_updated 配信用)
    - removed:  既存 fish_id が消失 (fish_removed 配信用 {id})
    - replaced: 既存 fish_id の image 変更 (管理UIの画像差し替え/元に戻す)。
                fish_removed → fish_added の順で再配信し、走行中の水槽の絵を入れ替える

    Phase 1.5 で owner_by_id 追跡を追加: 既存 fish の owner_person_id が None → 設定値に
    変わったタイミングで fish_owner_updated を発火。realtime_loop の _link_fish_to_person
    や app.py の管理 UI が guest_fish.json を書き換えると watchdog で再評価されてここを通る。
    """
    fishes = (gf or {}).get("fishes", []) or []
    current_ids = set()
    added = []
    updated = []
    replaced = []
    for f in fishes:
        fid = f.get("id")
        if not fid:
            continue
        current_ids.add(fid)
        owner = f.get("owner_person_id")
        image = f.get("image")
        if fid not in guest_fish_tracker["known_ids"]:
            guest_fish_tracker["known_ids"].add(fid)
            guest_fish_tracker["owner_by_id"][fid] = owner
            guest_fish_tracker["image_by_id"][fid] = image
            added.append(_fish_to_payload(f))
        else:
            prev_owner = guest_fish_tracker["owner_by_id"].get(fid)
            if prev_owner != owner:
                guest_fish_tracker["owner_by_id"][fid] = owner
                updated.append({"id": fid, "owner_person_id": owner})
            prev_image = guest_fish_tracker["image_by_id"].get(fid)
            if prev_image != image:
                guest_fish_tracker["image_by_id"][fid] = image
                replaced.append(_fish_to_payload(f))
    removed_ids = guest_fish_tracker["known_ids"] - current_ids
    removed = [{"id": fid} for fid in removed_ids]
    for fid in removed_ids:
        guest_fish_tracker["known_ids"].discard(fid)
        guest_fish_tracker["owner_by_id"].pop(fid, None)
        guest_fish_tracker["image_by_id"].pop(fid, None)
    return added, updated, removed, replaced


def compute_owner_present_event(zs, gf):
    """recognized_person_id が変化したら fish_owner_present イベント payload を返す。

    payload: {person_id, fish_ids}。person_id が None / "" になっても発火させる
    (フロント側で前面化を解除するため)。変化なしなら None。
    """
    person_id = (zs or {}).get("recognized_person_id")
    if person_id == owner_present_tracker["current_person_id"]:
        return None
    owner_present_tracker["current_person_id"] = person_id
    fish_ids = []
    if person_id:
        for f in (gf or {}).get("fishes", []) or []:
            if f.get("owner_person_id") == person_id and f.get("id"):
                fish_ids.append(f["id"])
    return {"person_id": person_id, "fish_ids": fish_ids}


async def broadcast(event_type, payload):
    if not clients:
        return
    message = json.dumps({"type": event_type, "payload": payload})
    websockets.broadcast(clients, message)


async def evaluate_and_emit():
    """全監視ファイルを読み、変化があったイベントを送信する。"""
    zs = _load_json(ZONE_STATE_FILE)
    zone_type, zone_payload = compute_zone_event(zs)
    if zone_type is not None:
        zone_tracker["current_event"] = zone_type
        log.info("zone -> %s %s", zone_type, zone_payload)
        await broadcast(zone_type, zone_payload)

    ae = _load_json(AQUARIUM_EVENT_FILE)
    ai_type, ai_payload = compute_ai_event(ae)
    if ai_type is not None:
        log.info("ai -> %s %s", ai_type, ai_payload)
        await broadcast(ai_type, ai_payload)

    if GUEST_FISH_FILE is not None:
        gf = _load_json(GUEST_FISH_FILE)
        added, updated, removed, replaced = compute_guest_fish_events(gf)
        for payload in added:
            log.info("guest fish added -> %s", payload)
            await broadcast("fish_added", payload)
        for payload in updated:
            log.info("guest fish owner updated -> %s", payload)
            await broadcast("fish_owner_updated", payload)
        for payload in removed:
            log.info("guest fish removed -> %s", payload)
            await broadcast("fish_removed", payload)
        for payload in replaced:
            # 画像差し替え: 既存ハンドラだけで完結するよう remove → add で入れ替える
            log.info("guest fish image replaced -> %s", payload)
            await broadcast("fish_removed", {"id": payload["id"]})
            await broadcast("fish_added", payload)
        # Phase 1.5: 顔認識 person_id 変化に追従して紐付き魚の前面化トリガー
        owner_event = compute_owner_present_event(zs, gf)
        if owner_event is not None:
            log.info("fish_owner_present -> %s", owner_event)
            await broadcast("fish_owner_present", owner_event)


class FilesHandler(FileSystemEventHandler):
    """zone_state.json または aquarium_event.json の変更を検知して再評価をキック。"""

    def __init__(self, loop):
        self.loop = loop
        targets = {ZONE_STATE_FILE.resolve(), AQUARIUM_EVENT_FILE.resolve()}
        if GUEST_FISH_FILE is not None:
            try:
                targets.add(GUEST_FISH_FILE.resolve())
            except OSError:
                pass
        self._targets = targets

    def _is_target(self, path_str):
        try:
            return Path(path_str).resolve() in self._targets
        except OSError:
            return False

    def _kick(self):
        asyncio.run_coroutine_threadsafe(evaluate_and_emit(), self.loop)

    def on_modified(self, event):
        if not event.is_directory and self._is_target(event.src_path):
            self._kick()

    def on_created(self, event):
        if not event.is_directory and self._is_target(event.src_path):
            self._kick()

    def on_moved(self, event):
        dest = getattr(event, "dest_path", None)
        if dest and self._is_target(dest):
            self._kick()


async def ws_handler(websocket):
    clients.add(websocket)
    log.info("client connected (%d total)", len(clients))
    try:
        # 接続直後: 現在の zone state と (もし speaking 中なら) ai_speak_start を送る
        await websocket.send(
            json.dumps({"type": zone_tracker["current_event"], "payload": {}})
        )
        if ai_tracker["is_speaking"]:
            await websocket.send(
                json.dumps({"type": "ai_speak_start", "payload": {}})
            )
        # 既存のゲスト魚を全て fish_added として送る (新規クライアント向けの初期同期)
        if GUEST_FISH_FILE is not None:
            gf = _load_json(GUEST_FISH_FILE)
            for fish in (gf or {}).get("fishes", []) or []:
                if not fish.get("id"):
                    continue
                await websocket.send(
                    json.dumps({"type": "fish_added", "payload": _fish_to_payload(fish)})
                )
            # Phase 1.5: 現在認識されている owner がいれば前面化トリガーも送る
            cur_pid = owner_present_tracker["current_person_id"]
            if cur_pid:
                fish_ids = [
                    f["id"]
                    for f in (gf or {}).get("fishes", []) or []
                    if f.get("owner_person_id") == cur_pid and f.get("id")
                ]
                await websocket.send(
                    json.dumps({
                        "type": "fish_owner_present",
                        "payload": {"person_id": cur_pid, "fish_ids": fish_ids},
                    })
                )
        async for _ in websocket:
            pass
    except Exception:
        log.exception("ws_handler error")
    finally:
        clients.discard(websocket)
        log.info("client disconnected (%d total)", len(clients))


async def periodic_tick():
    """ファイル変更のないままドゥエル経過するケースを拾うための周期再評価。"""
    while True:
        await asyncio.sleep(1.0)
        await evaluate_and_emit()


async def main():
    loop = asyncio.get_running_loop()
    observer = Observer()
    handler = FilesHandler(loop)
    # zone_state と aquarium_event は同じディレクトリ (fish_ai_realtime)
    observer.schedule(handler, str(ZONE_STATE_FILE.parent), recursive=False)
    # guest_fish.json は別ディレクトリ (~/Documents/aquarium/) なので追加 schedule
    if (
        GUEST_FISH_FILE is not None
        and GUEST_FISH_FILE.parent != ZONE_STATE_FILE.parent
    ):
        GUEST_FISH_FILE.parent.mkdir(parents=True, exist_ok=True)
        observer.schedule(handler, str(GUEST_FISH_FILE.parent), recursive=False)
    observer.start()
    try:
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            log.info("WebSocket listening on ws://%s:%d", WS_HOST, WS_PORT)
            log.info("watching %s", ZONE_STATE_FILE)
            log.info("watching %s", AQUARIUM_EVENT_FILE)
            if GUEST_FISH_FILE is not None:
                log.info("watching %s", GUEST_FISH_FILE)
            await periodic_tick()
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    asyncio.run(main())
