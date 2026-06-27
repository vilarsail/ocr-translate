"""Stage: verify.

Compare organized chapters (chapters/N-chapter.md) against the original
body-ocr text on a per-leaf basis, catching content loss, duplication,
or LLM hallucination.

Two metrics per leaf:
  1. char ratio = md_chars / ocr_chars (中文字符+字母数字，去标题/页码标记)
     - < ratio_min  → too_short (丢内容)
     - > ratio_max  → too_long  (重复或幻觉)
  2. 3-gram recall & precision (顺序校验)
     - recall    = OCR 3-grams 出现在 md 中的比例   (低 = 丢内容)
     - precision = md 3-grams 出现在 OCR 中的比例    (低 = 幻觉/重复)
     - recall < 0.85    → low_recall
     - precision < 0.85 → low_precision

Per-chapter aggregates are rolled up from leaves. Output:
  chapters/verify-report.json
"""
import hashlib
import json
import re
from pathlib import Path

from .. import workspace
from ..logbus import logbus

# n-gram 阈值固定（字数比才是用户可调的主信号）
NGRAM_RECALL_MIN = 0.85
NGRAM_PRECISION_MIN = 0.85
NGRAM_N = 3

# 默认字数比阈值（可被 run() 参数覆盖）
DEFAULT_RATIO_MIN = 0.90
DEFAULT_RATIO_MAX = 1.10

_CHAR_RE = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]")
_PAGE_MARK_RE = re.compile(r"^---\s*第\s*\d+\s*页\s*---")
_HEADING_RE = re.compile(r"^#{1,6}\s")


def _count_chars(text: str) -> int:
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s or _HEADING_RE.match(s) or _PAGE_MARK_RE.match(s):
            continue
        kept.append(s)
    return len(_CHAR_RE.findall("".join(kept)))


def _normalized_chars(text: str) -> str:
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s or _HEADING_RE.match(s) or _PAGE_MARK_RE.match(s):
            continue
        kept.append(s)
    return "".join(_CHAR_RE.findall("".join(kept)))


def _ngrams(s: str, n: int = NGRAM_N) -> set[str]:
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _load_body_pages(root: Path) -> dict[int, str]:
    body_dir = root / "body-ocr"
    out: dict[int, str] = {}
    if not body_dir.exists():
        return out
    for fp in body_dir.glob("*.txt"):
        if fp.stem.isdigit():
            out[int(fp.stem)] = fp.read_text(encoding="utf-8", errors="replace")
    return out


def _frag_path(fragments_dir: Path, chapter_idx: int, item: dict) -> Path:
    key = f"{item['title']}|{item['start_pdf']}|{item['end_pdf']}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    return fragments_dir / f"{chapter_idx:02d}-{h}.md"


def _read_fragment(fragments_dir: Path, chapter_idx: int, item: dict) -> str:
    p = _frag_path(fragments_dir, chapter_idx, item)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _leaf_status(ratio: float, recall: float, precision: float,
                 ratio_min: float, ratio_max: float) -> str:
    issues = []
    if ratio < ratio_min:
        issues.append("too_short")
    elif ratio > ratio_max:
        issues.append("too_long")
    if recall < NGRAM_RECALL_MIN:
        issues.append("low_recall")
    if precision < NGRAM_PRECISION_MIN:
        issues.append("low_precision")
    return "+".join(issues) if issues else "ok"


def load_plan_and_ranges(root: Path) -> tuple[list[dict], list[dict]] | None:
    plan_path = root / "chapters" / "heading-ranges.json"
    ranges_path = root / "chapters" / "chapter-ranges.json"
    if not plan_path.exists() or not ranges_path.exists():
        return None
    plan = json.load(open(plan_path, encoding="utf-8"))["plan"]
    ranges = json.load(open(ranges_path, encoding="utf-8"))["ranges"]
    return plan, ranges


def compute_report(project_id: str, ratio_min: float, ratio_max: float,
                   should_stop=None) -> dict | None:
    """Compute verify report from current fragments + OCR. Returns report dict
    (also written to chapters/verify-report.json), or None if prerequisites
    missing."""
    root = workspace.project_root(project_id)
    pr = load_plan_and_ranges(root)
    if pr is None:
        logbus.log(project_id, "verify",
                   "heading-ranges.json / chapter-ranges.json 不存在，请先执行 organize。",
                   level="error")
        return None
    plan, ranges = pr
    body_pages = _load_body_pages(root)
    fragments_dir = root / "chapters" / ".fragments"

    leaves = [p for p in plan if p["is_leaf"]]
    total = len(leaves)
    leaf_reports: list[dict] = []
    failed = 0

    for i, leaf in enumerate(leaves, 1):
        if should_stop and should_stop():
            return None
        title = leaf["title"]
        sp, ep = leaf["start_pdf"], leaf["end_pdf"]
        frag = _read_fragment(fragments_dir, leaf["chapter_idx"], leaf)
        ocr_text = "\n".join(body_pages[p] for p in range(sp, ep + 1) if p in body_pages)
        ocr_chars = _count_chars(ocr_text)
        md_chars = _count_chars(frag)
        ratio = (md_chars / ocr_chars) if ocr_chars else 0.0
        ocr_ng = _ngrams(_normalized_chars(ocr_text))
        md_ng = _ngrams(_normalized_chars(frag))
        if ocr_ng and md_ng:
            recall = len(ocr_ng & md_ng) / len(ocr_ng)
            precision = len(ocr_ng & md_ng) / len(md_ng)
        else:
            recall = 0.0
            precision = 0.0
        status = _leaf_status(ratio, recall, precision, ratio_min, ratio_max)
        if status != "ok":
            failed += 1
        leaf_reports.append({
            "chapter_idx": leaf["chapter_idx"],
            "level": leaf["level"],
            "title": title,
            "start_pdf": sp,
            "end_pdf": ep,
            "pages": ep - sp + 1,
            "ocr_chars": ocr_chars,
            "md_chars": md_chars,
            "ratio": round(ratio, 4),
            "ngram_recall": round(recall, 4),
            "ngram_precision": round(precision, 4),
            "status": status,
        })

    by_chap: dict[int, list[dict]] = {}
    for r in leaf_reports:
        by_chap.setdefault(r["chapter_idx"], []).append(r)
    chapter_reports = []
    for r in ranges:
        idx = r["idx"]
        title = r["entry"]["title"]
        ls = by_chap.get(idx, [])
        ocr_sum = sum(l["ocr_chars"] for l in ls)
        md_sum = sum(l["md_chars"] for l in ls)
        ratio = (md_sum / ocr_sum) if ocr_sum else 0.0
        bad = [l for l in ls if l["status"] != "ok"]
        status = "ok" if not bad else "+".join(sorted({s for l in bad for s in l["status"].split("+")}))
        chapter_reports.append({
            "chapter_idx": idx,
            "title": title,
            "leaf_count": len(ls),
            "bad_leaf_count": len(bad),
            "ocr_chars": ocr_sum,
            "md_chars": md_sum,
            "ratio": round(ratio, 4),
            "status": status,
            "bad_leaves": bad,
        })

    report = {
        "thresholds": {
            "ratio_min": ratio_min, "ratio_max": ratio_max,
            "ngram_recall_min": NGRAM_RECALL_MIN,
            "ngram_precision_min": NGRAM_PRECISION_MIN,
            "ngram_n": NGRAM_N,
        },
        "leaf_count": total,
        "ok_count": total - failed,
        "bad_count": failed,
        "chapters": chapter_reports,
        "leaves": leaf_reports,
    }
    out_path = root / "chapters" / "verify-report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def bad_leaves_for_fix(report: dict) -> list[dict]:
    """Return leaf report items that should be deleted & re-organized.
    Skip leaves that are merely too_short on tiny pages (OCR noise cleanup
    is legitimate and won't improve on redo) — only flag leaves where a
    redo could actually help: low_recall, low_precision, too_long, or
    too_short on >2 pages.
    """
    out = []
    for l in report.get("leaves", []):
        if l["status"] == "ok":
            continue
        st = l["status"]
        is_short_small = ("too_short" in st and l["pages"] <= 2
                          and "low_recall" not in st and "low_precision" not in st)
        if is_short_small:
            continue
        out.append(l)
    return out


def run(project_id: str, ratio_min: float = DEFAULT_RATIO_MIN,
        ratio_max: float = DEFAULT_RATIO_MAX, should_stop=None) -> bool:
    root = workspace.project_root(project_id)
    pr = load_plan_and_ranges(root)
    if pr is None:
        workspace.set_stage(project_id, "verify", "failed", error="no heading-ranges")
        return False
    plan, _ = pr
    leaves_total = sum(1 for p in plan if p["is_leaf"])

    logbus.log(project_id, "verify",
               f"开始校验：{leaves_total} 个叶子。"
               f"阈值：字数比 {ratio_min}-{ratio_max}，3-gram recall/precision ≥ {NGRAM_RECALL_MIN}。")
    workspace.set_stage(project_id, "verify", "running", total=leaves_total)

    report = compute_report(project_id, ratio_min, ratio_max, should_stop)
    if report is None:
        workspace.set_stage(project_id, "verify", "failed", error="compute failed")
        return False

    ok = report["ok_count"]
    bad = report["bad_count"]
    bad_chapters = [c["title"] for c in report["chapters"] if c["status"] != "ok"]
    logbus.log(project_id, "verify",
               f"校验完成：{ok}/{report['leaf_count']} 叶子正常，{bad} 异常。"
               f"异常章节：{bad_chapters or '无'}。写入 verify-report.json。")
    workspace.set_stage(project_id, "verify", "done",
                        total=report["leaf_count"], ok=ok, bad=bad,
                        bad_chapters=bad_chapters)
    return True
