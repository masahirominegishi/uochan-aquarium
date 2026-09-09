"""Aquarium upload server (Phase 1).

aiohttp で aquarium UI 静的配信 + お客さんがアップロードする魚画像の受け口を兼ねる
HTTP サーバー。pi-main の旧 `python3 -m http.server` を置き換える。

エンドポイント:
  GET  /                    aquarium UI (index.html)
  GET  /upload              お客さん向けアップロードフォーム HTML
  POST /api/upload          画像受信 → HSV 閾値で背景除去 → 保存 → guest_fish.json 追記
  POST /sales               カフェキオスクの会計の控え → stats/sales-YYYYMMDD.jsonl 追記
  GET  /guest_fish/{name}   切り抜き済みゲスト魚画像配信
  GET  /<その他>            STATIC_ROOT 配下の静的ファイル (sketch.js, fish.js, assets/...)

データの流れ:
  upload → guest_fish/{id}.png 保存
        → guest_fish.json 追記 (in-place)
        → bridge が watchdog で検知 → WebSocket で fish_added を配信
        → ブラウザ (sketch.js) が GuestFish として水槽に追加
"""

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web
from PIL import Image

from guest_fish_pipeline import append_fish, crop_to_paper, new_fish_id, remove_white_background


# ─── 設定 ─────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


def _load_config():
    try:
        with CONFIG_PATH.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_cfg = _load_config()


def _env_or(key, default):
    v = os.environ.get(key)
    return v if v is not None else default


def _resolve_path(rel_or_abs):
    """相対パスは web/ 基準で解決、絶対パスはそのまま。"""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = (HERE / p).resolve()
    return p


HOST = _env_or("AQUARIUM_WEB_HOST", _cfg.get("http", {}).get("host", "0.0.0.0"))
PORT = int(_env_or("AQUARIUM_WEB_PORT", _cfg.get("http", {}).get("port", 8080)))

STATIC_ROOT = _resolve_path(
    _env_or("AQUARIUM_WEB_STATIC_ROOT", _cfg.get("static_root", ".."))
)
GUEST_FISH_DIR = _resolve_path(
    _env_or("AQUARIUM_GUEST_FISH_DIR", _cfg.get("guest_fish_dir", "../guest_fish"))
)
GUEST_FISH_PATH = _resolve_path(
    _env_or("AQUARIUM_GUEST_FISH_PATH", _cfg.get("guest_fish_path", "../guest_fish.json"))
)
# Phase 1.5: realtime_loop に「飼い主登録しますか？」を聞かせるためのトリガーファイル。
# upload_server がアップロード成功時に書き、realtime_loop が次の zone_and_greet_loop tick
# で読み取って消費する。Phase 2/3 では廃止される (realtime_loop 内から直接呼ぶため)。
PENDING_OWNER_LINK_PATH = _resolve_path(
    _env_or(
        "AQUARIUM_PENDING_OWNER_LINK_PATH",
        _cfg.get(
            "pending_owner_link_path",
            "/home/mine/Documents/fish_ai_realtime/pending_owner_link.json",
        ),
    )
)

# カフェキオスク (paypay-kiosk) が起動時に商品リストを取得する読み取り専用配信元。
# item.json は UI 編集データ (uochan-ui-data) なので STATIC_ROOT 外にあり、
# static_handler の path traversal ガードを通れないため専用ルートで出す。
ITEM_JSON_PATH = _resolve_path(
    _env_or(
        "AQUARIUM_ITEM_JSON_PATH",
        _cfg.get("item_json_path", "/home/mine/Documents/fish_ai_realtime/item.json"),
    )
)
# 事業者マスタ (キオスクの会計単位) と商品写真。item.json と同じく UI 編集データなので
# STATIC_ROOT 外にあり、専用ルートで出す。
VENDOR_JSON_PATH = _resolve_path(
    _env_or(
        "AQUARIUM_VENDOR_JSON_PATH",
        _cfg.get("vendor_json_path", "/home/mine/Documents/fish_ai_realtime/vendors.json"),
    )
)
ITEM_IMAGE_DIR = _resolve_path(
    _env_or(
        "AQUARIUM_ITEM_IMAGE_DIR",
        _cfg.get("item_image_dir", "/home/mine/Documents/fish_ai_realtime/item_images"),
    )
)
# キオスク表示ジャンル (タブ)。item.json と同じく UI 編集データなので専用ルートで出す。
GENRE_JSON_PATH = _resolve_path(
    _env_or(
        "AQUARIUM_GENRE_JSON_PATH",
        _cfg.get("genre_json_path", "/home/mine/Documents/fish_ai_realtime/kiosk_genres.json"),
    )
)

# カフェキオスク (paypay-kiosk) の会計の控えの置き場。来店統計 (occupancy/visitors)
# と同じ fish_ai_realtime/stats/ に日ごとの jsonl で1行ずつ足す。読むのは管理UIの
# 「売上」画面。ここは追記だけで、書き換えも削除もしない。
SALES_DIR = _resolve_path(
    _env_or(
        "AQUARIUM_SALES_DIR",
        _cfg.get("sales_dir", "/home/mine/Documents/fish_ai_realtime/stats"),
    )
)

V_THRESH = int(_cfg.get("background_removal", {}).get("value_threshold", 200))
S_THRESH = int(_cfg.get("background_removal", {}).get("saturation_threshold", 30))
LONG_EDGE = int(_cfg.get("output", {}).get("long_edge", 600))
MAX_BYTES = int(_cfg.get("upload", {}).get("max_bytes", 25 * 1024 * 1024))

_log_level = _env_or("AQUARIUM_WEB_LOG_LEVEL", _cfg.get("logging", {}).get("level", "INFO")).upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("upload_server")


_metadata_lock = asyncio.Lock()


# ─── ハンドラ ──────────────────────────────────────────
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


async def upload_form_handler(request: web.Request) -> web.Response:
    return web.Response(text=UPLOAD_HTML, content_type="text/html", charset="utf-8")


async def guest_fish_image_handler(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not _SAFE_NAME_RE.match(name):
        return web.Response(status=400, text="invalid name")
    p = GUEST_FISH_DIR / name
    if not p.exists() or not p.is_file():
        return web.Response(status=404)
    return web.FileResponse(p)


async def item_json_handler(request: web.Request) -> web.Response:
    if not ITEM_JSON_PATH.is_file():
        return web.Response(status=404)
    resp = web.FileResponse(ITEM_JSON_PATH)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def vendor_json_handler(request: web.Request) -> web.Response:
    """事業者マスタ。未作成なら 404 → キオスクは前回の vendors-data.js のまま起動する。"""
    if not VENDOR_JSON_PATH.is_file():
        return web.Response(status=404)
    resp = web.FileResponse(VENDOR_JSON_PATH)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def genre_json_handler(request: web.Request) -> web.Response:
    """キオスク表示ジャンル。未作成なら 404 → キオスクは前回の genres-data.js のまま起動する。"""
    if not GENRE_JSON_PATH.is_file():
        return web.Response(status=404)
    resp = web.FileResponse(GENRE_JSON_PATH)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def item_image_handler(request: web.Request) -> web.Response:
    """商品写真。item.json の image (ファイル名のみ) をキオスクがこの URL で引く。"""
    name = request.match_info["name"]
    if not _SAFE_NAME_RE.match(name):
        return web.Response(status=400, text="invalid name")
    p = ITEM_IMAGE_DIR / name
    if not p.exists() or not p.is_file():
        return web.Response(status=404)
    return web.FileResponse(p)


# ─── 店員呼び出し (カフェキオスク「店員を呼ぶ」ボタン, 2026-07-26) ─────────
# キオスクが POST してくると、うおちゃんの声の作り置き音声 (sound/staff_call.wav,
# OpenAI TTS coral で生成した「店員さーん、対応お願いします」) を店内スピーカー
# (既定シンク = uorear = 水槽/カフェ側) から paplay で再生する。
# うおちゃんがサブ水槽で接客中 (zone_state.active_zone == zone_003, 会話ラッチ込み)
# のときは自分の接客と声が重なるので鳴らさない → キオスク側のチャイムのみ。
ZONE_STATE_PATH = _resolve_path(
    _env_or(
        "AQUARIUM_ZONE_STATE",
        _cfg.get("zone_state_path", "/home/mine/Documents/fish_ai_realtime/zone_state.json"),
    )
)
STAFF_CALL_WAV = _resolve_path(
    _env_or("AQUARIUM_STAFF_CALL_WAV", _cfg.get("staff_call_wav", "../sound/staff_call.wav"))
)
STAFF_CALL_ZONE = _env_or("AQUARIUM_ZONE_ID", _cfg.get("staff_call_zone", "zone_003"))
_staff_call_state = {"last": 0.0}   # 連打デバウンス (monotonic)


def _fish_at_sub_tank() -> bool:
    """うおちゃんがサブ水槽 (会話側) に居るか。判定不能時は False (=鳴らす) に倒す。"""
    try:
        with ZONE_STATE_PATH.open() as f:
            state = json.load(f)
        return state.get("active_zone") == STAFF_CALL_ZONE
    except Exception:
        return False


async def staff_call_handler(request: web.Request) -> web.Response:
    now = time.monotonic()
    if now - _staff_call_state["last"] < 4.0:
        return web.json_response({"ok": True, "voice": False, "reason": "debounce"})
    _staff_call_state["last"] = now
    if _fish_at_sub_tank():
        log.info("[staff_call] うおちゃん接客中 → 声はスキップ (キオスクのチャイムのみ)")
        return web.json_response({"ok": True, "voice": False, "reason": "uochan_busy"})
    if not STAFF_CALL_WAV.is_file():
        log.warning("[staff_call] 音声ファイルが無い: %s", STAFF_CALL_WAV)
        return web.json_response({"ok": False, "voice": False, "reason": "no_wav"}, status=500)
    # systemd 配下は XDG_RUNTIME_DIR が無く paplay が PipeWire に繋がらないので補う
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    subprocess.Popen(["paplay", str(STAFF_CALL_WAV)], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("[staff_call] 呼び出し音声を再生")
    return web.json_response({"ok": True, "voice": True})


# ─── 売上の控え (カフェキオスクのセルフレジ, 2026-09-09) ──────────────────
# キオスクは会計が終わったとき・途中でやめたときに、その内容をここへ POST する。
# 受け取ったものを日ごとのファイルに1行足すだけ。
#
# ★これは「お客様が会計を終えたと押した金額」であって PayPay の入金実績ではない。
#   固定QR + 客が金額を手入力する方式なので、打ち間違い・払わずに帰るが起こりうる。
#   だから途中でやめた会計 (status=aborted) も消さずに残し、あとで PayPay 側の
#   明細と突き合わせたときに差の理由を追えるようにしてある。
#
# キオスクは届かなかった控えを自分の中に貯めて後で送り直してくる (取りこぼし防止)。
# 同じ order_id が二度来たら二重計上しないよう、その日のファイルを見て弾く。
_SALES_STATUS = ("completed", "aborted")
_SALES_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SALES_MAX_LINES = 100
_SALES_CORS = {"Access-Control-Allow-Origin": "*",
               "Access-Control-Allow-Headers": "Content-Type",
               "Access-Control-Allow-Methods": "POST, OPTIONS"}


def _sales_day(client_ts: str) -> str:
    """控えを入れる日。原則キオスクの時計 (=会計した瞬間) で決める。

    貯めてから送ってくることがあるので受信時刻では決めない。ただし時計が
    狂っている (今から7日以上ずれる) ときだけサーバーの日付に倒す。
    """
    now = time.time()
    try:
        t = datetime.fromisoformat(str(client_ts).replace("Z", "+00:00")).timestamp()
        if abs(now - t) <= 7 * 86400:
            return time.strftime("%Y%m%d", time.localtime(t))
    except Exception:
        pass
    return time.strftime("%Y%m%d", time.localtime(now))


def _sales_str(v, limit: int) -> str:
    return str(v if v is not None else "")[:limit]


def _normalize_sale(body):
    """(控えの行, エラー文) を返す。壊れた控えは受け取らない (400)。"""
    if not isinstance(body, dict):
        return None, "オブジェクトではありません"
    order_id = _sales_str(body.get("order_id"), 64).strip()
    if not _SALES_ORDER_ID_RE.match(order_id):
        return None, "order_id が不正です"
    status = body.get("status")
    if status not in _SALES_STATUS:
        return None, "status は completed / aborted のどちらかです"

    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        return None, "明細が空です"
    if len(raw_lines) > _SALES_MAX_LINES:
        return None, "明細が多すぎます"
    lines = []
    for ln in raw_lines:
        if not isinstance(ln, dict):
            return None, "明細が不正です"
        try:
            qty = int(ln["qty"])
            unit_price = int(ln["unit_price"])
        except Exception:
            return None, "明細の個数/単価が不正です"
        if qty <= 0 or unit_price < 0:
            return None, "明細の個数/単価が不正です"
        lines.append({
            "item_id": _sales_str(ln.get("item_id"), 64),
            "name": _sales_str(ln.get("name"), 200),
            "vendor_id": _sales_str(ln.get("vendor_id"), 64),
            "vendor_name": _sales_str(ln.get("vendor_name"), 100),
            "unit_price": unit_price,     # 会計した瞬間の値段を控える
            "qty": qty,                   # (あとで値段を変えても過去の売上が動かないように)
        })

    rounds = []
    for r in (body.get("rounds") or []):
        if not isinstance(r, dict):
            return None, "会計単位が不正です"
        try:
            amount = int(r["amount"])
        except Exception:
            return None, "会計単位の金額が不正です"
        if amount < 0:
            return None, "会計単位の金額が不正です"
        rounds.append({
            "vendor_id": _sales_str(r.get("vendor_id"), 64),
            "vendor_name": _sales_str(r.get("vendor_name"), 100),
            "amount": amount,
            "done": bool(r.get("done")),   # 客が「会計できました」を押したか
        })

    row = {
        "order_id": order_id,
        "status": status,
        "client_ts": _sales_str(body.get("ts"), 40),   # キオスクの時計 (会計した瞬間)
        "server_ts": round(time.time(), 3),            # pi-main が受け取った時刻
        "lines": lines,
        "rounds": rounds,
        "total": sum(ln["unit_price"] * ln["qty"] for ln in lines),
        "paid_total": sum(r["amount"] for r in rounds if r["done"]),
        "source": "kiosk",
    }
    return row, None


def _sales_already_recorded(path: Path, order_id: str) -> bool:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("order_id") == order_id:
                        return True
                except Exception:
                    continue
    except FileNotFoundError:
        return False
    return False


async def sales_options_handler(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_SALES_CORS)


async def sales_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON として読めません"},
                                 status=400, headers=_SALES_CORS)
    row, err = _normalize_sale(body)
    if err:
        log.warning("[sales] 受け取れない控え: %s", err)
        return web.json_response({"error": err}, status=400, headers=_SALES_CORS)

    day = _sales_day(row["client_ts"])
    path = SALES_DIR / f"sales-{day}.jsonl"
    try:
        SALES_DIR.mkdir(parents=True, exist_ok=True)
        if _sales_already_recorded(path, row["order_id"]):
            # 送り直し。控えは既にある = キオスクには成功として返す (溜めたぶんを消させる)
            log.info("[sales] 送り直し %s (記録済み)", row["order_id"])
            return web.json_response({"ok": True, "day": day, "duplicate": True},
                                     headers=_SALES_CORS)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("[sales] 書けなかった %s: %s", path, e)
        return web.json_response({"error": "保存に失敗しました"},
                                 status=500, headers=_SALES_CORS)

    log.info("[sales] %s %s 合計%d円 (受取済%d円) 明細%d件",
             day, row["status"], row["total"], row["paid_total"], len(row["lines"]))
    return web.json_response({"ok": True, "day": day, "duplicate": False},
                             headers=_SALES_CORS)


async def api_upload_handler(request: web.Request) -> web.Response:
    if request.content_length and request.content_length > MAX_BYTES:
        return web.json_response({"error": "file too large"}, status=413)

    reader = await request.multipart()
    file_field = None
    async for field in reader:
        if field.name == "image":
            file_field = field
            break
    if file_field is None:
        return web.json_response({"error": "image フィールドがありません"}, status=400)

    raw = await file_field.read(decode=False)
    if not raw:
        return web.json_response({"error": "empty file"}, status=400)
    if len(raw) > MAX_BYTES:
        return web.json_response({"error": "file too large"}, status=413)

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        log.warning("画像デコード失敗: %s", e)
        return web.json_response({"error": "画像として読めませんでした"}, status=400)

    loop = asyncio.get_running_loop()

    def _process(im):
        cropped, _ = crop_to_paper(im, margins=0)   # 周囲の余白/机を落として紙面に (アップロード写真は歪み補正の負 margin 不要なので 0)
        return remove_white_background(cropped, v_thresh=V_THRESH, s_thresh=S_THRESH, long_edge=LONG_EDGE)

    try:
        processed = await loop.run_in_executor(None, lambda: _process(img))
    except Exception as e:
        log.exception("切り抜き失敗")
        return web.json_response({"error": f"処理失敗: {e}"}, status=500)

    fish_id = new_fish_id()
    out_name = f"{fish_id}.png"
    GUEST_FISH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GUEST_FISH_DIR / out_name
    try:
        processed.save(out_path, "PNG")
    except Exception as e:
        log.exception("画像保存失敗")
        return web.json_response({"error": f"保存失敗: {e}"}, status=500)

    # 切り抜き失敗時の救済用に、切り抜き前の元画像も guest_fish/raw/ に残す (失敗しても登録は続行)
    try:
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get((img.format or "").upper(), "jpg")
        raw_dir = GUEST_FISH_DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{fish_id}.{ext}").write_bytes(raw)
    except Exception as e:
        log.warning("元画像 (raw) 保存失敗・登録は続行: %s", e)

    async with _metadata_lock:
        append_fish(GUEST_FISH_PATH, fish_id, out_name)

    # Phase 1.5: realtime_loop に「飼い主登録しますか？」のトリガーを渡す。
    # 失敗してもアップロード自体は成功扱いにする (realtime_loop が落ちていても水槽には魚が出る)。
    try:
        PENDING_OWNER_LINK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_OWNER_LINK_PATH.with_suffix(PENDING_OWNER_LINK_PATH.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {"fish_id": fish_id, "uploaded_at": time.time()},
                f,
                ensure_ascii=False,
            )
        os.replace(tmp, PENDING_OWNER_LINK_PATH)
        log.info("pending_owner_link written: %s", fish_id)
    except Exception as e:
        log.warning("pending_owner_link write failed: %s", e)

    log.info("guest fish uploaded: %s (%d bytes raw)", fish_id, len(raw))
    return web.json_response(
        {"ok": True, "id": fish_id, "image_url": f"/guest_fish/{out_name}"}
    )


async def static_handler(request: web.Request) -> web.Response:
    """STATIC_ROOT 配下の静的ファイル配信。tail が空なら index.html。"""
    tail = request.match_info.get("tail", "")
    if not tail:
        target = STATIC_ROOT / "index.html"
    else:
        # path traversal 防止: 正規化して STATIC_ROOT 配下に収まることを確認
        candidate = (STATIC_ROOT / tail).resolve()
        try:
            candidate.relative_to(STATIC_ROOT)
        except ValueError:
            return web.Response(status=400, text="bad path")
        target = candidate
    if not target.exists() or not target.is_file():
        return web.Response(status=404)
    resp = web.FileResponse(target)
    # kiosk(chromium) が古い JS/HTML をヒューリスティックキャッシュで握り続け、
    # 再起動しても再検証せず古い版を使う問題への対策。no-cache で毎回再検証させる
    # (FileResponse の ETag/Last-Modified で未変更なら 304 が返るので帯域コストは小さい)。
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ─── アップロードフォーム HTML ─────────────────────────
UPLOAD_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>うおちゃん水槽 - 魚を投入</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 480px; margin: 0 auto; padding: 16px; line-height: 1.5; }
  h1 { font-size: 20px; }
  p { color: #555; }
  input[type=file] { font-size: 16px; margin: 12px 0; display: block; }
  button { font-size: 18px; padding: 12px 24px; cursor: pointer; border: none; border-radius: 6px; background: #2a7; color: white; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #preview { max-width: 100%; max-height: 320px; display: block; margin: 12px 0; border: 1px solid #ccc; border-radius: 4px; }
  #status { margin-top: 16px; padding: 12px; border-radius: 4px; min-height: 1em; }
  #status.ok { background: #dfd; color: #060; }
  #status.err { background: #fdd; color: #600; }
  #status.busy { background: #ffd; color: #660; }
</style>
</head>
<body>
<h1>魚を水槽に投入</h1>
<p>白い紙に魚の絵を描いて、写真を撮ってください。<br>白以外の色 (色鉛筆・ペン) で描かれた部分が切り抜かれて、水槽を泳ぎます。</p>
<input type="file" id="file" accept="image/*">
<img id="preview" hidden alt="preview">
<button id="submit" disabled>水槽に投入</button>
<div id="status"></div>
<script>
const fileEl = document.getElementById('file');
const submitEl = document.getElementById('submit');
const previewEl = document.getElementById('preview');
const statusEl = document.getElementById('status');

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || '';
}

fileEl.addEventListener('change', () => {
  const f = fileEl.files[0];
  if (!f) { submitEl.disabled = true; previewEl.hidden = true; return; }
  previewEl.src = URL.createObjectURL(f);
  previewEl.hidden = false;
  submitEl.disabled = false;
  setStatus('');
});

submitEl.addEventListener('click', async () => {
  const f = fileEl.files[0];
  if (!f) return;
  submitEl.disabled = true;
  setStatus('処理中...', 'busy');
  const fd = new FormData();
  fd.append('image', f, f.name);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (r.ok) {
      setStatus('あなたの魚が水槽に泳いでいます！', 'ok');
    } else {
      setStatus('エラー: ' + (j.error || r.statusText), 'err');
      submitEl.disabled = false;
    }
  } catch (e) {
    setStatus('送信エラー: ' + e.message, 'err');
    submitEl.disabled = false;
  }
});
</script>
</body>
</html>
"""


# ─── アプリ組み立て ────────────────────────────────────
def make_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BYTES + 1024 * 1024)
    app.router.add_post("/api/upload", api_upload_handler)
    app.router.add_post("/staff_call", staff_call_handler)
    app.router.add_post("/sales", sales_handler)
    app.router.add_options("/sales", sales_options_handler)
    app.router.add_get("/upload", upload_form_handler)
    app.router.add_get("/guest_fish/{name}", guest_fish_image_handler)
    app.router.add_get("/item.json", item_json_handler)
    app.router.add_get("/vendors.json", vendor_json_handler)
    app.router.add_get("/kiosk_genres.json", genre_json_handler)
    app.router.add_get("/item_images/{name}", item_image_handler)
    # フォールバック: それ以外はすべて STATIC_ROOT 配下から配信
    app.router.add_get("/", static_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    return app


def main():
    log.info("STATIC_ROOT: %s", STATIC_ROOT)
    log.info("GUEST_FISH_DIR: %s", GUEST_FISH_DIR)
    log.info("GUEST_FISH_PATH: %s", GUEST_FISH_PATH)
    log.info("PENDING_OWNER_LINK_PATH: %s", PENDING_OWNER_LINK_PATH)
    log.info("ITEM_JSON_PATH: %s", ITEM_JSON_PATH)
    log.info("SALES_DIR: %s", SALES_DIR)
    log.info("listening on http://%s:%d", HOST, PORT)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
