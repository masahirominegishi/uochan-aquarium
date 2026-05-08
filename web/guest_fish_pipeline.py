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


def _white_paper_components_mask(
    bg_candidate: np.ndarray,
    *,
    min_largest_ratio: float = 0.05,
    min_each_ratio: float = 0.001,
) -> np.ndarray:
    """白っぽい候補のうち、紙の連結成分 (主要 + 細片) をすべて背景化するマスクを返す。

    絵の黒い線で紙の白が複数の連結成分に分断されるケースが多いため、
    「最大成分」だけでなく「画像全体の min_each_ratio 以上のサイズを持つ
    すべての白成分」を紙の一部として背景化する。

    魚の中の小さな白 (目玉等、min_each_ratio 未満) は対象外で残る。
    最大成分が min_largest_ratio 未満の場合は紙が見つからないとみなして
    空マスクを返す (誤検出回避)。
    """
    from scipy.ndimage import label
    labeled, num = label(bg_candidate)
    if num == 0:
        return np.zeros_like(bg_candidate)
    sizes = np.bincount(labeled.ravel())
    sizes[0] = 0
    largest_size = int(sizes.max())
    h, w = bg_candidate.shape
    if largest_size < int(h * w * min_largest_ratio):
        return np.zeros_like(bg_candidate)
    threshold_each = int(h * w * min_each_ratio)
    paper_labels = np.where(sizes >= threshold_each)[0]
    return np.isin(labeled, paper_labels)


def _edge_connected_gray_mask(gray: np.ndarray, thresh: int) -> np.ndarray:
    """グレースケール画像の縁から色差 thresh 以内で連結する領域を返す。

    画像の縁ピクセルの代表色 (中央値) を bordered の 1px 縁に塗り、そこから
    flood fill して thresh 以内の連結を背景マークする。テーブル等の色付き
    領域 (画像縁周辺) を捕捉するための処理。紙の白は中央値 (テーブル色) と
    色差が大きいので捕捉されず、HSV 閾値側で別途処理される。
    """
    h, w = gray.shape
    edge_pixels = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    edge_repr = int(np.median(edge_pixels))
    bordered = Image.new("L", (w + 2, h + 2), edge_repr)
    bordered.paste(Image.fromarray(gray, "L"), (1, 1))
    ImageDraw.floodfill(bordered, (0, 0), 200, thresh=thresh)
    arr = np.asarray(bordered)[1:-1, 1:-1]
    return arr == 200


def remove_white_background(
    img: Image.Image,
    *,
    v_thresh: int = 180,
    s_thresh: int = 60,
    paper_flood_thresh: int = 60,
    paper_min_largest_ratio: float = 0.05,
    paper_min_each_ratio: float = 0.001,
    long_edge: int = 600,
) -> Image.Image:
    """白っぽい紙 + 紙の外側の背景を透明化 → トリミング & 長辺リサイズ。

    3 段階で背景を捕捉:
      1) HSV 閾値で「白っぽいピクセル」候補を作る
      2) 候補のうち、画像全体の paper_min_each_ratio 以上のサイズを持つ
         連結成分すべてを紙の一部として背景化 (最大成分が
         paper_min_largest_ratio 未満なら何もしない、誤検出回避)
      3) 縁から繋がる白も背景化 (紙が画像の縁に接するケース)
      4) グレースケールで縁から色差 paper_flood_thresh 以内で flood fill
         (紙の外側のテーブル等の色付き領域も透明化)

      paper_flood_thresh=0 で 4) を無効化できる。
    """
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

    # 紙 (連結成分群) + 縁から繋がる白 を背景化
    bg_mask = _white_paper_components_mask(
        bg_candidate,
        min_largest_ratio=paper_min_largest_ratio,
        min_each_ratio=paper_min_each_ratio,
    )
    bg_mask = bg_mask | _edge_connected_bg_mask(bg_candidate)

    if paper_flood_thresh > 0:
        gray = (0.299 * r + 0.587 * g + 0.114 * b).astype(np.uint8)
        bg_mask = bg_mask | _edge_connected_gray_mask(gray, paper_flood_thresh)

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
