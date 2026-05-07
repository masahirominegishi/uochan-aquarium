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
import secrets
import time
from pathlib import Path

import numpy as np
from aiohttp import web
from PIL import Image, ImageDraw, ImageOps


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

V_THRESH = int(_cfg.get("background_removal", {}).get("value_threshold", 240))
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


# ─── 背景除去 ──────────────────────────────────────────
def _edge_connected_bg_mask(bg_candidate: np.ndarray) -> np.ndarray:
    """画像の縁から到達できる白領域のみを背景とみなして mask を返す。

    bg_candidate は「色的には白っぽい」全ピクセルの bool 配列。これをそのまま
    透明化すると魚の中の白 (目玉・お腹等) も消えてしまうので、画像の縁に接して
    いる連結成分のみを抽出する (flood fill from edges)。
    """
    h, w = bg_candidate.shape
    # 0 = 描画 / 255 = 白っぽい候補ピクセル
    mask_pil = Image.fromarray(np.where(bg_candidate, 255, 0).astype(np.uint8), mode="L")
    # 1 px の白縁を足す。flood fill (0,0) からその縁を通ってあらゆる端の白に到達できる。
    bordered = Image.new("L", (w + 2, h + 2), 255)
    bordered.paste(mask_pil, (1, 1))
    # (0,0) から flood fill し、到達白を 128 に塗る (描画ピクセル 0 はブロックされる)
    ImageDraw.floodfill(bordered, (0, 0), 128, thresh=0)
    arr = np.asarray(bordered)[1:-1, 1:-1]
    return arr == 128


def remove_white_background(img: Image.Image) -> Image.Image:
    """HSV で白っぽいピクセルを抽出 → 縁から繋がっている部分だけを透明化 → トリミング & 長辺リサイズ。"""
    img = ImageOps.exif_transpose(img).convert("RGB")
    arr = np.array(img).astype(np.float32)
    r = arr[..., 0]
    g = arr[..., 1]
    b = arr[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    v = maxc
    s = np.where(maxc == 0, 0.0, (maxc - minc) / np.maximum(maxc, 1.0) * 255.0)
    bg_candidate = (v >= V_THRESH) & (s <= S_THRESH)

    # 縁から繋がっていない「魚の中の白」は残す
    bg_mask = _edge_connected_bg_mask(bg_candidate)

    rgba = np.dstack([arr.astype(np.uint8), np.full(arr.shape[:2], 255, dtype=np.uint8)])
    rgba[bg_mask] = [0, 0, 0, 0]
    out = Image.fromarray(rgba, "RGBA")

    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)

    w, h = out.size
    if max(w, h) > LONG_EDGE:
        if w >= h:
            new_size = (LONG_EDGE, max(1, int(h * LONG_EDGE / w)))
        else:
            new_size = (max(1, int(w * LONG_EDGE / h)), LONG_EDGE)
        out = out.resize(new_size, Image.LANCZOS)

    return out


# ─── メタデータ I/O ────────────────────────────────────
def _new_fish_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def _load_metadata() -> dict:
    if not GUEST_FISH_PATH.exists():
        return {"fishes": []}
    try:
        with GUEST_FISH_PATH.open() as f:
            data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("fishes"), list):
                return {"fishes": []}
            return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("guest_fish.json 読み込み失敗、空で再開: %s", e)
        return {"fishes": []}


def _save_metadata(data: dict) -> None:
    """atomic write (temp -> rename) で破損を防ぐ。"""
    GUEST_FISH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GUEST_FISH_PATH.with_suffix(GUEST_FISH_PATH.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, GUEST_FISH_PATH)


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
    try:
        processed = await loop.run_in_executor(None, remove_white_background, img)
    except Exception as e:
        log.exception("背景除去失敗")
        return web.json_response({"error": f"処理失敗: {e}"}, status=500)

    fish_id = _new_fish_id()
    out_name = f"{fish_id}.png"
    GUEST_FISH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GUEST_FISH_DIR / out_name
    try:
        processed.save(out_path, "PNG")
    except Exception as e:
        log.exception("画像保存失敗")
        return web.json_response({"error": f"保存失敗: {e}"}, status=500)

    async with _metadata_lock:
        data = _load_metadata()
        data.setdefault("fishes", []).append(
            {
                "id": fish_id,
                "image": out_name,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "owner_person_id": None,
            }
        )
        _save_metadata(data)

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
    return web.FileResponse(target)


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
    # フォールバック: それ以外はすべて STATIC_ROOT 配下から配信
    app.router.add_get("/", static_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    return app


def main():
    log.info("STATIC_ROOT: %s", STATIC_ROOT)
    log.info("GUEST_FISH_DIR: %s", GUEST_FISH_DIR)
    log.info("GUEST_FISH_PATH: %s", GUEST_FISH_PATH)
    log.info("listening on http://%s:%d", HOST, PORT)
    web.run_app(make_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
