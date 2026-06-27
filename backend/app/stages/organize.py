"""Stage: organize.

Two sub-steps:
  5a. chapter-locate — for each level=1 TOC entry, find its starting page in body-ocr/*.txt
      using page_hint + LLM verification. Write chapter-ranges.json.
  5b. chapter-organize — for each located chapter, call LLM to organize the raw body text
      into structured markdown (# / ## / ### headings), write chapters/N-chapter.md.
"""
import hashlib
import json
from pathlib import Path

from .. import llm_client, workspace
from ..logbus import logbus

LOCATE_SYSTEM = (
    "你是一名古籍/图书排版校对助手。用户给你一个章节标题与若干关键词，"
    "并给你一段按页拼接的正文 OCR 文本。"
    "你的任务是判断该章节的起始位置是否在这段文本内，"
    "若是，给出最可能的起始页码与一段开头锚点文字。"
    "只输出 JSON，不要解释。"
)

LOCATE_USER_TEMPLATE = """章节标题：{title}
关键词：{keywords}

正文文本（按页拼接，每页前有「--- 第 N 页 ---」标记）：
<<<BODY_BEGIN
{body_window}
BODY_END>>>

请判断该章节的起始位置是否落在上述文本范围内，输出 JSON：
{{
  "found": true,
  "start_page": 12,             // 章节起始所在页码（int，1-based）
  "start_text_anchor": "..."    // 该章节在正文中开头的 20-60 字符，用于后续精确切片；找不到时给空字符串
}}

要求：
1. 若该章节起始不在给定文本范围内，found 给 false，start_page 与 anchor 留空或给 0；
2. start_page 必须是文本中实际出现的页码；
3. start_text_anchor 应为正文 OCR 文本中能稳定匹配到的一段开头（避免选页码行、页眉）；
4. 只输出 JSON 对象本身。
"""

ORGANIZE_LEAF_SYSTEM = (
    "你是一名精通中文图书排版与文字校对的编辑助手。"
    "用户给你一个小节（目录中某个二/三级标题，或无子条目的一级标题）的原始 OCR 文本（按页拼接，含页码标记）。"
    "你的任务是把 OCR 文本整理为干净的 Markdown 正文段落。"
    "只输出正文，不要输出本节标题（# / ## / ### 由系统按目录自动添加），"
    "不要解释、不要 markdown 代码块包裹、不要 <think> 标签。"
)

ORGANIZE_LEAF_USER_TEMPLATE = """本节标题：{section_title}（目录级别 {level}）

本节原始 OCR 文本（按页拼接，每页前有「--- 第 N 页 ---」标记）：
<<<BODY_BEGIN
{body_text}
BODY_END>>>

整理要求：
1. 不要输出本节标题行（#/##/### 由系统添加）；直接从正文段落开始；
2. 段落之间空一行；保留原文段落划分；
3. 删除页码标记行（如「--- 第 N 页 ---」）、页眉页脚、孤立的标题页大字；
4. 如文内有更细的小节标题（如「一、」「第一节」「（一）」），用 `####` 标记；
5. 修正明显 OCR 错别字，但不得增删实质内容；
6. 只输出整理后的 Markdown 正文，不要任何解释或思考过程。
"""


def _load_toc_entries(root: Path) -> list[dict]:
    p = root / "toc-structure.json"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("entries", [])


def _load_body_pages(root: Path) -> dict[int, str]:
    body_dir = root / "body-ocr"
    if not body_dir.exists():
        return {}
    out: dict[int, str] = {}
    for fp in body_dir.glob("*.txt"):
        if not fp.stem.isdigit():
            continue
        out[int(fp.stem)] = fp.read_text(encoding="utf-8", errors="replace")
    return out


def _window_text(body_pages: dict[int, str], center_page: int, radius: int = 3) -> tuple[str, list[int]]:
    """Return concatenation of body pages within ±radius around center_page."""
    if not body_pages:
        return "", []
    pages_sorted = sorted(body_pages.keys())
    lo = max(pages_sorted[0], center_page - radius)
    hi = min(pages_sorted[-1], center_page + radius)
    selected = [p for p in pages_sorted if lo <= p <= hi]
    parts = []
    for p in selected:
        parts.append(f"--- 第 {p} 页 ---\n{body_pages[p]}")
    return "\n\n".join(parts), selected


def _chapter_text(body_pages: dict[int, str], start_page: int, end_page: int) -> str:
    parts = []
    for p in sorted(body_pages.keys()):
        if start_page <= p <= end_page:
            parts.append(f"--- 第 {p} 页 ---\n{body_pages[p]}")
    return "\n\n".join(parts)


def _first_sub_hint(toc_entries: list[dict], level1_idx: int) -> int:
    """First non-zero page_hint among the L2/L3 descendants of the level1
    entry at position `level1_idx` (1-based among level=1 entries). 0 if none.
    """
    seen = 0
    collecting = False
    for e in toc_entries:
        lvl = e.get("level", 1)
        if lvl == 1:
            seen += 1
            if seen == level1_idx:
                collecting = True
                continue
            if collecting:
                break
        else:
            if collecting:
                h = e.get("page_hint") or 0
                if h:
                    return h
    return 0


def _sub_steps5a(project_id: str, level1_entries: list[dict],
                 body_pages: dict[int, str], offset: int,
                 toc_entries: list[dict],
                 should_stop) -> list[dict]:
    """Locate start page for each level=1 entry. Returns list of {entry, found, start_page, anchor}.

    `offset = pdf_start - printed_start` is the linear offset from printed
    page to PDF physical page: pdf_page = printed_page + offset. When a
    level=1 entry has page_hint=0 (a part-divider title with no printed
    page of its own), fall back to the first descendant's page_hint minus
    one, so the search window lands on the part-title page.
    """
    logbus.log(project_id, "organize",
               f"5a 章节定位：共 {len(level1_entries)} 个一级标题。"
               f"页码偏移：印刷 + {offset} = PDF 页。")
    results = []
    pages_sorted = sorted(body_pages.keys()) if body_pages else []
    body_median = pages_sorted[len(pages_sorted) // 2] if pages_sorted else 0

    for idx, entry in enumerate(level1_entries, 1):
        if should_stop():
            logbus.log(project_id, "organize", "收到停止请求，中止 5a。", level="warn")
            break
        title = entry.get("title", "").strip()
        keywords = entry.get("match_keywords") or []
        page_hint = entry.get("page_hint") or 0

        # page_hint=0 的一级标题（部类扉页）：取第一个子条目的 page_hint - 1
        if not page_hint:
            sub_hint = _first_sub_hint(toc_entries, idx)
            if sub_hint:
                page_hint = max(1, sub_hint - 1)
                map_note_src = f"子条目印刷页 {sub_hint} - 1 = 印刷页 {page_hint}"
            else:
                page_hint = 0
                map_note_src = "无子条目页码"
        else:
            map_note_src = f"印刷页 {page_hint}"

        # 印刷页码 + 偏移 = PDF 页号
        if page_hint:
            center_pdf = page_hint + offset
            map_note = f"{map_note_src} + {offset} = PDF 页 {center_pdf}"
        else:
            center_pdf = body_median
            map_note = f"{map_note_src}，用 body 中位数 PDF 页 {center_pdf}"

        logbus.log(project_id, "organize",
                   f"5a [{idx}/{len(level1_entries)}] 定位「{title}」（{map_note}）")

        if not body_pages:
            results.append({"entry": entry, "idx": idx, "found": False,
                            "start_page": 0, "anchor": "", "reason": "no body-ocr"})
            continue

        # 把 center_pdf 钳制到 body 范围内
        if not (pages_sorted[0] <= center_pdf <= pages_sorted[-1]):
            center_pdf = max(pages_sorted[0], min(pages_sorted[-1], center_pdf))
            logbus.log(project_id, "organize",
                       f"5a [{idx}] center_pdf 钳制到 body 范围：{center_pdf}", level="warn")

        window_text, _ = _window_text(body_pages, center_pdf, radius=3)

        def _log_5a(level, message, _idx=idx):
            logbus.log(project_id, "organize", f"[5a/{_idx}] {message}", level=level)

        data = llm_client.call_json(
            system_prompt=LOCATE_SYSTEM,
            user_content=LOCATE_USER_TEMPLATE.format(
                title=title,
                keywords=", ".join(str(k) for k in keywords),
                body_window=window_text,
            ),
            expected_keys={"found"},
            log_fn=_log_5a,
            log_prefix=f"[organize/5a/{idx}]",
            temperature=0.1,
            max_tokens=1024,
            should_stop=should_stop,
        )
        if data is None:
            results.append({"entry": entry, "idx": idx, "found": False,
                            "start_page": 0, "anchor": "", "reason": "llm failed"})
            continue
        found = bool(data.get("found"))
        sp = int(data.get("start_page") or 0)
        anchor = (data.get("start_text_anchor") or "").strip()
        results.append({"entry": entry, "idx": idx, "found": found,
                        "start_page": sp, "anchor": anchor})
        logbus.log(project_id, "organize",
                   f"5a [{idx}/{len(level1_entries)}] 「{title}」 -> found={found} start_page={sp}")

    return results


def _compute_heading_plan(toc_entries: list[dict], ranges: list[dict], offset: int) -> list[dict]:
    """Build a flat plan of EVERY toc entry in document order with its PDF page range.

    Each item: {chapter_idx, level, title, is_leaf, start_pdf, end_pdf}.
    - chapter_idx: 1-based index among level=1 entries (matches ranges[].idx).
    - is_leaf: True if this entry has no children (deepest in its branch) — these are
      the work units for 5b. An entry is a leaf if the next entry in document order
      has level <= its own (or there is no next entry).
    - start_pdf/end_pdf: PDF page range. For leaves, start absorbs any pending intro
      pages from non-leaf ancestors (cursor folding); end = next entry's start - 1
      or chapter end. Non-leaf entries have start_pdf/end_pdf = None (they contribute
      only a heading at assembly time).
    - Only entries within found chapters are included; non-found chapters are skipped
      entirely (assembly writes a placeholder for them).
    """
    chap_by_idx = {
        r["idx"]: (r["start_page"], r["end_page"])
        for r in ranges if r.get("found") and r.get("start_page", 0) > 0
    }
    plan: list[dict] = []
    chap_idx = 0
    chap_range = None
    cursor = 0  # last covered PDF page in current chapter
    for i, e in enumerate(toc_entries):
        lvl = e.get("level", 1)
        if lvl == 1:
            chap_idx += 1
            chap_range = chap_by_idx.get(chap_idx)
            if chap_range:
                cursor = chap_range[0] - 1
        if chap_range is None:
            continue  # chapter not found; skip this entry

        next_lvl: int | None = None
        if i + 1 < len(toc_entries):
            next_lvl = toc_entries[i + 1].get("level", 1)
        is_leaf = (next_lvl is None) or (next_lvl <= lvl)

        if is_leaf:
            leaf_start = cursor + 1
            if i + 1 < len(toc_entries) and toc_entries[i + 1].get("level", 1) != 1:
                ne_ph = toc_entries[i + 1].get("page_hint") or 0
                if ne_ph:
                    leaf_end = (ne_ph + offset) - 1
                else:
                    leaf_end = chap_range[1]
            else:
                leaf_end = chap_range[1]
            leaf_start = max(leaf_start, chap_range[0])
            leaf_end = min(leaf_end, chap_range[1])
            if leaf_end < leaf_start:
                leaf_end = leaf_start
            plan.append({
                "chapter_idx": chap_idx,
                "level": lvl,
                "title": (e.get("title") or "").strip(),
                "is_leaf": True,
                "start_pdf": leaf_start,
                "end_pdf": leaf_end,
            })
            cursor = leaf_end
        else:
            plan.append({
                "chapter_idx": chap_idx,
                "level": lvl,
                "title": (e.get("title") or "").strip(),
                "is_leaf": False,
                "start_pdf": None,
                "end_pdf": None,
            })
    return plan


def _frag_path(fragments_dir: Path, chapter_idx: int, item: dict) -> Path:
    """Fragment filename includes a hash of (title, start, end) so stale fragments
    from a previous plan (different offset / TOC) are invalidated automatically."""
    key = f"{item['title']}|{item['start_pdf']}|{item['end_pdf']}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    return fragments_dir / f"{chapter_idx:02d}-{h}.md"


def _read_fragment(fragments_dir: Path, chapter_idx: int, item: dict) -> str | None:
    p = _frag_path(fragments_dir, chapter_idx, item)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def _assemble_chapters(plan: list[dict], fragments_dir: Path,
                       chapters_dir: Path, ranges: list[dict]) -> None:
    """Write N-chapter.md for each chapter from leaf fragments + heading hierarchy."""
    by_chap: dict[int, list[dict]] = {}
    for item in plan:
        by_chap.setdefault(item["chapter_idx"], []).append(item)

    for r in ranges:
        idx = r["idx"]
        title = (r["entry"].get("title") or "").strip()
        out_path = chapters_dir / f"{idx}-chapter.md"
        items = by_chap.get(idx, [])
        if not items:
            r["organize_failed"] = True
            if r.get("found"):
                out_path.write_text(f"# {title}\n\n（无正文 OCR 文本）\n", encoding="utf-8")
            else:
                out_path.write_text(f"# {title}\n\n（章节定位失败，待人工补充）\n", encoding="utf-8")
            continue

        lines: list[str] = []
        any_failed = False
        for item in items:
            head = "#" * item["level"]
            lines.append(f"{head} {item['title']}")
            lines.append("")
            if item["is_leaf"]:
                frag = _read_fragment(fragments_dir, idx, item)
                if frag is None:
                    any_failed = True
                    lines.append("（本节 LLM 组织失败，待人工补充）")
                else:
                    lines.append(frag.rstrip())
                lines.append("")
        out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        if any_failed:
            r["organize_failed"] = True


def _compute_ranges(locate_results: list[dict], body_pages: dict[int, str]) -> list[dict]:
    """From located start pages, compute (start_page, end_page] ranges.

    end_page = next chapter's start_page - 1, or last body page for the final chapter.
    Chapters not found get start_page=0; their range will be empty.
    """
    pages_sorted = sorted(body_pages.keys()) if body_pages else []
    last_page = pages_sorted[-1] if pages_sorted else 0
    # 仅对 found 的章节排序
    located = [r for r in locate_results if r.get("found") and r.get("start_page", 0) > 0]
    located.sort(key=lambda r: r["start_page"])

    ranges = []
    for i, r in enumerate(located):
        start = r["start_page"]
        end = located[i + 1]["start_page"] - 1 if i + 1 < len(located) else last_page
        if end < start:
            end = start
        ranges.append({**r, "end_page": end})

    # 不在 located 中的章节（未找到），加占位
    for r in locate_results:
        if r not in located:
            ranges.append({**r, "end_page": 0})
    # 按 idx 排序保持原顺序
    ranges.sort(key=lambda r: r.get("idx", 0))
    return ranges


MAX_LEAF_PAGES = 8  # 超过此页数的叶子按页切成多个 bucket 分批送 LLM，避免输出被截断


def _organize_leaf_via_llm(project_id: str, li: int, title: str, level: int,
                           body_text: str, should_stop,
                           max_tokens: int = 8192) -> str | None:
    """Call LLM to organize one chunk of body text into markdown."""
    def _log_5b(level, message, _li=li):
        logbus.log(project_id, "organize", f"[5b/{_li}] {message}", level=level)
    return llm_client.call_text(
        system_prompt=ORGANIZE_LEAF_SYSTEM,
        user_content=ORGANIZE_LEAF_USER_TEMPLATE.format(
            section_title=title, level=level, body_text=body_text,
        ),
        log_fn=_log_5b,
        log_prefix=f"[organize/5b/{li}]",
        temperature=0.2,
        max_tokens=max_tokens,
        should_stop=should_stop,
    )


def _sub_steps5b(project_id: str, root: Path, plan: list[dict],
                 body_pages: dict[int, str], ranges: list[dict], should_stop,
                 max_leaf_pages: int = MAX_LEAF_PAGES,
                 max_tokens: int = 8192,
                 only_keys: set[tuple] | None = None):
    """Organize markdown per leaf entry (deepest heading in each branch).

    `only_keys`: if given, only process leaves whose (chapter_idx, title,
    start_pdf, end_pdf) is in this set (used by fix mode). None = all leaves.
    """
    """Organize markdown per leaf entry (deepest heading in each branch).

    Work unit = leaf (a heading with no sub-headings under it). Each leaf's
    page range is small (a few pages), so the LLM gets a manageable context.
    Leaves spanning more than MAX_LEAF_PAGES are split into page-buckets and
    processed sequentially, then concatenated — this prevents LLM output
    truncation on long sections (e.g. a 38-page L3 leaf).

    Fragments are cached on disk (chapters/.fragments/) keyed by a hash of
    (title, start, end), so reruns skip already-done leaves (resume). After
    all leaves are processed, chapters are assembled from fragments + the
    full heading hierarchy (# / ## / ###).
    """
    chapters_dir = root / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    fragments_dir = chapters_dir / ".fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)

    leaves = [p for p in plan if p["is_leaf"]]
    if only_keys is not None:
        leaves = [p for p in leaves
                  if (p["chapter_idx"], p["title"], p["start_pdf"], p["end_pdf"]) in only_keys]
    total = len(leaves)
    done = 0
    failed = 0
    skipped = 0

    mode_desc = f"仅修复 {total} 个目标叶子" if only_keys is not None else f"共 {total} 个叶子"
    logbus.log(project_id, "organize",
               f"5b 章节组织：{mode_desc}（最小标题单元），按页切分后逐节送 LLM。"
               f"超过 {max_leaf_pages} 页的叶子会分批处理。已存在的 fragment 会跳过（断点续传）。")
    workspace.set_stage(project_id, "organize", "running",
                        progress={"done": 0, "total": total, "failed": 0})

    for li, leaf in enumerate(leaves, 1):
        if should_stop():
            logbus.log(project_id, "organize", "收到停止请求，中止 5b。", level="warn")
            workspace.set_stage(project_id, "organize", "stopped",
                                progress={"done": done, "total": total, "failed": failed})
            return

        title = leaf["title"]
        sp, ep = leaf["start_pdf"], leaf["end_pdf"]
        frag_path = _frag_path(fragments_dir, leaf["chapter_idx"], leaf)

        # 断点续传：fragment 已存在且非空则跳过（fix 模式下应已删除，这里兜底）
        if frag_path.exists() and frag_path.read_text(encoding="utf-8").strip():
            skipped += 1
            done += 1
            logbus.log(project_id, "organize",
                       f"5b [{li}/{total}] 「{title}」已有 fragment，跳过。")
            workspace.set_stage(project_id, "organize", "running",
                                progress={"done": done, "total": total, "failed": failed})
            continue

        full_body = _chapter_text(body_pages, sp, ep)
        if not full_body.strip():
            frag_path.write_text("", encoding="utf-8")
            logbus.log(project_id, "organize",
                       f"5b [{li}/{total}] 「{title}」正文本为空（页 {sp}-{ep}），写空 fragment。",
                       level="warn")
            done += 1
            workspace.set_stage(project_id, "organize", "running",
                                progress={"done": done, "total": total, "failed": failed})
            continue

        # 大叶子按页切分
        page_count = ep - sp + 1
        if page_count > max_leaf_pages:
            buckets: list[tuple[int, int]] = []
            cur = sp
            while cur <= ep:
                buck_end = min(cur + max_leaf_pages - 1, ep)
                buckets.append((cur, buck_end))
                cur = buck_end + 1
            logbus.log(project_id, "organize",
                       f"5b [{li}/{total}] 「{title}」跨 {page_count} 页，分 {len(buckets)} 批处理"
                       f"（每批 ≤{max_leaf_pages} 页）...")
            parts: list[str] = []
            leaf_failed = False
            for bi, (bs, be) in enumerate(buckets, 1):
                if should_stop():
                    leaf_failed = True
                    break
                bucket_text = _chapter_text(body_pages, bs, be)
                if not bucket_text.strip():
                    continue
                logbus.log(project_id, "organize",
                           f"5b [{li}/{total}] 「{title}」批次 {bi}/{len(buckets)}（页 {bs}-{be}，{len(bucket_text)} 字符）...")
                md = _organize_leaf_via_llm(project_id, li, title, leaf["level"],
                                            bucket_text, should_stop, max_tokens=max_tokens)
                if md is None:
                    logbus.log(project_id, "organize",
                               f"5b [{li}/{total}] 「{title}」批次 {bi} LLM 失败。", level="error")
                    leaf_failed = True
                    break
                parts.append(md.rstrip())
            if leaf_failed or not parts:
                logbus.log(project_id, "organize",
                           f"5b [{li}/{total}] 「{title}」分批处理未完成，下次重跑会重试。", level="error")
                failed += 1
            else:
                combined = "\n\n".join(parts)
                frag_path.write_text(combined, encoding="utf-8")
                logbus.log(project_id, "organize",
                           f"5b [{li}/{total}] 「{title}」完成（{len(buckets)} 批合并，{len(combined)} 字符）")
        else:
            logbus.log(project_id, "organize",
                       f"5b [{li}/{total}] 「{title}」组织（页 {sp}-{ep}，{len(full_body)} 字符）...")
            md = _organize_leaf_via_llm(project_id, li, title, leaf["level"],
                                        full_body, should_stop, max_tokens=max_tokens)
            if md is None:
                logbus.log(project_id, "organize",
                           f"5b [{li}/{total}] 「{title}」LLM 失败，下次重跑会重试。", level="error")
                failed += 1
            else:
                frag_path.write_text(md, encoding="utf-8")
                logbus.log(project_id, "organize",
                           f"5b [{li}/{total}] 「{title}」完成，{len(md)} 字符")

        done += 1
        workspace.set_stage(project_id, "organize", "running",
                            progress={"done": done, "total": total, "failed": failed})

    # 组装各章
    _assemble_chapters(plan, fragments_dir, chapters_dir, ranges)
    logbus.log(project_id, "organize",
               f"5b 完成：{total} 叶子，成功 {total - failed - skipped}，跳过 {skipped}，失败 {failed}。"
               f"已组装 {len(ranges)} 个章节文件。")
    workspace.set_stage(project_id, "organize", "done",
                        progress={"done": done, "total": total, "failed": failed})


def fix_bad_leaves(project_id: str, bad_leaf_reports: list[dict], should_stop,
                   max_leaf_pages: int = 4, max_tokens: int = 12288) -> bool:
    """Delete fragments for the given bad leaves and re-organize only those
    leaves with a tighter bucket size and higher max_tokens.

    `bad_leaf_reports`: list of verify-report leaf dicts (with title,
    start_pdf, end_pdf, chapter_idx). Uses smaller buckets (default 4 pages)
    and higher max_tokens (default 12288) to give the LLM more room, avoiding
    the truncation that likely caused the original loss.
    """
    root = workspace.project_root(project_id)
    from . import verify as _verify
    pr = _verify.load_plan_and_ranges(root)
    if pr is None:
        logbus.log(project_id, "organize", "fix: heading-ranges.json 缺失，无法修复。", level="error")
        return False
    plan, ranges = pr
    body_pages = _load_body_pages(root)
    fragments_dir = root / "chapters" / ".fragments"

    # 删除目标 fragment
    only_keys: set[tuple] = set()
    deleted = 0
    for bl in bad_leaf_reports:
        key = (bl["chapter_idx"], bl["title"], bl["start_pdf"], bl["end_pdf"])
        only_keys.add(key)
        # 找到 plan 中对应的 leaf item 以算 fragment 路径
        for p in plan:
            if (p["chapter_idx"], p["title"], p["start_pdf"], p["end_pdf"]) == key:
                fp = _frag_path(fragments_dir, p["chapter_idx"], p)
                if fp.exists():
                    fp.unlink()
                    deleted += 1
                break

    logbus.log(project_id, "organize",
               f"fix: 删除 {deleted} 个异常 fragment，重新组织（每批 ≤{max_leaf_pages} 页，"
               f"max_tokens={max_tokens}）...", level="info")
    if not only_keys:
        logbus.log(project_id, "organize", "fix: 无可修复叶子。", level="warn")
        return True

    _sub_steps5b(project_id, root, plan, body_pages, ranges, should_stop,
                 max_leaf_pages=max_leaf_pages, max_tokens=max_tokens,
                 only_keys=only_keys)
    return True


def run(project_id: str, pdf_start: int, printed_start: int, should_stop) -> bool:
    root = workspace.project_root(project_id)

    toc_entries = _load_toc_entries(root)
    if not toc_entries:
        logbus.log(project_id, "organize", "toc-structure.json 不存在或为空。", level="error")
        workspace.set_stage(project_id, "organize", "failed", error="no toc-structure")
        return False

    body_pages = _load_body_pages(root)
    if not body_pages:
        logbus.log(project_id, "organize", "body-ocr 为空。", level="error")
        workspace.set_stage(project_id, "organize", "failed", error="empty body-ocr")
        return False

    if not pdf_start or not printed_start:
        logbus.log(project_id, "organize",
                   "缺少页码锚点（pdf_start / printed_start），无法定位章节。",
                   level="error")
        workspace.set_stage(project_id, "organize", "failed", error="missing anchor")
        return False

    level1_entries = [e for e in toc_entries if e.get("level") == 1]
    if not level1_entries:
        logbus.log(project_id, "organize", "toc-structure 中无 level=1 条目。", level="error")
        workspace.set_stage(project_id, "organize", "failed", error="no level1 entry")
        return False

    logbus.log(project_id, "organize",
               f"开始 organize：{len(level1_entries)} 个一级标题，body-ocr 共 {len(body_pages)} 页。"
               f"页码锚点：PDF {pdf_start} = 印刷 {printed_start}。",
               level="info")

    # 线性偏移：印刷页码 + offset = PDF 物理页号
    offset = pdf_start - printed_start

    # 5a 章节定位
    locate_results = _sub_steps5a(project_id, level1_entries, body_pages, offset, toc_entries, should_stop)
    if should_stop():
        workspace.set_stage(project_id, "organize", "stopped")
        return False

    # 计算 ranges（一级标题边界，用于划分章节文件）
    ranges = _compute_ranges(locate_results, body_pages)
    ranges_path = root / "chapters" / "chapter-ranges.json"
    ranges_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ranges_path, "w", encoding="utf-8") as f:
        json.dump({"ranges": ranges}, f, ensure_ascii=False, indent=2)

    # 计算所有层级标题的页码范围（一级用于划文件，二/三级用于划任务单元）
    plan = _compute_heading_plan(toc_entries, ranges, offset)
    plan_path = root / "chapters" / "heading-ranges.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"plan": plan}, f, ensure_ascii=False, indent=2)
    leaf_count = sum(1 for p in plan if p["is_leaf"])
    logbus.log(project_id, "organize",
               f"5a 完成：{len(ranges)} 个一级标题范围写入 chapter-ranges.json；"
               f"{len(plan)} 个标题（含 {leaf_count} 个叶子节点）写入 heading-ranges.json。")

    # 5b 章节组织（按叶子节点切分，逐节送 LLM，支持断点续传）
    _sub_steps5b(project_id, root, plan, body_pages, ranges, should_stop)

    # 重新写一次 ranges（5b 可能标记 organize_failed）
    with open(ranges_path, "w", encoding="utf-8") as f:
        json.dump({"ranges": ranges}, f, ensure_ascii=False, indent=2)

    if should_stop():
        workspace.set_stage(project_id, "organize", "stopped")
        return False

    logbus.log(project_id, "organize", "organize 完成。", level="info")
    return True
