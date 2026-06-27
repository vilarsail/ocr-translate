import base64
import json
import random
import re
import threading
import time
from openai import OpenAI

from .config import API_KEY_PATH, MAX_RETRIES_PER_MODEL, MODELS_TO_TRY, NVIDIA_BASE_URL


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove `<think>...</think>` blocks some models emit in thinking mode."""
    return _THINK_RE.sub("", text)


class ClientPool:
    """Round-robin pool of OpenAI clients, one per API key line."""

    def __init__(self):
        self._clients: list[OpenAI] = []
        self._index = 0
        self._lock = threading.Lock()
        self.load()

    def load(self):
        keys = self._read_keys()
        self._clients = [
            OpenAI(base_url=NVIDIA_BASE_URL, api_key=k, timeout=60.0) for k in keys
        ]
        self._index = 0
        return len(self._clients)

    def _read_keys(self) -> list[str]:
        try:
            with open(API_KEY_PATH, "r", encoding="utf-8") as f:
                keys = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []
        if not keys:
            raise ValueError(f"API key 文件为空: {API_KEY_PATH}")
        return keys

    def count(self) -> int:
        return len(self._clients)

    def next(self) -> OpenAI:
        with self._lock:
            if not self._clients:
                raise RuntimeError("无可用 API client")
            client = self._clients[self._index % len(self._clients)]
            self._index += 1
            return client


_pool: ClientPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ClientPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ClientPool()
        return _pool


def reload_pool() -> int:
    global _pool
    with _pool_lock:
        _pool = ClientPool()
        return _pool.count()


def extract_json_object(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            json_str = text[start:end + 1]
        else:
            raise ValueError("未在返回结果中找到合法的 JSON 结构。")
    return json.loads(json_str)


def _stream_completion(client: OpenAI, model_name: str, system_prompt: str,
                       user_content: str, temperature: float, max_tokens: int,
                       timeout: float, extra_body: dict | None = None) -> str:
    """Stream a chat completion and return concatenated text. honors think mode via extra_body."""
    if extra_body:
        stream = client.with_options(timeout=timeout).chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            top_p=0.95,
            max_tokens=max_tokens,
            stream=True,
            extra_body=extra_body,
        )
    else:
        stream = client.with_options(timeout=timeout).chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            top_p=0.95,
            max_tokens=max_tokens,
            stream=True,
        )

    result_text = ""
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        if chunk.choices and chunk.choices[0].delta.content is not None:
            result_text += chunk.choices[0].delta.content
    return result_text


def _think_extra_body() -> dict:
    """Enable thinking mode for models that support chat_template_kwargs."""
    return {"chat_template_kwargs": {"think": True}}


def call_text(
    system_prompt: str,
    user_content: str,
    log_fn,
    log_prefix: str,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    should_stop=None,
) -> str | None:
    """Call LLM with multi-model fallback; return raw text (think段已剥离).

    Returns None if all models fail or if stopped.
    """
    pool = get_pool()
    extra_body = _think_extra_body()

    for model_config in MODELS_TO_TRY:
        if should_stop and should_stop():
            log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
            return None
        model_name = model_config["name"]
        timeout = model_config.get("timeout", 600)
        log_fn("info", f"{log_prefix} [LLM] 尝试模型: {model_name}（think，超时 {timeout}s）")

        for attempt in range(MAX_RETRIES_PER_MODEL):
            if should_stop and should_stop():
                log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
                return None
            client = pool.next()
            try:
                raw = _stream_completion(
                    client, model_name, system_prompt, user_content,
                    temperature=temperature, max_tokens=max_tokens,
                    timeout=timeout, extra_body=extra_body,
                )
                if should_stop and should_stop():
                    log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，丢弃已返回结果。")
                    return None

                text = strip_think(raw).strip()
                if not text:
                    raise ValueError("返回内容为空（可能 think 段被剥离后无剩余文本）")

                log_fn("info", f"{log_prefix} [LLM] 请求成功 ({model_name})，返回 {len(text)} 字符。")
                return text

            except Exception as e:
                log_fn("warn", f"{log_prefix} [Warning] {model_name} 尝试 {attempt + 1}/{MAX_RETRIES_PER_MODEL} 失败: {e}")
                if attempt < MAX_RETRIES_PER_MODEL - 1:
                    sleep_time = (2 ** attempt) * 5 + random.uniform(0, 5)
                    log_fn("info", f"{log_prefix} 等待 {sleep_time:.1f}s 后重试该模型...")
                    _interruptible_sleep(sleep_time, should_stop)
                    if should_stop and should_stop():
                        log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
                        return None
                else:
                    log_fn("error", f"{log_prefix} [Error] {model_name} 多次失败，切换下一个模型...")

    log_fn("error", f"{log_prefix} [Error] 所有备选模型均已失败，放弃当前批次。")
    return None


def call_json(
    system_prompt: str,
    user_content: str,
    expected_keys: set,
    log_fn,
    log_prefix: str,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    should_stop=None,
) -> dict | None:
    """Call LLM and parse JSON; verify expected_keys present. Returns dict or None."""
    pool = get_pool()
    extra_body = _think_extra_body()

    for model_config in MODELS_TO_TRY:
        if should_stop and should_stop():
            log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
            return None
        model_name = model_config["name"]
        timeout = model_config.get("timeout", 600)
        log_fn("info", f"{log_prefix} [LLM] 尝试模型: {model_name}（think，超时 {timeout}s）")

        for attempt in range(MAX_RETRIES_PER_MODEL):
            if should_stop and should_stop():
                log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
                return None
            client = pool.next()
            try:
                raw = _stream_completion(
                    client, model_name, system_prompt, user_content,
                    temperature=temperature, max_tokens=max_tokens,
                    timeout=timeout, extra_body=extra_body,
                )
                if should_stop and should_stop():
                    log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，丢弃已返回结果。")
                    return None

                cleaned = strip_think(raw)
                data = extract_json_object(cleaned)
                missing_keys = expected_keys - set(data.keys())
                if missing_keys:
                    raise ValueError(f"完整性校验失败：缺少键 {missing_keys}")

                log_fn("info", f"{log_prefix} [LLM] 请求成功 ({model_name})，解析 {len(data)} 项。")
                return data

            except Exception as e:
                log_fn("warn", f"{log_prefix} [Warning] {model_name} 尝试 {attempt + 1}/{MAX_RETRIES_PER_MODEL} 失败: {e}")
                if attempt < MAX_RETRIES_PER_MODEL - 1:
                    sleep_time = (2 ** attempt) * 5 + random.uniform(0, 5)
                    log_fn("info", f"{log_prefix} 等待 {sleep_time:.1f}s 后重试该模型...")
                    _interruptible_sleep(sleep_time, should_stop)
                    if should_stop and should_stop():
                        log_fn("warn", f"{log_prefix} [LLM] 收到停止请求，放弃当前批次。")
                        return None
                else:
                    log_fn("error", f"{log_prefix} [Error] {model_name} 多次失败，切换下一个模型...")

    log_fn("error", f"{log_prefix} [Error] 所有备选模型均已失败，放弃当前批次。")
    return None


def _interruptible_sleep(seconds: float, should_stop=None):
    if not should_stop:
        time.sleep(seconds)
        return
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop():
            return
        time.sleep(min(0.5, deadline - time.time()))


def encode_image_b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("ascii")
