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
  bg_method     GUEST_FISH_BG_METHOD       bg_method                            "shape"
  white_balance GUEST_FISH_WHITE_BALANCE   shape_detect.white_balance            True   # shape: 紙で正規化 (flat-field)
  wb_target     GUEST_FISH_WB_TARGET       shape_detect.wb_target                 245
  ink_thresh    GUEST_FISH_INK_THRESH      shape_detect.ink_thresh                 28
  bg_blur       GUEST_FISH_BG_BLUR         shape_detect.bg_blur                     0   # 0=自動
  close_px      GUEST_FISH_CLOSE_PX        shape_detect.close_px                   40
  smooth        GUEST_FISH_SMOOTH          shape_detect.smooth                    0.0   # 0=整えない (シャープ)
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
    # autocontrast: PIL.ImageOps.autocontrast 相当の自動色補正 (各チャンネルのヒストグラムを [0,255] に伸長)。
    #   = オートホワイトバランス + オートレベル を 1 発で。紙が白に、インクが黒に、色かぶりが消える。
    #   cutoff = 上下それぞれ何 % の外れ値を無視するか (大きいほど強く飛ばす)。
    "autocontrast": True,
    "autocontrast_cutoff": 1,
    "white_balance": False,  # 紙の面で割る flat-field 正規化 (ビネット = 周辺の暗がりも消える)。autocontrast と併用も可
    "wb_target": 245,       # flat-field 後の紙の明るさ。低いほど暗め
    # レベル補正 (flat-field の後、Photoshop の「レベル」相当: 入力 [black,white] を [0,255] に伸長 + gamma)
    "levels_black": 0,      # これ以下は黒に。上げるほどインク/暗部が締まり全体が暗く (= レベル補正。0 = しない)
    "levels_white": 255,    # これ以上は白に。下げるほど薄いグレーが白に飛ぶ (= レベル補正。255 = しない)
    "levels_gamma": 1.0,    # 中間調 (1.0=変えない、<1 で暗く、>1 で明るく)
    "ink_thresh": 28,       # 紙からの局所残差がこれ以上ならインク。下げると薄いインクも拾う/ノイズも拾う
    "bg_blur": 0,           # 紙の面を推定するメディアンぼかしの ksize (0 = 画像サイズから自動)。WB と ink 検出で共用
    "close_px": 40,         # 輪郭の隙間を閉じる強さ。端点ペアを直線で繋ぐ最大距離 = 2*close_px px。小さいと大きく開いた口が閉じず中空に、大きすぎると離れた線同士を繋いでしまう
    "smooth": 0.0,          # ベタ面の縁をなめらかにする (approxPolyDP epsilon を周長の何 % か)。0 (既定) = なめらかにしない (手描き線をそのままシャープに切る)。インク自体はこれに関わらず常にシャープ
    "trim_halo": True,      # 仕上げ: 透明に隣接する白 (輪郭線の外に膨らんだ白) を、インク/橋渡し線にぶつかるまで削る。橋渡し線は削らない。体内部まで漏れる場合はスキップ
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
                   bg_method=None, ink_thresh=None, bg_blur=None, close_px=None, smooth=None, trim_halo=None,
                   white_balance=None, wb_target=None, levels_black=None, levels_white=None, levels_gamma=None,
                   autocontrast=None, autocontrast_cutoff=None) -> dict:
    """remove_white_background / cutout_guest_fish が実際に使うパラメータを 引数 > env > config > 既定 で解決して返す。"""
    return {
        "bg_method": _resolve_str(bg_method, "GUEST_FISH_BG_METHOD", ("bg_method",), _DEFAULTS["bg_method"]),
        # shape
        "autocontrast": _resolve_bool(autocontrast, "GUEST_FISH_AUTOCONTRAST", ("shape_detect", "autocontrast"), _DEFAULTS["autocontrast"]),
        "autocontrast_cutoff": _resolve_int(autocontrast_cutoff, "GUEST_FISH_AUTOCONTRAST_CUTOFF", ("shape_detect", "autocontrast_cutoff"), _DEFAULTS["autocontrast_cutoff"]),
        "white_balance": _resolve_bool(white_balance, "GUEST_FISH_WHITE_BALANCE", ("shape_detect", "white_balance"), _DEFAULTS["white_balance"]),
        "wb_target": _resolve_int(wb_target, "GUEST_FISH_WB_TARGET", ("shape_detect", "wb_target"), _DEFAULTS["wb_target"]),
        "levels_black": _resolve_int(levels_black, "GUEST_FISH_LEVELS_BLACK", ("shape_detect", "levels_black"), _DEFAULTS["levels_black"]),
        "levels_white": _resolve_int(levels_white, "GUEST_FISH_LEVELS_WHITE", ("shape_detect", "levels_white"), _DEFAULTS["levels_white"]),
        "levels_gamma": _resolve_float(levels_gamma, "GUEST_FISH_LEVELS_GAMMA", ("shape_detect", "levels_gamma"), _DEFAULTS["levels_gamma"]),
        "ink_thresh": _resolve_int(ink_thresh, "GUEST_FISH_INK_THRESH", ("shape_detect", "ink_thresh"), _DEFAULTS["ink_thresh"]),
        "bg_blur": _resolve_int(bg_blur, "GUEST_FISH_BG_BLUR", ("shape_detect", "bg_blur"), _DEFAULTS["bg_blur"]),
        "close_px": _resolve_int(close_px, "GUEST_FISH_CLOSE_PX", ("shape_detect", "close_px"), _DEFAULTS["close_px"]),
        "smooth": _resolve_float(smooth, "GUEST_FISH_SMOOTH", ("shape_detect", "smooth"), _DEFAULTS["smooth"]),
        "trim_halo": _resolve_bool(trim_halo, "GUEST_FISH_TRIM_HALO", ("shape_detect", "trim_halo"), _DEFAULTS["trim_halo"]),
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


def _border_connected(mask: np.ndarray) -> np.ndarray:
    """bool マスクのうち、画像の縁に接している連結成分だけ True にして返す。"""
    from scipy import ndimage
    labels, n = ndimage.label(mask)
    if n == 0:
        return np.zeros_like(mask, dtype=bool)
    edge = np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    keep = set(int(v) for v in np.unique(edge))
    keep.discard(0)
    return np.isin(labels, list(keep)) if keep else np.zeros_like(mask, dtype=bool)


def _keep_main_blob(mask: np.ndarray, near_px: int) -> np.ndarray:
    """最大連結成分 + そこから near_px 以内にある他の成分も残す (= 体に近い独立したヒレ等を捨てない)。

    near_px <= 0 や成分が 1 つなら _largest_component と同じ。離れたゴミ (紙のシミ等) は捨てる。
    """
    from scipy import ndimage
    labels, n = ndimage.label(mask)
    if n <= 1 or not near_px or near_px <= 0:
        return _largest_component(mask)
    sizes = ndimage.sum(np.ones_like(labels), labels, index=range(1, n + 1))
    biggest = int(np.argmax(sizes)) + 1
    near = ndimage.binary_dilation(labels == biggest, iterations=int(near_px))
    keep_labels = np.unique(labels[near & (labels > 0)])
    return np.isin(labels, keep_labels)


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
    """紙の面 (ビネット・色かぶり込みの、なだらかに変化する成分) を推定して返す。

    インクや塗りつぶしで推定が汚染されないよう、まず「明るく彩度の低い = 紙」のピクセル
    だけ残して、それ以外 (線画・色塗り) は周囲の紙から inpaint で埋める → その後メディアン
    ぼかしで滑らかに。これで塗りつぶした絵でも「その下にあるはずの白い紙」を推定できる。
    blur=0 で自動 (窓は大きめ ~画像長辺の 7%、最小 31・最大 151)。
    """
    import cv2
    h, w = rgb.shape[:2]
    src = np.ascontiguousarray(rgb)
    v, s = _value_saturation(rgb)
    paper_mask = (v >= 195) & (s <= 35)               # 紙っぽいピクセル
    if 0.02 <= float(paper_mask.mean()) < 0.999:      # 紙が一定以上あり、かつ全部が紙ではない
        nonpaper = np.ascontiguousarray((~paper_mask).astype(np.uint8))
        src = cv2.inpaint(src, nonpaper, 5, cv2.INPAINT_TELEA)   # 線画・色塗りの所を周囲の紙で埋める
    if blur and blur > 0:
        k = int(blur)
    else:
        k = min(151, max(31, int(round(max(h, w) * 0.07))))
    if k % 2 == 0:
        k += 1
    return cv2.medianBlur(src, k)


def flat_field(rgb: np.ndarray, *, blur: int = 0, target: int = 245) -> np.ndarray:
    """紙の面 (なだらかな照明ムラ/色かぶり/ビネット) を _paper_background で推定し、それで割って
    target に正規化する (flat-field correction = ホワイトバランス兼デビネット)。

    効果: 紙が一様な明るさ target になり、ビネット・ピンク/紫かぶりが消え、黒インクが
    黒に、色が正しい色に出る。撮影の露出ムラ・ホワイトバランスのズレをまとめて補正。
    """
    bg = _paper_background(rgb, blur).astype(np.float32)
    np.maximum(bg, 1.0, out=bg)
    out = rgb.astype(np.float32) * (float(target) / bg)
    return np.clip(out, 0, 255).astype(np.uint8)


def levels(rgb: np.ndarray, *, black: int = 0, white: int = 255, gamma: float = 1.0) -> np.ndarray:
    """Photoshop の「レベル補正」相当: 入力レンジ [black, white] を [0, 255] に伸長 + 中間調 gamma。

    - black を上げる → それ以下が黒に締まる (インク/暗部が濃く、全体が暗く)
    - white を下げる → それ以上が白に飛ぶ (薄いグレー = ベタッとしたグレーが消えて白に)
    - gamma < 1 → 中間調が暗く / > 1 → 明るく
    """
    black, white, gamma = int(black), int(white), float(gamma)
    if black <= 0 and white >= 255 and abs(gamma - 1.0) < 1e-6:
        return rgb
    span = max(1, white - black)
    lin = np.clip((rgb.astype(np.float32) - black) / span, 0.0, 1.0)
    if abs(gamma - 1.0) >= 1e-6:
        lin = np.power(lin, 1.0 / max(gamma, 1e-3))
    return np.clip(lin * 255.0 + 0.5, 0, 255).astype(np.uint8)


def ink_mask(rgb: np.ndarray, *, thresh: int, blur: int = 0) -> np.ndarray:
    """背景差分でインク (色・明るさ問わず) を拾った bool マスク。

    紙の面を _paper_background で推定し、元画像との残差 (チャンネルごとの差の最大) が
    thresh 以上をインクとみなす。なだらかな照明ムラ (ビネット・ピンクかぶり) は残差が
    ほぼ 0 なので除外される。
    """
    bg = _paper_background(rgb, blur)
    resid = np.abs(rgb.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    return resid >= int(thresh)


def _bridge_endpoint_gaps(ink: np.ndarray, max_gap: int) -> np.ndarray:
    """ink (bool マスク) を骨格化して線の端点 (行き止まり) を求め、向き合っていて max_gap px 以内の
    端点ペアを「直線」で繋いで輪郭の隙間 (開いた口・ヒレの切れ目・ペンの途切れ) を閉じる。

    モルフォロジーの太い円弧と違い直線なので、輪郭線の外に白が膨らまない。各端点の「線が伸びる
    方向」を求め、相手がその先にいるペアだけ繋ぐ (内側の模様の線端を輪郭に繋いだりしない)。
    元の ink に直線を足した bool マスクを返す。
    """
    if max_gap is None or max_gap <= 0:
        return ink
    import cv2
    from scipy import ndimage
    src = np.ascontiguousarray((ink.astype(np.uint8)) * 255)
    try:
        sk = cv2.ximgproc.thinning(src)  # 1px 骨格 (0/255)
    except Exception:
        return ink
    skb = sk > 0
    if not skb.any():
        return ink
    nbr = ndimage.convolve(skb.astype(np.int16), np.ones((3, 3), np.int16), mode="constant") - skb.astype(np.int16)
    eps = np.argwhere(skb & (nbr == 1)).astype(int)  # (y, x)
    if len(eps) < 2:
        return ink
    if len(eps) > 240:
        eps = eps[:240]
    H, W = skb.shape
    win = 8
    dirs = np.zeros((len(eps), 2), float)  # 各端点の forward 方向 (線が伸びていく向き)
    for idx, (y, x) in enumerate(eps):
        y0, y1, x0, x1 = max(0, y - win), min(H, y + win + 1), max(0, x - win), min(W, x + win + 1)
        ys, xs = np.where(skb[y0:y1, x0:x1])
        if len(ys) == 0:
            continue
        cy, cx = ys.mean() + y0, xs.mean() + x0   # 窓内の骨格の重心
        v = np.array([y - cy, x - cx], float)      # 重心 → 端点 = forward
        nrm = float(np.hypot(*v))
        if nrm > 1e-3:
            dirs[idx] = v / nrm
    out = (ink.astype(np.uint8)).copy()
    n = len(eps)
    g2 = int(max_gap) * int(max_gap)
    cand = []
    for i in range(n):
        for j in range(i + 1, n):
            dy = float(eps[i][0] - eps[j][0])
            dx = float(eps[i][1] - eps[j][1])
            d2 = dy * dy + dx * dx
            if d2 == 0 or d2 > g2:
                continue
            d = d2 ** 0.5
            vij = np.array([-dy, -dx], float) / d   # i → j
            if float(dirs[i] @ vij) > 0.05 and float(dirs[j] @ (-vij)) > 0.05:
                cand.append((d2, i, j))
    cand.sort()
    used = np.zeros(n, bool)
    for _d2, i, j in cand:
        if used[i] or used[j]:
            continue
        used[i] = used[j] = True
        cv2.line(out, (int(eps[i][1]), int(eps[i][0])), (int(eps[j][1]), int(eps[j][0])), 1, thickness=3)
    return out.astype(bool)


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


def fish_mask(rgb: np.ndarray, *, ink_thresh: int, bg_blur: int = 0, close_px: int = 40,
              smooth: float = 0.0, trim_halo: bool = True) -> np.ndarray:
    """魚の形 (中身まで満たした) の bool マスクを返す。alpha に使う。色 (RGB) は呼び出し側で元のまま。

    段階1: インク検出 → 骨格化して線の端点を求め、~2*close_px px 以内の端点ペアを「直線」で繋いで
      輪郭の隙間 (開いた口・ヒレの切れ目・ペンの途切れ) を閉じる → 残った微小な隙間だけ小さな close →
      最大連結成分 → 穴埋め (= ベタ面)。直線で閉じ切らず中空ならモルフォロジー (太い円弧) でフォールバック。
      → 元のインクを足し戻す + 体に近い独立成分 (体の外に描いた腹びれ等) も残す。
    段階2 (trim_halo): 透明に隣接する「白」(= インクでも橋渡し線でもない部分) を、インクや橋渡し線に
      ぶつかるまで削る (= 輪郭線の外に膨らんだ白を仕上げで取り除く)。橋渡し線にはぶつかって止まるので
      「線が繋がってなくて繋いだ所」は削らない。体内部まで漏れる (= 隙間が塞がってなかった) 場合はスキップ。
    """
    from scipy import ndimage
    ink = ink_mask(rgb, thresh=ink_thresh, blur=bg_blur)
    k = max(0, int(close_px))
    if k > 0:
        bridged = _bridge_endpoint_gaps(ink, max_gap=2 * k)            # 隙間を直線で閉じる (太らせない)
        sealed = ndimage.binary_closing(bridged, structure=np.ones((3, 3)), iterations=2)  # 残った ~2px の隙間だけ
        lc = _largest_component(sealed)
        body = ndimage.binary_fill_holes(lc)                           # 閉じた輪郭の中を満たす
        if body.sum() < 2.2 * int(lc.sum()):
            # 直線つなぎで輪郭が閉じ切らず中空 → モルフォロジー (太い円弧) でフォールバック
            grown = _largest_component(ndimage.binary_dilation(bridged, iterations=k))
            body = ndimage.binary_erosion(ndimage.binary_fill_holes(grown), iterations=k, border_value=1)
    else:
        bridged = ink
        body = ndimage.binary_fill_holes(_largest_component(ink))
    if smooth and smooth > 0:
        body = _smooth_mask(body, smooth)
    mask = _keep_main_blob(body | ink, near_px=max(8, k))

    if trim_halo:
        barrier = ndimage.binary_dilation(bridged, iterations=1)       # インク + 橋渡し線 (+1px の余裕)
        passable = mask & ~barrier                                     # 削れる候補 (白の太り部分 + 体内部)
        reach = _border_connected((~mask) | passable)                  # 画像の縁 (透明) から passable を通って届く所
        removable = reach & mask & passable                            # 透明に隣接する白
        if 0 < removable.sum() < 0.5 * mask.sum():                     # 体内部まで漏れてない場合だけ適用
            mask = _keep_main_blob((mask & ~removable) | (ink & mask), near_px=max(8, k))
    return mask


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


def prepare_rgb(img: Image.Image, **kw) -> np.ndarray:
    """切り抜き前処理: EXIF 補正 + RGB 化 + (white_balance なら flat-field) + レベル補正 + (autocontrast なら自動色補正)
    した RGB 配列を返す。kw は resolve_params に渡す (bg_blur / white_balance / wb_target / levels_* / autocontrast*)。

    tune_guest_fish.py がマスク可視化を本処理と同じ画像で行うために共有する。
    """
    p = resolve_params(bg_method="shape", **kw)
    rgb = np.array(ImageOps.exif_transpose(img).convert("RGB"))
    if p["white_balance"]:
        rgb = flat_field(rgb, blur=p["bg_blur"], target=p["wb_target"])
    rgb = levels(rgb, black=p["levels_black"], white=p["levels_white"], gamma=p["levels_gamma"])
    if p["autocontrast"]:
        rgb = np.array(ImageOps.autocontrast(Image.fromarray(rgb), cutoff=max(0, int(p["autocontrast_cutoff"])),
                                             preserve_tone=False))
    return rgb


def cutout_guest_fish(img: Image.Image, **kw) -> Image.Image:
    """形ベースで魚を切り抜いた RGBA を返す。輪郭の内側は前処理後の写真のまま、外側は透明。kw は resolve_params 用。"""
    p = resolve_params(bg_method="shape", **kw)
    rgb = prepare_rgb(img, white_balance=p["white_balance"], wb_target=p["wb_target"], bg_blur=p["bg_blur"],
                      levels_black=p["levels_black"], levels_white=p["levels_white"], levels_gamma=p["levels_gamma"],
                      autocontrast=p["autocontrast"], autocontrast_cutoff=p["autocontrast_cutoff"])
    mask = fish_mask(rgb, ink_thresh=p["ink_thresh"], bg_blur=p["bg_blur"], close_px=p["close_px"], smooth=p["smooth"], trim_halo=p["trim_halo"])
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


def remove_white_background(img: Image.Image, **kw) -> Image.Image:
    """ゲスト魚を切り抜いた RGBA を返す。bg_method ("shape" 既定 / "hsv") で方式を選ぶ。

    kw は resolve_params に渡す (省略したものは env / config.json / 既定値で解決。モジュール docstring 参照)。
    本番 (固定カメラ + 紙だけが映る撮影ブース) を前提。

    - "shape": 自動色補正 (autocontrast) や紙の正規化 (white_balance/flat-field) で色を整え → 背景差分で
      インクを拾い → 輪郭の隙間を close_px で橋渡し → 中を満たし → ベタ面を smooth で整える + 元インク足し戻し。
      輪郭の内側は前処理後の写真のまま、外側は透明。
    - "hsv" (旧): HSV しきい値で「縁から繋がる白」を透明化。fill_body=True なら魚の中身も埋める。
    """
    params = resolve_params(**kw)
    long_edge = params["long_edge"]

    if params["bg_method"] == "shape":
        return cutout_guest_fish(img, **{k: params[k] for k in (
            "white_balance", "wb_target", "levels_black", "levels_white", "levels_gamma",
            "autocontrast", "autocontrast_cutoff", "ink_thresh", "bg_blur", "close_px", "smooth", "trim_halo", "long_edge")})

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
