"""Stage: export-md.

Concatenate chapters/*-chapter.md (sorted by filename) into merged/book.md,
separated by horizontal rules. Synchronous, fast.
"""
import re

from .. import workspace
from ..logbus import logbus


def _chapter_index(name: str) -> int:
    m = re.match(r"(\d+)-chapter\.md$", name)
    return int(m.group(1)) if m else 0


def run(project_id: str) -> dict:
    root = workspace.project_root(project_id)
    chapters_dir = root / "chapters"
    merged_dir = root / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)

    if not chapters_dir.exists():
        logbus.log(project_id, "export-md", "chapters/ 目录不存在。", level="error")
        workspace.set_stage(project_id, "export-md", "failed", error="no chapters dir")
        return {"ok": False, "error": "no chapters dir"}

    md_files = sorted(
        [p for p in chapters_dir.glob("*-chapter.md")],
        key=lambda p: _chapter_index(p.name),
    )
    if not md_files:
        logbus.log(project_id, "export-md", "chapters/ 无 .md 文件。", level="error")
        workspace.set_stage(project_id, "export-md", "failed", error="no chapter files")
        return {"ok": False, "error": "no chapter files"}

    parts = []
    for fp in md_files:
        text = fp.read_text(encoding="utf-8", errors="replace").rstrip()
        parts.append(text)
    book = "\n\n---\n\n".join(parts) + "\n"

    out_path = merged_dir / "book.md"
    out_path.write_text(book, encoding="utf-8")

    logbus.log(project_id, "export-md",
               f"合并 {len(md_files)} 个章节 -> {out_path.relative_to(root).as_posix()}，共 {len(book)} 字符。")
    workspace.set_stage(project_id, "export-md", "done", count=len(md_files), size=len(book))
    return {"ok": True, "count": len(md_files), "size": len(book), "path": "merged/book.md"}
