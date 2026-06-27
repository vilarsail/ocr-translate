import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
WORKSPACES_DIR = BASE_DIR / "workspaces"

API_KEY_PATH = os.path.expanduser("~/.config/nvidia_api.key")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# OCR 后端默认值: "glm" (ollama glm-ocr) 或 "paddle" (本地 PaddleOCR)
# 单次 stage 调用可通过请求参数 backend 覆盖；这里只决定不传时的回退值。
OCR_BACKEND = os.environ.get("OCR_BACKEND", "paddle")

# glm-ocr 配置（ollama 本地服务）
OLLAMA_BASE_URL = os.environ.get("OCR_OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OCR_OLLAMA_MODEL", "glm-ocr")

# PaddleOCR 配置
# 模型目录，若不指定则自动下载到 ~/.paddleocr/
# (PaddleOCR 3.x 用 text_detection_model_dir / text_recognition_model_dir
# 等子模型目录，目前未启用自定义目录，保留此变量供未来使用)
PADDLE_OCR_MODEL_DIR = os.environ.get("PADDLE_OCR_MODEL_DIR", "")

# OCR 渲染 DPI（每页转 PDF 的清晰度）
# - glm-ocr: 150 DPI 平衡质量和 token 数量
# - PaddleOCR: 150 DPI 足够，建议不要超过 180
#   150 DPI A4 ≈ 1240x1750px，已能覆盖 PaddleOCR 的最佳输入范围；
#   超过 200 DPI 不仅不会提升识别率，还会显著增加内存占用。
OCR_RENDER_DPI = int(os.environ.get("OCR_RENDER_DPI", "150"))

# 长前缀遮蔽（与 B-T-translate 不同的随机串，避免同时部署撞前缀）
API_PREFIX = os.environ.get(
    "OCR_API_PREFIX",
    "/ocrsys-7e2f9a4d1c8b3e6f5a0d2b9c7e4f1a8d3b6e9c0d5a2f7b8e1c4d6a9f3b5e0c2d",
)
SERVER_PORT = int(os.environ.get("OCR_PORT", "7013"))
SERVER_HOST = os.environ.get("OCR_HOST", "0.0.0.0")

# LLM 多模型 fallback 顺序：deepseek-v4-pro（think） → glm-5.1（think）
# → minimax-2.7（think） → deepseek-v4-flash（think）
# 通过 extra_body 透传 chat_template_kwargs.think=true 开启思考模式。
MODELS_TO_TRY = [
    {"name": "deepseek-ai/deepseek-v4-pro", "timeout": 600},
    {"name": "z-ai/glm-5.1", "timeout": 600},
    {"name": "minimaxai/minimax-m2.7", "timeout": 600},
    {"name": "deepseek-ai/deepseek-v4-flash", "timeout": 400},
]

MAX_RETRIES_PER_MODEL = 2
DEFAULT_OCR_WORKERS = 1
DEFAULT_OCR_TIMEOUT = 600  # OCR 单页超时

STAGES = [
    "toc-ocr",
    "toc-structure",
    "body-ocr",
    "organize",
    "verify",
    "export-md",
]
