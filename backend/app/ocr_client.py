"""OCR client — dispatches to a backend chosen per call.

Backends:
- glm:    ollama glm-ocr via HTTP (see glm_ocr.py)
- paddle: local PaddleOCR PP-OCRv6 inference (see paddle_ocr.py)

The backend is selected per OCR stage invocation — the caller passes
`backend="glm" | "paddle"` through to ocr_page(). When unset, falls back
to OCR_BACKEND from config (default "paddle").

Each backend module is imported lazily on first use and then cached for
the lifetime of the process. Note: once paddle is loaded, its model
weights stay resident (~600 MB) even if later calls switch to glm — this
is intentional, reloading the model per call would be far too slow. If
you need to free paddle's memory you must restart the backend process.
"""
from typing import Any, Callable, Optional

from .config import OCR_BACKEND

_modules: dict[str, Any] = {}


def _module(backend: str):
    """Lazily import and cache the requested backend module."""
    m = _modules.get(backend)
    if m is not None:
        return m

    if backend == "paddle":
        from . import paddle_ocr as m
    elif backend == "glm":
        from . import glm_ocr as m
    else:
        raise ValueError(
            f"backend={backend!r} 不支持，可选: 'glm' | 'paddle'"
        )
    _modules[backend] = m
    return m


def ocr_page(
    image_bytes: bytes,
    hint: str = "body",
    timeout: Optional[float] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    log_prefix: str = "",
    backend: Optional[str] = None,
) -> str:
    """OCR a full page image, return extracted text.

    Dispatches to the requested backend. When `backend` is None, falls
    back to OCR_BACKEND from config.
    """
    b = backend or OCR_BACKEND
    return _module(b).ocr_page(
        image_bytes,
        hint=hint,
        timeout=timeout,
        log_fn=log_fn,
        log_prefix=log_prefix,
    )
