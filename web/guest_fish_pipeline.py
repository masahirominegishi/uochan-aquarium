"""ゲスト魚画像処理の共有モジュール。

Phase 1 (upload_server.py) と Phase 2/3 (fish_ai_realtime/realtime_loop.py の
音声シャッター / 物理ボタン) の両方から使われる。HTTP/aiohttp 依存はせず、
純粋な画像処理 + メタデータ I/O のみ提供する。

本番環境 (固定カメラ + 紙だけが映る撮影ブース、適正露出) を前提。切り抜きは
bg_method で 2 方式:
  "shape" (既定): 背景差分でインクを拾う → 輪郭の隙間を膨張で橋渡しして閉じる →
      中を alpha で満たす (色は元のまま) → 輪郭をなめらかに整える。露出/色かぶりに強い。
  "hsv" (旧): HSV しきい値で「縁から繋がる白」を透明化。fill_body=True なら中身も埋める。

各パラメータの決まり方 (優先順): 関数引数 > 環境変数 > config.json > 既定値。
  bg_method   GUEST_FISH_BG_METHOD      bg_method                            "shape"
  ink_thresh  GUEST_FISH_INK_THRESH     shape_detect.ink_thresh                  28   # shape
  bg_blur     GUEST_FISH_BG_BLUR        shape_detect.bg_blur                      0   # 0=自動
  close_px    GUEST_FISH_CLOSE_PX       shape_detect.close_px                    18
  smooth      GUEST_FISH_SMOOTH         shape_detect.smooth                     1.0
  v_thresh    GUEST_FISH_V_THRESH       background_removal.value_threshold       200   # hsv
  s_thresh    GUEST_FISH_S_THRESH       background_removal.saturation_threshold   30
  fill_body   GUEST_FISH_FILL_BODY      output.fill_body                       False
  fill_close  GUEST_FISH_FILL_CLOSE     output.fill_close                         25
  long_edge   GUEST_FISH_LONG_EDGE      output.long_edge                         600
紙面検出側 (detect_paper_bbox) は別途 paper_detect.* / GUEST_FISH_PAPER_* 参照。
チューニングは web/tune_guest_fish.py で raw 画像に対してオフラインで詰め、
良い値が出たら config.json か env に反映する (env なら realtime_loop 再起動だけで即時)。
"""

import json
import os
import secrets
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

_DEFAULTS = {
    "bg_method": "shape",   # "shape" = 形ベース (背景差分→輪郭を閉じる→中を満たす→輪郭整える) / "hsv" = 旧 白除去
    # shape 用
    "ink_thresh": 28,       # 紙からの局所残差がこれ以上ならインク。下げると薄いインクも拾う/ノイズも拾う
    "bg_blur": 0,           # 紙の面を推定するメディアンぼかしの ksize (0 = 画像サイズから自動)
    "close_px": 40,         # 輪郭の隙間 (開いた口・ヒレ・ペンの途切れ) を橋渡しする膨張量 px。小さいと中が埋まらない、大きすぎると細部がくっつく
    "smooth": 1.0,          # 輪郭整え: approxPolyDP の epsilon を周長の何 % にするか。0 でスムージング無し
    # hsv 用 (旧)
    "v_thresh": 200, "s_thresh": 30, "fill_body": False, "fill_close": 25,
    # 共通
    "long_edge": 600,
}
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


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return None


def _resolve_bool(explicit, env_name: str, cfg_path: tuple[str, ...], default: bool) -> bool:
    """引数 > 環境変数 > config.json > 既定値 の優先順で bool を解決する。"""
    for cand in (explicit, os.environ.get(env_name), _config_value(cfg_path)):
        b = _as_bool(cand)
        if b is not None:
            return b
    return default


def _config_value(cfg_path: tuple[str, ...]):
    node = _load_config()
    for key in cfg_path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _resolve_int(explicit, env_name: str, cfg_path: tuple[str, ...], default: int) -> int:
    """引数 > 環境変数 > config.json > 既定値 の優先順で int を解決する。"""
    for cand in (explicit, os.environ.get(env_name), _config_value(cfg_path)):
        v = _as_int(cand)
        if v is not None:
            return v
    return default


def _resolve_float(explicit, env_name: str, cfg_path: tuple[str, ...], default: float) -> float:
    """引数 > 環境変数 > config.json > 既定値 の優先順で float を解決する。"""
    for cand in (explicit, os.environ.get(env_name), _config_value(cfg_path)):
        v = _as_float(cand)
        if v is not None:
            return v
    return default


def _resolve_str(explicit, env_name: str, cfg_path: tuple[str, ...], default: str) -> str:
    """引数 > 環境変数 > config.json > 既定値 の優先順で文字列 (小文字化) を解決する。"""
    for cand in (explicit, os.environ.get(env_name), _config_value(cfg_path)):
        if isinstance(cand, str) and cand.strip():
            return cand.strip().lower()
    return default


def _parse_margins(value) -> tuple[int, int, int, int] | None:
    """margins 指定を (left, top, right, bottom) の 4-tuple に正規化する。

    受け付ける形: None / 数値 (= 4 辺同値) / [l,t,r,b] / "l,t,r,b" / "n"。
    符号付き (負 = 内側に縮める)。解釈できなければ None。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if len(value) == 4 and all(_as_int(x) is not None for x in value):
            return tuple(int(x) for x in value)  # type: ignore[return-value]
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return (n, n, n, n)
    if isinstance(value, str):
        parts = [p for p in value.replace(" ", "").split(",") if p != ""]
        ints = [_as_int(p) for p in parts]
        if len(ints) == 1 and ints[0] is not None:
            return (ints[0], ints[0], ints[0], ints[0])
        if len(ints) == 4 and all(i is not None for i in ints):
            return tuple(ints)  # type: ignore[return-value]
    return None


def resolve_params(v_thresh=None, s_thresh=None, long_edge=None, fill_body=None, fill_close=None,
                   bg_method=None, ink_thresh=None, bg_blur=None, close_px=None, smooth=None) -> dict:
    """remove_white_background / cutout_guest_fish が実際に使うパラメータを 引数 > env > config > 既定 で解決して返す。"""
    return {
        "bg_method": _resolve_str(bg_method, "GUEST_FISH_BG_METHOD", ("bg_method",), _DEFAULTS["bg_method"]),
        # shape
        "ink_thresh": _resolve_int(ink_thresh, "GUEST_FISH_INK_THRESH", ("shape_detect", "ink_thresh"), _DEFAULTS["ink_thresh"]),
        "bg_blur": _resolve_int(bg_blur, "GUEST_FISH_BG_BLUR", ("shape_detect", "bg_blur"), _DEFAULTS["bg_blur"]),
        "close_px": _resolve_int(close_px, "GUEST_FISH_CLOSE_PX", ("shape_detect", "close_px"), _DEFAULTS["close_px"]),
        "smooth": _resolve_float(smooth, "GUEST_FISH_SMOOTH", ("shape_detect", "smooth"), _DEFAULTS["smooth"]),
        # hsv (旧)
        "v_thresh": _resolve_int(v_thresh, "GUEST_FISH_V_THRESH", ("background_removal", "value_threshold"), _DEFAULTS["v_thresh"]),
        "s_thresh": _resolve_int(s_thresh, "GUEST_FISH_S_THRESH", ("background_removal", "saturation_threshold"), _DEFAULTS["s_thresh"]),
        "fill_body": _resolve_bool(fill_body, "GUEST_FISH_FILL_BODY", ("output", "fill_body"), _DEFAULTS["fill_body"]),
        "fill_close": _resolve_int(fill_close, "GUEST_FISH_FILL_CLOSE", ("output", "fill_close"), _DEFAULTS["fill_close"]),
        # 共通
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


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """bool マスクの最大連結成分だけ残して返す。空なら元のまま。"""
    from scipy import ndimage
    labels, n = ndimage.label(mask)
    if n <= 1:
        return mask
    sizes = ndimage.sum(np.ones_like(labels), labels, index=range(1, n + 1))
    return labels == (int(np.argmax(sizes)) + 1)


def compute_silhouette(rgb: np.ndarray, *, v_thresh: int, s_thresh: int, close_px: int) -> np.ndarray:
    """魚の「中身まで埋めた」シルエットの bool マスクを返す (teamLab Sketch Aquarium 風)。

    手順: compute_bg_mask で求めた「縁から繋がる白 (= 紙)」の補集合 = インク線画 +
    色のついた部分 + 縁から繋がっていない白 (目玉等) を出発点に、close_px だけ膨張
    させて輪郭線の隙間 (開いた口・ヒレの切れ目) を橋渡し → 穴埋め (= 体の内側が埋まる)
    → 同じだけ収縮させて輪郭の太さを元に戻す → 最大連結成分。close_px が小さいと
    隙間を埋めきれず中空のまま、大きすぎると細い部分が太る/くっつく。
    """
    from scipy import ndimage

    not_bg = ~compute_bg_mask(rgb, v_thresh=v_thresh, s_thresh=s_thresh)
    k = max(0, int(close_px))
    if k > 0:
        grown = ndimage.binary_dilation(not_bg, iterations=k)
        filled = ndimage.binary_fill_holes(grown)
        sil = ndimage.binary_erosion(filled, iterations=k, border_value=1)
    else:
        sil = ndimage.binary_fill_holes(not_bg)
    return _largest_component(sil)


# ─── 形ベース (shape) パイプライン ──────────────────────────────
# 適正露出 (色が飛んでいない) の撮影を前提に、画素のしきい値ではなく「魚の形」を
# 捉えて切り抜く。1) 背景差分でインクを拾う → 2) 隙間を膨張で橋渡しして輪郭を閉じる
# → 3) 中を alpha で満たす (色は元のまま) → 4) 輪郭をなめらかに整える。
def _paper_background(rgb: np.ndarray, blur: int) -> np.ndarray:
    """紙の面 (ビネット・色かぶり込みの、なだらかに変化する成分) をメディアンぼかしで推定。"""
    import cv2
    h, w = rgb.shape[:2]
    k = int(blur) if blur and blur > 0 else max(9, int(round(max(h, w) * 0.027)))
    if k % 2 == 0:
        k += 1
    return cv2.medianBlur(np.ascontiguousarray(rgb), k)


def ink_mask(rgb: np.ndarray, *, thresh: int, blur: int = 0) -> np.ndarray:
    """背景差分でインク (色・明るさ問わず) を拾った bool マスク。

    紙の面を _paper_background で推定し、元画像との残差 (チャンネルごとの差の最大) が
    thresh 以上をインクとみなす。なだらかな照明ムラ (ビネット・ピンクかぶり) は残差が
    ほぼ 0 なので除外される。
    """
    bg = _paper_background(rgb, blur)
    resid = np.abs(rgb.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    return resid >= int(thresh)


def _smooth_mask(mask: np.ndarray, eps_pct: float) -> np.ndarray:
    """mask の最大外周コンターを approxPolyDP + Chaikin で整え、塗り直したマスクを返す。

    eps_pct は approxPolyDP の epsilon を周長の何 % にするか。0 以下なら何もしない。
    """
    if eps_pct is None or eps_pct <= 0:
        return mask
    import cv2
    m = (mask.astype(np.uint8)) * 255
    contours = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]  # 3.x/4.x 両対応
    if not contours:
        return mask
    cnt = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(cnt, True)
    eps = max(1.5, peri * float(eps_pct) / 100.0)
    poly = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2).astype(np.float64)
    for _ in range(2):  # Chaikin で角を丸める
        if len(poly) < 3:
            break
        nxt = np.roll(poly, -1, axis=0)
        poly = np.column_stack([
            np.ravel(np.column_stack([0.75 * poly[:, 0] + 0.25 * nxt[:, 0], 0.25 * poly[:, 0] + 0.75 * nxt[:, 0]])),
            np.ravel(np.column_stack([0.75 * poly[:, 1] + 0.25 * nxt[:, 1], 0.25 * poly[:, 1] + 0.75 * nxt[:, 1]])),
        ])
    out = np.zeros(mask.shape, dtype=np.uint8)
    cv2.fillPoly(out, [np.round(poly).astype(np.int32)], 1)
    return out.astype(bool)


def fish_mask(rgb: np.ndarray, *, ink_thresh: int, bg_blur: int = 0, close_px: int = 18, smooth: float = 1.0) -> np.ndarray:
    """魚の形 (中身まで満たした) の bool マスクを返す。alpha に使う。色 (RGB) は呼び出し側で元のまま。

    インク検出 → close_px だけ膨張で輪郭の隙間を橋渡し → 最大連結成分 → 穴埋め →
    同じだけ収縮 → 輪郭整え (smooth)。
    """
    from scipy import ndimage
    m = ink_mask(rgb, thresh=ink_thresh, blur=bg_blur)
    k = max(0, int(close_px))
    if k > 0:
        m = ndimage.binary_dilation(m, iterations=k)
        m = _largest_component(m)
        m = ndimage.binary_fill_holes(m)
        m = ndimage.binary_erosion(m, iterations=k, border_value=1)
    else:
        m = ndimage.binary_fill_holes(_largest_component(m))
    return _smooth_mask(m, smooth)


def _crop_and_resize_rgba(rgb: np.ndarray, alpha: np.ndarray, long_edge: int) -> Image.Image:
    """alpha>0 の bbox に crop して RGBA を返す。透明部分の RGB は 0 に潰す (見えないが念のため)。"""
    alpha = alpha.astype(np.uint8)
    ys, xs = np.where(alpha > 0)
    if len(ys) == 0:
        return Image.fromarray(np.dstack([rgb.astype(np.uint8), alpha]), "RGBA")
    t, b, l, r = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    rgb_c = rgb[t:b, l:r].astype(np.uint8).copy()
    a_c = alpha[t:b, l:r]
    rgb_c[a_c == 0] = 0
    out = Image.fromarray(np.dstack([rgb_c, a_c]), "RGBA")
    w, h = out.size
    if long_edge and max(w, h) > long_edge:
        if w >= h:
            out = out.resize((long_edge, max(1, round(h * long_edge / w))), Image.LANCZOS)
        else:
            out = out.resize((max(1, round(w * long_edge / h)), long_edge), Image.LANCZOS)
    return out


def cutout_guest_fish(
    img: Image.Image,
    *,
    ink_thresh: int | None = None,
    bg_blur: int | None = None,
    close_px: int | None = None,
    smooth: float | None = None,
    long_edge: int | None = None,
) -> Image.Image:
    """形ベースで魚を切り抜いた RGBA を返す。輪郭の内側は撮った写真のまま、外側は透明。"""
    p = resolve_params(long_edge=long_edge, bg_method="shape", ink_thresh=ink_thresh,
                       bg_blur=bg_blur, close_px=close_px, smooth=smooth)
    img = ImageOps.exif_transpose(img).convert("RGB")
    rgb = np.array(img)
    mask = fish_mask(rgb, ink_thresh=p["ink_thresh"], bg_blur=p["bg_blur"], close_px=p["close_px"], smooth=p["smooth"])
    alpha = np.where(mask, 255, 0).astype(np.uint8)
    return _crop_and_resize_rgba(rgb, alpha, p["long_edge"])


# ─── 紙面検出 (撮影画像から白い紙のシートだけを切り出す) ──────────
# v_thresh: これ未満の明るさは「紙ではない (暗いブース枠/影/外の景色)」扱い。
#           上げるほど縁の暗い部分を強くトリミングする。
# s_thresh: これより彩度が高いと「紙ではない (色フリンジ等)」扱い。下げるほど縁の
#           色かぶり (赤/ピンクのフチ) を強くトリミングする。中央の色かぶりは縁から
#           繋がっていないので、しきい値を下げても紙面内には残る。
# margins:  検出した紙面 bbox を各辺 (left, top, right, bottom) に広げる px。負で
#           内側に縮める (= レンズ歪みで四隅に出るボックスのフチを確実に切り落とす)。
#           単一値を与えると 4 辺同値。pad は margins の旧名 (互換用、単一値のみ)。
_PAPER_DEFAULTS = {"v_thresh": 150, "s_thresh": 80, "margins": (0, 0, 0, 0)}


def resolve_paper_margins(margins=None, pad=None) -> tuple[int, int, int, int]:
    """紙面 bbox を広げる量を 引数 margins > env GUEST_FISH_PAPER_MARGINS >
    config paper_detect.margins > (引数 pad > env GUEST_FISH_PAPER_PAD >
    config paper_detect.pad) > 既定 の優先順で解決して (l, t, r, b) を返す。"""
    for cand in (margins, os.environ.get("GUEST_FISH_PAPER_MARGINS"), _config_value(("paper_detect", "margins"))):
        m = _parse_margins(cand)
        if m is not None:
            return m
    # margins 系が無ければ uniform pad にフォールバック
    if pad is not None:
        m = _parse_margins(pad)
        if m is not None:
            return m
    p = _as_int(os.environ.get("GUEST_FISH_PAPER_PAD"))
    if p is None:
        p = _as_int(_config_value(("paper_detect", "pad")))
    if p is not None:
        return (p, p, p, p)
    return _PAPER_DEFAULTS["margins"]


def resolve_paper_params(v_thresh=None, s_thresh=None, margins=None, pad=None) -> dict:
    """detect_paper_bbox が実際に使う値を 引数 > env > config.json > 既定 で解決して返す。"""
    return {
        "v_thresh": _resolve_int(v_thresh, "GUEST_FISH_PAPER_V", ("paper_detect", "value_threshold"), _PAPER_DEFAULTS["v_thresh"]),
        "s_thresh": _resolve_int(s_thresh, "GUEST_FISH_PAPER_S", ("paper_detect", "saturation_threshold"), _PAPER_DEFAULTS["s_thresh"]),
        "margins": resolve_paper_margins(margins, pad),
    }


def detect_paper_bbox(
    rgb: np.ndarray,
    *,
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    margins=None,
    pad=None,
    min_area_frac: float = 0.04,
) -> tuple[int, int, int, int] | None:
    """白い紙のシートだけを囲む bbox (l, t, r, b) を返す。検出できなければ None。

    手順: 「明るくて彩度が低い = きれいな紙」以外のピクセル (暗いブース枠・影・
    色フリンジ・外の景色) のうち、画像の縁から連結しているものを背景として
    flood fill で除外する。残った最大連結領域が紙面 (絵のインクは紙の内側に
    浮いているので一緒に残る)。margins (l,t,r,b) で各辺を外/内に微調整できる。
    """
    from scipy import ndimage  # 重い import は遅延

    h, w = rgb.shape[:2]
    p = resolve_paper_params(v_thresh, s_thresh, margins, pad)
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
    ml, mt, mr, mb = p["margins"]
    l = min(max(0, int(xs.min()) - ml), w - 1)
    t = min(max(0, int(ys.min()) - mt), h - 1)
    r = max(min(w, int(xs.max()) + 1 + mr), l + 1)
    b = max(min(h, int(ys.max()) + 1 + mb), t + 1)
    if r - l < 2 or b - t < 2:
        return None
    return (l, t, r, b)


def crop_to_paper(
    img: Image.Image,
    *,
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    margins=None,
    pad=None,
) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    """img を紙面の bbox に crop して (cropped, bbox) を返す。検出できなければ (img, None)。"""
    rgb = np.array(ImageOps.exif_transpose(img).convert("RGB"))
    bbox = detect_paper_bbox(rgb, v_thresh=v_thresh, s_thresh=s_thresh, margins=margins, pad=pad)
    if bbox is None:
        return img, None
    return img.crop(bbox), bbox


def remove_white_background(
    img: Image.Image,
    *,
    bg_method: str | None = None,
    # shape 用
    ink_thresh: int | None = None,
    bg_blur: int | None = None,
    close_px: int | None = None,
    smooth: float | None = None,
    # hsv 用 (旧)
    v_thresh: int | None = None,
    s_thresh: int | None = None,
    fill_body: bool | None = None,
    fill_close: int | None = None,
    # 共通
    long_edge: int | None = None,
) -> Image.Image:
    """ゲスト魚を切り抜いた RGBA を返す。bg_method ("shape" 既定 / "hsv") で方式を選ぶ。

    省略した引数は env / config.json / 既定値の順で解決される (モジュール docstring 参照)。
    本番 (固定カメラ + 紙だけが映る撮影ブース、適正露出) を前提。

    - "shape": 背景差分でインクを拾い → 輪郭の隙間を close_px で橋渡し → 中を満たし →
      輪郭を smooth で整える。輪郭の内側は撮った写真のまま、外側は透明。露出や色かぶりに強い。
    - "hsv" (旧): HSV しきい値で「縁から繋がる白」を透明化。fill_body=True なら魚の中身も埋める。
    """
    params = resolve_params(v_thresh, s_thresh, long_edge, fill_body, fill_close,
                            bg_method, ink_thresh, bg_blur, close_px, smooth)
    long_edge = params["long_edge"]

    if params["bg_method"] == "shape":
        return cutout_guest_fish(img, ink_thresh=params["ink_thresh"], bg_blur=params["bg_blur"],
                                 close_px=params["close_px"], smooth=params["smooth"], long_edge=long_edge)

    # ── 旧 HSV 方式 ──
    v_thresh, s_thresh = params["v_thresh"], params["s_thresh"]
    fill_body, fill_close = params["fill_body"], params["fill_close"]
    img = ImageOps.exif_transpose(img).convert("RGB")
    rgb = np.array(img)
    if fill_body:
        sil = compute_silhouette(rgb, v_thresh=v_thresh, s_thresh=s_thresh, close_px=fill_close)
        alpha = np.where(sil, 255, 0).astype(np.uint8)
    else:
        bg_mask = compute_bg_mask(rgb, v_thresh=v_thresh, s_thresh=s_thresh)
        alpha = np.where(bg_mask, 0, 255).astype(np.uint8)
    return _crop_and_resize_rgba(rgb, alpha, long_edge)


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
