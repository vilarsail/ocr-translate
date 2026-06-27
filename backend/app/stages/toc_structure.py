"""Stage: toc-structure.

Read toc-ocr/*.txt, ask LLM to produce a hierarchical TOC JSON:
  {"entries": [{level, title, page_hint, match_keywords}, ...]}
Write toc-structure.json.
"""
import json
from pathlib import Path

from .. import llm_client, workspace
from ..logbus import logbus

SYSTEM_PROMPT = (
    "你是一名精通排版与目录结构的中文图书编辑助手。"
    "用户会给你通过 OCR 提取的目录页文字（可能含噪、错字、缩进信息丢失），"
    "你的任务是还原出层级化的目录树。"
    "只输出 JSON，不要任何解释、不要 markdown 代码块。"
)

USER_PROMPT_TEMPLATE = """下面是某本书目录页的 OCR 结果（按页拼接，每页前有「--- 第 N 页 ---」标记）：

<<<TOC_TEXT_BEGIN
{toc_text}
TOC_TEXT_END>>>

请输出层级化目录 JSON，结构为：
{{
  "entries": [
    {{
      "level": 1,                       // 1=章/卷/部分等最高级别
      "title": "第一章 概论",            // 完整标题，去除前后空白与页码
      "page_hint": 12,                  // 目录中标注的页码（int，没有就给 0 或 null）
      "match_keywords": ["第一章", "概论"]  // 用于在正文 OCR 中匹配该章节起始的关键词，2-4 个，越独特越好
    }},
    {{
      "level": 2,
      "title": "第一节 定义",
      "page_hint": 12,
      "match_keywords": ["第一节", "定义"]
    }}
  ]
}}

要求：
1. 严格按 OCR 中的出现顺序输出条目；
2. level 仅取 1/2/3，不要更深层级；如目录无层级，全部给 1；
3. title 不要包含页码、不要包含前后导点线（....）；
4. match_keywords 选取标题中独特的字词组合；
5. 即使 OCR 质量差也要尽力输出，宁缺勿错；
6. 只输出 JSON 对象本身。
"""


def _collect_toc_text(project_root: Path) -> tuple[str, int]:
    toc_dir = project_root / "toc-ocr"
    if not toc_dir.exists():
        return "", 0
    pages = []
    files = sorted(toc_dir.glob("*.txt"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        pages.append(f"--- 第 {fp.stem} 页 ---\n{text}")
    return "\n\n".join(pages), len(files)


def run(project_id: str, should_stop) -> bool:
    log_prefix = "[toc-structure]"
    root = workspace.project_root(project_id)

    toc_text, page_count = _collect_toc_text(root)
    if not toc_text.strip():
        logbus.log(project_id, "toc-structure", "toc-ocr 目录为空，无法分析结构。", level="error")
        workspace.set_stage(project_id, "toc-structure", "failed", error="empty toc-ocr")
        return False

    logbus.log(project_id, "toc-structure",
               f"读取 {page_count} 页目录 OCR 文本，共 {len(toc_text)} 字符，开始调用 LLM 分析结构...")

    def _log(level, message):
        logbus.log(project_id, "toc-structure", message, level=level)

    user_prompt = USER_PROMPT_TEMPLATE.format(toc_text=toc_text)
    data = llm_client.call_json(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_prompt,
        expected_keys={"entries"},
        log_fn=_log,
        log_prefix=log_prefix,
        temperature=0.2,
        max_tokens=8192,
        should_stop=should_stop,
    )
    if data is None:
        workspace.set_stage(project_id, "toc-structure", "failed", error="llm failed")
        return False

    entries = data.get("entries") or []
    if not isinstance(entries, list) or not entries:
        logbus.log(project_id, "toc-structure", "LLM 返回的 entries 为空。", level="error")
        workspace.set_stage(project_id, "toc-structure", "failed", error="empty entries")
        return False

    level1_count = sum(1 for e in entries if e.get("level") == 1)
    if level1_count == 0:
        logbus.log(project_id, "toc-structure", "无 level=1 条目，结构无效。", level="error")
        workspace.set_stage(project_id, "toc-structure", "failed", error="no level1 entry")
        return False

    out_path = root / "toc-structure.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)

    logbus.log(project_id, "toc-structure",
               f"结构分析完成：{len(entries)} 条目，其中 {level1_count} 个一级标题。"
               f"写入 {out_path.name}")
    workspace.set_stage(project_id, "toc-structure", "done",
                        total_entries=len(entries),
                        level1_count=level1_count)
    return True
