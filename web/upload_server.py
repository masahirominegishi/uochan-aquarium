"""Aquarium upload server (Phase 1).

aiohttp で aquarium UI 静的配信 + お客さんがアップロードする魚画像の受け口を兼ねる
HTTP サーバー。pi-main の旧 `python3 -m http.server` を置き換える。

エンドポイント:
  GET  /                    aquarium UI (index.html)
  GET  /upload              お客さん向けアップロードフォーム HTML
  POST /api/upload          画像受信 → HSV 閾値で背景除去 → 保存 → guest_fish.json 追記
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
import time
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
    app.router.add_get("/upload", upload_form_handler)
    app.router.add_get("/guest_fish/{name}", guest_fish_image_handler)
    app.router.add_get("/item.json", item_json_handler)
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
    log.info("listening on http://%s:%d", HOST, PORT)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
