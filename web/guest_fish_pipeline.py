"""ゲスト魚画像処理の共有モジュール。

Phase 1 (upload_server.py) と Phase 2/3 (fish_ai_realtime/realtime_loop.py の
音声シャッター / 物理ボタン) の両方から使われる。HTTP/aiohttp 依存はせず、
純粋な画像処理 + メタデータ I/O のみ提供する。

本番環境 (固定カメラ + 紙だけが映る撮影ブース) を前提にしたシンプル実装。
HSV 閾値で「白っぽいピクセル」を判定し、画像の縁から flood fill で繋がる
部分のみを背景として透明化する (魚の中の目玉等の白は残す)。

しきい値の決まり方 (優先順):
  1. 関数引数で明示された値
  2. 環境変数 GUEST_FISH_V_THRESH / GUEST_FISH_S_THRESH / GUEST_FISH_LONG_EDGE
  3. 同ディレクトリの config.json (background_removal.* / output.long_edge)
  4. ハードコードの既定値 (200 / 30 / 600)
チューニングは web/tune_guest_fish.py で raw 画像に対してオフラインで詰め、
良い値が出たら config.json か上記 env に反映する (realtime_loop の再起動不要、
env なら反映即時)。
"""

import json
import os
import secrets
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

_DEFAULTS = {"v_thresh": 200, "s_thresh": 30, "long_edge": 600}
_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_cfg_cache: dict | None = None


def _load_config() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        try:
            with _CONFIG_PATH.open() as f:
                _cfg_cache = json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            _cfg_cache = {}
    return _cfg_cache


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_int(explicit, env_name: str, cfg_path: tuple[str, ...], default: int) -> int:
    """引数 > 環境変数 > config.json > 既定値 の優先順で int を解決する。"""
    if explicit is not None:
        v = _as_int(explicit)
        if v is not None:
            return v
    v = _as_int(os.environ.get(env_name))
    if v is not None:
        return v
    node = _load_config()
    for key in cfg_path:
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(key)
    v = _as_int(node)
    if v is not None:
        return v
    return default


def resolve_params(v_thresh=None, s_thresh=None, long_edge=None) -> dict:
    """remove_white_background が実際に使うしきい値を返す (CLI からの確認用)。"""
    return {
        "v_thresh": _resolve_int(v_thresh, "GUEST_FISH_V_THRESH", ("background_removal", "value_threshold"), _DEFAULTS["v_thresh"]),
        "s_thresh": _resolve_int(s_thresh, "GUEST_FISH_S_THRESH", ("background_removal", "saturation_threshold"), _DEFAULTS["s_thresh"]),
        "long_edge": _resolve_int(long_edge, "GUEST_FISH_LONG_EDGE", ("output", "long_edge"), _DEFAULTS["long_edge"]),
    }


# ─── 背景除去 ──────────────────────────────────────────
def _edge_connected_bg_mask(bg_candidate: np.ndarray) -> np.ndarray:
    """画像の縁から到達できる白領域のみを背景とみなして mask を返す。

    bg_candidate は「白っぽい」全ピクセルの bool 配列。これをそのまま透明化
    すると魚の中の白 (目玉・お腹等) も消えてしまうので、画像の縁に接して
    いる連結成分のみを抽出する (flood fill from edges)。
    """
    h, w = bg_candidate.shape
    mask_pil = Image.fromarray(np.where(bg_candidate, 255, 0).astype(np.uint8), mode="L")
    bordered = Image.new("L", (w + 2, h + 2), 255)
    bordered.paste(mask_pil, (1, 1))
    ImageDraw.floodfill(bordered, (0, 0), 128, thresh=0)
    arr = np.asarray(bordered)[1:-1, 1:-1]
    return arr == 128


def _value_saturation(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """RGB 配列から HSV の V (= max channel, 0-255) と S (0-255) を返す。"""
    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    v = maxc
    s = np.where(maxc == 0, 0.0, (maxc - minc) / np.maximum(maxc, 1.0) * 255.0)
    return v, s


def compute_bg_mask(rgb: np.ndarray, *, v_thresh: int, s_thresh: int) -> np.ndarray:
    """RGB 配列 (H,W,3) から「背景として透明化されるピクセル」の bool マスクを返す。

    HSV で V >= v_thresh かつ S <= s_thresh のピクセルを「白っぽい候補」とし、
    画像の縁から flood fill で繋がる部分だけを背景とみなす。tune_guest_fish.py
    がマスク可視化のために直接呼ぶ。
    """
    v, s = _value_saturation(rgb)
    bg_candidate = (v >= v_thresh) & (s <= s_thresh)
    return _edge_connected_bg_mask(bg_candidate)


# ─── 紙面検出 (撮影画像から白い紙のシートだけを切り出す) ──────────
# v_thresh: これ未満の明るさは「紙ではない (暗いブース枠/影/外の景色)」扱い。
#           上げるほど縁の暗い部分を強くトリミングする。
# s_thresh: これより彩度が高いと「紙ではない (色フリンジ等)」扱い。下げるほど縁の
#           色かぶり (赤/ピンクのフチ) を強くトリミングする。中央の色かぶりは縁から
#           繋がっていないので、しきい値を下げても紙面内には残る。
# pad:      検出した bbox を外側に広げる px。負で内側に縮める (縁の影を確実に切る用)。
_PAPER_DEFAULTS = {"v_thresh": 150, "s_thresh": 80, "pad": 0}


def resolve_paper_params(v_thresh=None, s_thresh=None, pad=None) -> dict:
    """detect_paper_bbox が実際に使う値を 引数 > env > config.json > 既定 で解決して返す。"""
    return {
        "v_thresh": _resolve_int(v_thresh, "GUEST_FISH_PAPER_V", ("paper_detect", "value_threshold"), _PAPER_DEFAULTS["v_thresh"]),
        "s_thresh": _resolve_int(s_thresh, "GUEST_FISH_PAPER_S", ("paper_detect", "saturation_threshold"), _PAPER_DEFAULTS["s_thresh"]),
        "pad": _resolve_int(pad, "GUEST_FISH_PAPER_PAD", ("paper_detect", "pad"), _PAPER_DEFAULTS["pad"]),
    }


def detect_paper_bbox(
    rgb: np.ndarray,
    *,
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    pad: int | None = None,
    min_area_frac: float = 0.04,
) -> tuple[int, int, int, int] | None:
    """白い紙のシートだけを囲む bbox (l, t, r, b) を返す。検出できなければ None。

    手順: 「明るくて彩度が低い = きれいな紙」以外のピクセル (暗いブース枠・影・
    色フリンジ・外の景色) のうち、画像の縁から連結しているものを背景として
    flood fill で除外する。残った最大連結領域が紙面 (絵のインクは紙の内側に
    浮いているので一緒に残る)。pad で bbox を外/内に微調整できる。
    """
    from scipy import ndimage  # 重い import は遅延

    h, w = rgb.shape[:2]
    p = resolve_paper_params(v_thresh, s_thresh, pad)
    v, s = _value_saturation(rgb)
    clean_paper = (v >= p["v_thresh"]) & (s <= p["s_thresh"])
    frame = _edge_connected_bg_mask(~clean_paper)   # 縁から繋がる「紙でない」領域 = ブース枠等
    paper_candidate = ~frame
    paper_candidate = ndimage.binary_opening(paper_candidate, structure=np.ones((5, 5)))
    labels, n = ndimage.label(paper_candidate)
    if n == 0:
        return None
    sizes = ndimage.sum(np.ones_like(labels), labels, index=range(1, n + 1))
    biggest = int(np.argmax(sizes)) + 1
    if sizes[biggest - 1] < min_area_frac * h * w:
        return None
    region = ndimage.binary_fill_holes(labels == biggest)
    ys, xs = np.where(region)
    pad_v = p["pad"]
    l = max(0, int(xs.min()) - pad_v)
    t = max(0, int(ys.min()) - pad_v)
    r = min(w, int(xs.max()) + 1 + pad_v)
    b = min(h, int(ys.max()) + 1 + pad_v)
    if r - l < 2 or b - t < 2:
        return None
    return (l, t, r, b)


def crop_to_paper(
    img: Image.Image,
    *,
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    pad: int | None = None,
) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    """img を紙面の bbox に crop して (cropped, bbox) を返す。検出できなければ (img, None)。"""
    rgb = np.array(ImageOps.exif_transpose(img).convert("RGB"))
    bbox = detect_paper_bbox(rgb, v_thresh=v_thresh, s_thresh=s_thresh, pad=pad)
    if bbox is None:
        return img, None
    return img.crop(bbox), bbox


def remove_white_background(
    img: Image.Image,
    *,
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    long_edge: int | None = None,
) -> Image.Image:
    """HSV 閾値で白っぽいピクセルを抽出 → 縁から繋がる部分のみ透明化 → トリミング & 長辺リサイズ。

    しきい値を省略 (None) すると env / config.json / 既定値の順で解決される
    (モジュール docstring 参照)。本番環境 (固定カメラ + 紙だけが映る撮影
    ブース) では紙の縁が画像の縁に接するので、シンプルな縁 flood fill で紙
    全体が背景化される。机や手が映るテスト環境では透明化が不完全になる
    可能性がある。
    """
    params = resolve_params(v_thresh, s_thresh, long_edge)
    v_thresh, s_thresh, long_edge = params["v_thresh"], params["s_thresh"], params["long_edge"]

    img = ImageOps.exif_transpose(img).convert("RGB")
    rgb = np.array(img)
    bg_mask = compute_bg_mask(rgb, v_thresh=v_thresh, s_thresh=s_thresh)

    rgba = np.dstack([rgb.astype(np.uint8), np.full(rgb.shape[:2], 255, dtype=np.uint8)])
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
