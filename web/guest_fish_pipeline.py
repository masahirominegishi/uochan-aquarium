"""ゲスト魚画像処理の共有モジュール (Phase 2 で切り出し)。

Phase 1 (upload_server.py) と Phase 2 (fish_ai_realtime/realtime_loop.py の
音声シャッター) の両方から使われる。HTTP/aiohttp 依存はせず、純粋な
画像処理 + メタデータ I/O のみ提供する。

呼び出し側は web/config.json か独自の設定値を引数で渡す (デフォルト値あり)。
"""

import json
import os
import secrets
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps


# ─── 背景除去 ──────────────────────────────────────────
def _edge_connected_bg_mask(bg_candidate: np.ndarray) -> np.ndarray:
    """画像の縁から到達できる白領域のみを背景とみなして mask を返す。

    bg_candidate は「色的には白っぽい」全ピクセルの bool 配列。これをそのまま
    透明化すると魚の中の白 (目玉・お腹等) も消えてしまうので、画像の縁に接して
    いる連結成分のみを抽出する (flood fill from edges)。
    """
    h, w = bg_candidate.shape
    mask_pil = Image.fromarray(np.where(bg_candidate, 255, 0).astype(np.uint8), mode="L")
    bordered = Image.new("L", (w + 2, h + 2), 255)
    bordered.paste(mask_pil, (1, 1))
    ImageDraw.floodfill(bordered, (0, 0), 128, thresh=0)
    arr = np.asarray(bordered)[1:-1, 1:-1]
    return arr == 128


def remove_white_background(
    img: Image.Image,
    *,
    v_thresh: int = 240,
    s_thresh: int = 30,
    long_edge: int = 600,
) -> Image.Image:
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
    bg_candidate = (v >= v_thresh) & (s <= s_thresh)

    bg_mask = _edge_connected_bg_mask(bg_candidate)

    rgba = np.dstack([arr.astype(np.uint8), np.full(arr.shape[:2], 255, dtype=np.uint8)])
    rgba[bg_mask] = [0, 0, 0, 0]
    out = Image.fromarray(rgba, "RGBA")

    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)

    w, h = out.size
    if max(w, h) > long_edge:
        if w >= h:
            new_size = (long_edge, max(1, int(h * long_edge / w)))
        else:
            new_size = (max(1, int(w * long_edge / h)), long_edge)
        out = out.resize(new_size, Image.LANCZOS)

    return out


# ─── メタデータ I/O ────────────────────────────────────
def new_fish_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def load_metadata(path: Path) -> dict:
    if not path.exists():
        return {"fishes": []}
    try:
        with path.open() as f:
            data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("fishes"), list):
                return {"fishes": []}
            return data
    except (json.JSONDecodeError, OSError):
        return {"fishes": []}


def save_metadata(path: Path, data: dict) -> None:
    """atomic write (temp -> rename) で破損を防ぐ。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def append_fish(
    metadata_path: Path,
    fish_id: str,
    image_filename: str,
    *,
    owner_person_id: str | None = None,
) -> dict:
    """guest_fish.json に新しい魚を 1 件追記して、追記したエントリを返す。"""
    data = load_metadata(metadata_path)
    entry = {
        "id": fish_id,
        "image": image_filename,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "owner_person_id": owner_person_id,
    }
    data.setdefault("fishes", []).append(entry)
    save_metadata(metadata_path, data)
    return entry
