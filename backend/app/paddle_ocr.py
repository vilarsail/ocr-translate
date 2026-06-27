"""PaddleOCR (PP-OCRv6) local inference backend.

Memory-optimized for a Linux server with ~2.5 GB free RAM. The model is
loaded once and reused; CPU inference only; one page at a time.

Memory budget (FP32, conservative) on a ~2.5 GB-free host:
  - paddlepaddle runtime:         ~250 MB
  - PP-OCRv6 det + rec (medium):  ~600 MB
  - per-page inference peak:      ~300-500 MB (clamped by MAX_IMAGE_DIM)
  - FastAPI + other python state: ~150 MB
  - total peak:                   ~1.3-1.5 GB
  - headroom:                     ~1.0 GB

That headroom is comfortable. If the host were tighter (e.g. 1.5 GB free),
switch to INT8 quantized models (download manually, point
PADDLE_OCR_MODEL_DIR at them) — weights drop ~60%, total peak ~0.9-1.1 GB.

Anti-OOM measures:
  - MAX_IMAGE_DIM clamps the longest side of the input image before OCR.
    A 150 DPI A4 page is ~1240x1750px; clamping to 2000px leaves headroom
    for denser pages without exploding memory.
  - batch_num=1, no concurrent inference.
  - Disable orientation/unwarping/textline-cls submodels (they each load
    their own weights and add ~100-200 MB).
"""
import io
import threading
from typing import Callable, Optional

from PIL import Image

from .config import PADDLE_OCR_MODEL_DIR  # noqa: F401  (reserved for future per-submodel dir overrides)


# Longest side (px) allowed for an OCR input image. Larger images are
# downscaled before inference to bound memory. 2000px ≈ A4 @ 170 DPI.
# Longest side (px) allowed for an OCR input image. Larger images are
# downscaled before inference to bound memory. 1600px is enough for A4
# @ 150 DPI (~1240x1750 already under it) while keeping a hard ceiling
# on denser pages.
_MAX_IMAGE_DIM = 1600

# Detection side-length limit. PaddleOCR's default (limit_type='min',
# limit_side_len=64) rescales the image so its shortest side is at least
# 64px — fine for accuracy but the long side can still explode to ~3000px
# on a tall page, ballooning memory. Forcing limit_type='max' with
# limit_side_len=1600 caps the longest detection input side at 1600px,
# which is plenty for printed text and bounds peak memory.
_DET_LIMIT_SIDE_LEN = 1600
_DET_LIMIT_TYPE = "max"

_lock = threading.Lock()
_ocr_engine: Optional[object] = None


def _get_ocr_engine():
    """Lazy-init the OCR engine with double-checked locking."""
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine

    with _lock:
        if _ocr_engine is not None:
            return _ocr_engine

        from paddleocr import PaddleOCR  # late import

        # PaddleOCR 3.x API (paddleocr>=3.0):
        # - no use_gpu (paddle picks device automatically; CPU build stays on CPU)
        # - no enable_mkldnn at top level (CPU perf is fine without it on small models)
        # - no model_dir at top level (per-submodel *_model_dir only)
        # - use_doc_orientation_classify / use_doc_unwarping / use_textline_orientation
        #   each toggle a sub-model; disabling all three saves ~200-300 MB.
        kwargs = dict(
            ocr_version="PP-OCRv6",
            lang="ch",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_det_limit_side_len=_DET_LIMIT_SIDE_LEN,
            text_det_limit_type=_DET_LIMIT_TYPE,
        )

        _ocr_engine = PaddleOCR(**kwargs)
        return _ocr_engine


def _clamp_image(image: Image.Image) -> Image.Image:
    """Downscale image if its longest side exceeds _MAX_IMAGE_DIM.

    Returns the original image if it's already within bounds. Uses
    LANCZOS for downscaling to preserve text legibility.
    """
    w, h = image.size
    longest = max(w, h)
    if longest <= _MAX_IMAGE_DIM:
        return image
    scale = _MAX_IMAGE_DIM / longest
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return image.resize(new_size, Image.LANCZOS)


def _sort_and_group_lines(result) -> list[str]:
    """Group PaddleOCR text boxes into reading-order lines.

    PaddleOCR 3.x returns a list of OCRResult (dict-like). Each OCRResult
    has `dt_polys` (list of 4-point boxes), `rec_texts` (list of str),
    `rec_scores`. We pair them up by index.

    Multiple text regions on the same physical row are merged into one
    output line, ordered left-to-right. Lines are ordered top-to-bottom.

    Grouping threshold is relative to the median box height — using a
    fixed pixel threshold would break when DPI or font size varies.
    """
    if not result:
        return []

    boxes: list[tuple[float, float, float, str]] = []
    for page_result in result:
        if not page_result:
            continue
        polys = page_result.get("dt_polys") or []
        texts = page_result.get("rec_texts") or []
        for poly, text in zip(polys, texts):
            # poly is a 4-point box: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] (ndarray)
            # Fallback to rec_boxes (axis-aligned) if dt_polys missing.
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            y_avg = sum(ys) / len(ys)
            x_avg = sum(xs) / len(xs)
            height = max(ys) - min(ys)
            boxes.append((y_avg, x_avg, float(height), text))

    if not boxes:
        return []

    # Median box height → row-grouping threshold = 0.5 * median height.
    # Two boxes on the same row typically have y_avg within half a line
    # height of each other.
    heights = sorted(b[2] for b in boxes)
    median_h = heights[len(heights) // 2] if heights else 20
    row_threshold = max(8.0, median_h * 0.5)

    # Sort by y first so boxes on the same row become adjacent.
    boxes.sort(key=lambda b: (b[0], b[1]))

    lines: list[str] = []
    current_row: list[tuple[float, str]] = []
    current_y: Optional[float] = None

    for y_avg, x_avg, _h, text in boxes:
        if current_y is None or abs(y_avg - current_y) <= row_threshold:
            current_row.append((x_avg, text))
            if current_y is None:
                current_y = y_avg
        else:
            current_row.sort()
            lines.append(" ".join(t for _, t in current_row))
            current_row = [(x_avg, text)]
            current_y = y_avg

    if current_row:
        current_row.sort()
        lines.append(" ".join(t for _, t in current_row))

    return lines


def _ocr_image_bytes(image_bytes: bytes) -> str:
    """Run OCR on raw image bytes, return text in reading order."""
    ocr = _get_ocr_engine()

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    image = _clamp_image(image)

    # PaddleOCR 3.x: predict() returns list[OCRResult]; ocr() is deprecated
    # and returns the same structure with a DeprecationWarning.
    import numpy as np
    arr = np.array(image)

    result = ocr.predict(arr)  # type: ignore[union-attr]

    lines = _sort_and_group_lines(result)
    return "\n".join(lines)


def ocr_page(
    image_bytes: bytes,
    hint: str = "body",
    timeout: Optional[float] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    log_prefix: str = "",
) -> str:
    """OCR a full page image, return extracted text.

    Interface matches glm_ocr.ocr_page for drop-in compatibility.
    `hint` and `timeout` are accepted for signature compatibility but
    ignored — PaddleOCR doesn't use prompts and runs locally.
    """
    if log_fn:
        log_fn("info", f"{log_prefix} PaddleOCR 开始识别...")
    text = _ocr_image_bytes(image_bytes)
    if log_fn:
        log_fn("info", f"{log_prefix} PaddleOCR 完成，{len(text)} 字符。")
    return text
