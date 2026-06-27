"""Shared runner for toc-ocr and body-ocr stages.

Both stages: render each page in [start_page, end_page] to PNG via PyMuPDF,
send to ollama glm-ocr, save per-page text to <out_dir>/<page>.txt.
Skip pages whose output file already exists and is non-empty (resume support).
"""
from .. import ocr_client, pdf_utils, workspace
from ..config import OCR_RENDER_DPI
from ..logbus import logbus


def run(project_id: str, stage: str, out_dir_name: str, hint: str,
        start_page: int, end_page: int, should_stop,
        backend: str | None = None) -> bool:
    """Returns True on success, False on failure.

    `backend` overrides the default OCR backend for this stage run
    ("glm" | "paddle"). None falls back to config.OCR_BACKEND.
    """
    meta = workspace.get_project(project_id)
    if meta is None:
        return False

    pdf_pages = meta.get("pdf_pages", 0)
    if pdf_pages <= 0:
        logbus.log(project_id, stage, "项目无 PDF 或 PDF 页数为 0", level="error")
        workspace.set_stage(project_id, stage, "failed", error="no pdf pages")
        return False

    if start_page < 1 or end_page > pdf_pages or start_page > end_page:
        logbus.log(project_id, stage,
                   f"页码范围非法：start={start_page} end={end_page} 总页数={pdf_pages}",
                   level="error")
        workspace.set_stage(project_id, stage, "failed",
                            error=f"bad page range [{start_page},{end_page}] / {pdf_pages}")
        return False

    pdf_path = workspace.project_root(project_id) / "origin" / "origin.pdf"
    out_dir = workspace.project_root(project_id) / out_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    total = end_page - start_page + 1
    done = 0
    failed_pages: list[int] = []

    logbus.log(project_id, stage,
               f"开始 OCR：页 {start_page}-{end_page}（共 {total} 页），hint={hint}，backend={backend or 'default'}")
    workspace.set_stage(project_id, stage, "running",
                        progress={"done": 0, "total": total, "current_page": start_page})

    for page in range(start_page, end_page + 1):
        if should_stop():
            logbus.log(project_id, stage, "收到停止请求，停止后续页 OCR。", level="warn")
            workspace.set_stage(project_id, stage, "stopped",
                                progress={"done": done, "total": total, "current_page": page})
            return False

        out_file = out_dir / f"{page}.txt"
        if out_file.exists() and out_file.stat().st_size > 0:
            logbus.log(project_id, stage, f"页 {page}: 已存在，跳过。")
            done += 1
            workspace.set_stage(project_id, stage, "running",
                                progress={"done": done, "total": total, "current_page": page})
            continue

        logbus.log(project_id, stage, f"页 {page}: 渲染 + OCR 开始...")
        try:
            img = pdf_utils.render_page(pdf_path, page - 1, dpi=OCR_RENDER_DPI)
            text = ocr_client.ocr_page(
                img, hint=hint,
                log_fn=lambda lvl, msg: logbus.log(project_id, stage, msg, level=lvl),
                log_prefix=f"[页 {page}]",
                backend=backend,
            )
            out_file.write_text(text, encoding="utf-8")
            done += 1
            logbus.log(project_id, stage,
                       f"页 {page}: 完成，{len(text)} 字符。")
            workspace.set_stage(project_id, stage, "running",
                                progress={"done": done, "total": total, "current_page": page})
        except Exception as e:
            logbus.log(project_id, stage, f"页 {page}: 失败 - {e}", level="error")
            failed_pages.append(page)
            # 失败时：若已有非空文件则保留（避免覆盖之前成功的结果）；
            # 否则写空文件占位，用户可手动删除以重试。
            if not (out_file.exists() and out_file.stat().st_size > 0):
                try:
                    out_file.write_text("", encoding="utf-8")
                except Exception:
                    pass
            workspace.set_stage(project_id, stage, "running",
                                progress={"done": done, "total": total, "current_page": page})

    logbus.log(project_id, stage,
               f"OCR 结束。完成 {done}/{total}，失败 {len(failed_pages)} 页：{failed_pages}")
    if failed_pages:
        workspace.set_stage(project_id, stage, "done",
                            progress={"done": done, "total": total},
                            failed_pages=failed_pages)
    else:
        workspace.set_stage(project_id, stage, "done",
                            progress={"done": done, "total": total})
    return True
