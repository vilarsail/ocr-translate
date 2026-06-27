"""glm-ocr backend (ollama native /api/generate endpoint).

glm-ocr is a completion model (architecture `glmocr`) with custom
RENDERER/PARSER. The OpenAI-compatible /v1/chat/completions endpoint does
not reliably inject images for this architecture — use the native
/api/generate endpoint with the `images` field instead.

Key knobs:
- num_ctx: ollama defaults to 4096 which is too small for full-page images;
  we bump it to 32768.
- num_predict: cap output to avoid runaway generation (model sometimes
  emits infinite <|begin_of_image|> tokens on cold start).
- keep_alive: keep model in VRAM between calls to avoid repeated cold starts.
- garbage detection + retry: cold start occasionally produces empty HTML
  tables or image tokens; retry up to 3 times.
"""
import base64
import json
import threading
import urllib.request
from typing import Callable, Optional

from .config import OLLAMA_BASE_URL, OLLAMA_MODEL, DEFAULT_OCR_TIMEOUT

_lock = threading.Lock()

# ollama 原生 endpoint
_GENERATE_URL = OLLAMA_BASE_URL.rstrip("/").replace("/v1", "") + "/api/generate"

# 垃圾输出特征：模型冷启动时可能输出这些
_GARBAGE_MARKERS = ["<|begin_of_image|>", "<table", "<tr>", "<td>"]


def _is_garbage(text: str) -> bool:
    if not text or not text.strip():
        return True
    low = text.lower()
    garbage_hits = sum(1 for m in _GARBAGE_MARKERS if m in low)
    if garbage_hits >= 2:
        return True
    # 几乎全是 HTML 标签
    stripped = low
    for m in _GARBAGE_MARKERS + ["</tr>", "</td>", "</table>"]:
        stripped = stripped.replace(m, "")
    if len(text) > 50 and len(stripped.strip()) < len(text) * 0.3:
        return True
    return False


def _build_prompt(hint: str) -> str:
    if hint == "toc":
        return (
            "请提取图中目录页的所有文字，按从上到下、从左到右的阅读顺序逐行输出。"
            "保留每个条目末尾的页码数字。仅输出图中可见文字，不要解释。"
        )
    return (
        "请提取图中正文页的所有文字，按阅读顺序输出纯文本，保留段落换行。"
        "仅输出图中可见文字，不要解释。"
    )


def ocr_page(
    image_bytes: bytes,
    hint: str = "body",
    timeout: Optional[float] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    log_prefix: str = "",
) -> str:
    """Send image to ollama glm-ocr; return extracted text.

    hint: 'toc' or 'body' — picks the prompt.
    timeout: per-call timeout; defaults to DEFAULT_OCR_TIMEOUT.
    log_fn: optional (level, message) callback for progress logging.
    Retries up to 3 times on empty/garbage output or HTTP error.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = _build_prompt(hint)
    to = timeout or DEFAULT_OCR_TIMEOUT

    last_response = ""
    last_error: Optional[Exception] = None
    for attempt in range(3):
        if log_fn:
            log_fn("info", f"{log_prefix} OCR 第 {attempt + 1}/3 次尝试...")
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {
                "temperature": 0,
                "num_ctx": 32768,
                "num_predict": 2048,
            },
            "keep_alive": "30m",
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _GENERATE_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _lock:
                with urllib.request.urlopen(req, timeout=to) as r:
                    resp = json.loads(r.read().decode("utf-8"))
            text = resp.get("response", "") or ""
            if not _is_garbage(text):
                if log_fn:
                    log_fn("info", f"{log_prefix} OCR 成功（第 {attempt + 1} 次），{len(text)} 字符。")
                return text
            last_response = text
            if log_fn:
                log_fn("warn", f"{log_prefix} OCR 第 {attempt + 1} 次输出疑似垃圾（{len(text)} 字符），重试。")
        except Exception as e:
            last_error = e
            if log_fn:
                log_fn("warn", f"{log_prefix} OCR 第 {attempt + 1} 次失败: {e}")
            if attempt == 2:
                raise

    if last_error:
        raise last_error
    return last_response
