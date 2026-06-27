import asyncio
import threading

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import workspace
from .config import API_PREFIX, STAGES
from .logbus import logbus
from .llm_client import get_pool, reload_pool
from .stages import body_ocr, export_md, organize, toc_ocr, toc_structure, verify

app = FastAPI(title="OCR Translate", docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter(prefix=API_PREFIX)


@app.on_event("startup")
async def _capture_loop():
    logbus.set_loop(asyncio.get_running_loop())
    for p in workspace.list_projects():
        meta = workspace.get_project(p["id"])
        if not meta:
            continue
        changed = False
        for s, info in meta.get("stages", {}).items():
            if info.get("status") == "running":
                info["status"] = "interrupted"
                info["note"] = "进程重启，执行中断"
                changed = True
        if changed:
            workspace.save_meta(p["id"], meta)


# ---------- task tracking ----------
_running: dict[str, set[str]] = {}
_running_lock = threading.Lock()
_stop_flags: dict[tuple[str, str], threading.Event] = {}
_stop_lock = threading.Lock()


def _is_running(project_id: str, stage: str) -> bool:
    with _running_lock:
        return stage in _running.get(project_id, set())


def _mark_running(project_id: str, stage: str):
    with _running_lock:
        _running.setdefault(project_id, set()).add(stage)
    with _stop_lock:
        _stop_flags.pop((project_id, stage), None)


def _mark_done(project_id: str, stage: str):
    with _running_lock:
        _running.get(project_id, set()).discard(stage)


def _should_stop(project_id: str, stage: str) -> bool:
    with _stop_lock:
        ev = _stop_flags.get((project_id, stage))
    return ev is not None and ev.is_set()


def _request_stop(project_id: str, stage: str):
    with _stop_lock:
        ev = _stop_flags.setdefault((project_id, stage), threading.Event())
    ev.set()


def _make_should_stop(project_id: str, stage: str):
    def _check() -> bool:
        return _should_stop(project_id, stage)
    return _check


def _run_in_thread(project_id: str, stage: str, fn, *args, **kwargs):
    def _wrapper():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logbus.log(project_id, stage, f"阶段异常: {e}", level="error")
            workspace.set_stage(project_id, stage, "failed", error=str(e))
        finally:
            _mark_done(project_id, stage)

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    return t


# ---------- models ----------
class OcrRangeRequest(BaseModel):
    start_page: int
    end_page: int
    workers: int | None = None  # 预留，当前实现固定串行
    backend: str | None = None  # "glm" | "paddle" | None(回退默认)


# ---------- project routes ----------
@router.get("/health")
def health():
    return {"ok": True}


@router.get("/apikeys")
def api_keys():
    try:
        count = get_pool().count()
    except Exception as e:
        return {"loaded": False, "count": 0, "error": str(e)}
    return {"loaded": count > 0, "count": count}


@router.post("/apikeys/reload")
def api_keys_reload():
    try:
        count = reload_pool()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"loaded": True, "count": count}


@router.get("/projects")
def list_projects_api():
    return workspace.list_projects()


@router.post("/projects")
async def create_project_api(name: str = Form(""), file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持上传 .pdf 文件")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    meta = workspace.create_project(name, raw, filename=file.filename)
    logbus.log(meta["id"], "system",
               f"创建项目 {meta['name']}，原始 PDF {meta.get('origin_filename','origin.pdf')}，"
               f"页数 {meta.get('pdf_pages',0)}，大小 {len(raw)} 字节。")
    return meta


@router.get("/projects/{project_id}")
def get_project_api(project_id: str):
    meta = workspace.get_project(project_id)
    if meta is None:
        raise HTTPException(404, "project not found")
    files = workspace.list_project_files(project_id)
    return {**meta, "files": files}


@router.delete("/projects/{project_id}")
def delete_project_api(project_id: str):
    ok = workspace.delete_project(project_id)
    if not ok:
        raise HTTPException(404, "project not found")
    return {"ok": True}


@router.get("/projects/{project_id}/files")
def get_file_api(project_id: str, path: str):
    fp = workspace.safe_relpath(project_id, path)
    if fp is None or not fp.exists() or not fp.is_file():
        raise HTTPException(404, "file not found")
    try:
        text = fp.read_text(encoding="utf-8")
        return PlainTextResponse(text)
    except UnicodeDecodeError:
        return PlainTextResponse(fp.read_bytes().decode("utf-8", errors="replace"))


@router.get("/projects/{project_id}/download")
def download_project(project_id: str):
    """Stream the entire project workspace as a zip. Excludes origin.pdf (raw
    upload, large & regenerable not) and .fragments (intermediate cache)."""
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    root = workspace.project_root(project_id)

    import io
    import zipfile

    EXCLUDE_DIRS = {"origin", ".fragments"}
    EXCLUDE_SUFFIXES = {".pdf"}

    def _iter_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(root.rglob("*")):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(root)
                # 跳过 origin/ 目录、.fragments/ 缓存、PDF 原件
                if any(part in EXCLUDE_DIRS for part in rel.parts):
                    continue
                if fp.suffix.lower() in EXCLUDE_SUFFIXES:
                    continue
                arcname = f"{project_id}/{rel.as_posix()}"
                zf.write(fp, arcname)
        buf.seek(0)
        while True:
            chunk = buf.read(65536)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        _iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}.zip"'},
    )


@router.get("/projects/{project_id}/logs")
def get_logs_api(project_id: str, stage: str | None = None, tail: int = 200):
    items = logbus.tail_from_disk(project_id, stage, tail)
    return items


@router.websocket("/projects/{project_id}/logs")
async def ws_logs(websocket: WebSocket, project_id: str):
    await websocket.accept()
    try:
        await logbus.subscribe(project_id, websocket)
    except WebSocketDisconnect:
        pass


# ---------- stage routes ----------
@router.post("/projects/{project_id}/stage/toc-ocr")
def stage_toc_ocr(project_id: str, req: OcrRangeRequest):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "toc-ocr"):
        raise HTTPException(409, "toc-ocr already running")
    workspace.set_stage(project_id, "toc-ocr", "running")
    _mark_running(project_id, "toc-ocr")
    _run_in_thread(project_id, "toc-ocr", toc_ocr.run,
                   project_id, req.start_page, req.end_page,
                   _make_should_stop(project_id, "toc-ocr"),
                   req.backend)
    return {"ok": True, "stage": "toc-ocr", "status": "running"}


@router.post("/projects/{project_id}/stage/toc-structure")
def stage_toc_structure(project_id: str):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "toc-structure"):
        raise HTTPException(409, "toc-structure already running")
    workspace.set_stage(project_id, "toc-structure", "running")
    _mark_running(project_id, "toc-structure")
    _run_in_thread(project_id, "toc-structure", toc_structure.run,
                   project_id, _make_should_stop(project_id, "toc-structure"))
    return {"ok": True, "stage": "toc-structure", "status": "running"}


@router.post("/projects/{project_id}/stage/body-ocr")
def stage_body_ocr(project_id: str, req: OcrRangeRequest):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "body-ocr"):
        raise HTTPException(409, "body-ocr already running")
    workspace.set_stage(project_id, "body-ocr", "running")
    _mark_running(project_id, "body-ocr")
    _run_in_thread(project_id, "body-ocr", body_ocr.run,
                   project_id, req.start_page, req.end_page,
                   _make_should_stop(project_id, "body-ocr"),
                   req.backend)
    return {"ok": True, "stage": "body-ocr", "status": "running"}


class OrganizeRequest(BaseModel):
    pdf_start: int
    printed_start: int


@router.post("/projects/{project_id}/stage/organize")
def stage_organize(project_id: str, req: OrganizeRequest):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "organize"):
        raise HTTPException(409, "organize already running")
    workspace.set_stage(project_id, "organize", "running")
    _mark_running(project_id, "organize")
    _run_in_thread(project_id, "organize", organize.run,
                   project_id, req.pdf_start, req.printed_start,
                   _make_should_stop(project_id, "organize"))
    return {"ok": True, "stage": "organize", "status": "running"}


class VerifyRequest(BaseModel):
    ratio_min: float = 0.90
    ratio_max: float = 1.10
    mode: str = "check"        # check | fix | loop
    max_rounds: int = 3        # loop 模式最多轮次


@router.post("/projects/{project_id}/stage/verify")
def stage_verify(project_id: str, req: VerifyRequest):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "verify"):
        raise HTTPException(409, "verify already running")
    if req.mode not in ("check", "fix", "loop"):
        raise HTTPException(400, "mode must be check | fix | loop")
    workspace.set_stage(project_id, "verify", "running")
    _mark_running(project_id, "verify")
    _run_in_thread(project_id, "verify", _verify_driver,
                   project_id, req.ratio_min, req.ratio_max, req.mode, req.max_rounds,
                   _make_should_stop(project_id, "verify"))
    return {"ok": True, "stage": "verify", "status": "running"}


def _verify_driver(project_id: str, ratio_min: float, ratio_max: float,
                   mode: str, max_rounds: int, should_stop):
    """Drives verify with check/fix/loop modes. Runs in background thread."""
    try:
        if mode == "check":
            verify.run(project_id, ratio_min, ratio_max, should_stop)
            return

        # fix: one round of (check → delete bad → re-organize → re-check)
        # loop: repeat fix until 0 bad or max_rounds exhausted
        rounds = max_rounds if mode == "loop" else 1
        last_bad = -1
        for r in range(1, rounds + 1):
            if should_stop():
                break
            logbus.log(project_id, "verify",
                       f"{'循环' if mode == 'loop' else '修复'} 第 {r}/{rounds} 轮：校验...",
                       level="info")
            report = verify.compute_report(project_id, ratio_min, ratio_max, should_stop)
            if report is None:
                workspace.set_stage(project_id, "verify", "failed", error="no heading-ranges")
                return
            ok, bad = report["ok_count"], report["bad_count"]
            logbus.log(project_id, "verify",
                       f"第 {r} 轮校验完成：{ok} 正常，{bad} 异常。")
            workspace.set_stage(project_id, "verify", "running",
                                total=report["leaf_count"], ok=ok, bad=bad,
                                round=r, max_rounds=rounds)
            if bad == 0:
                logbus.log(project_id, "verify", "无异常，结束。", level="info")
                break
            if mode == "loop" and bad == last_bad:
                logbus.log(project_id, "verify",
                           f"异常数与上一轮相同（{bad}），可能为不可修复的合理损耗，停止循环。",
                           level="warn")
                break
            last_bad = bad

            targets = verify.bad_leaves_for_fix(report)
            if not targets:
                logbus.log(project_id, "verify",
                           "异常均为小叶子合理损耗（OCR 噪声清理），无需修复，结束。",
                           level="info")
                break
            logbus.log(project_id, "verify",
                       f"第 {r} 轮修复：{len(targets)} 个叶子重新组织...", level="info")
            if should_stop():
                break
            organize.fix_bad_leaves(project_id, targets, should_stop)

        # 最终校验
        if not should_stop():
            verify.run(project_id, ratio_min, ratio_max, should_stop)
    except Exception as e:
        logbus.log(project_id, "verify", f"verify 异常: {e}", level="error")
        workspace.set_stage(project_id, "verify", "failed", error=str(e))


@router.post("/projects/{project_id}/stage/export-md")
def stage_export_md(project_id: str):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if _is_running(project_id, "export-md"):
        raise HTTPException(409, "export-md already running")
    workspace.set_stage(project_id, "export-md", "running")
    _mark_running(project_id, "export-md")
    try:
        result = export_md.run(project_id)
        if not result.get("ok"):
            workspace.set_stage(project_id, "export-md", "failed", error=result.get("error", "unknown"))
        # done already set inside export_md.run on success
        _mark_done(project_id, "export-md")
        return result
    except Exception as e:
        workspace.set_stage(project_id, "export-md", "failed", error=str(e))
        _mark_done(project_id, "export-md")
        raise HTTPException(500, str(e))


@router.post("/projects/{project_id}/stage/{stage}/stop")
def stage_stop(project_id: str, stage: str):
    if not workspace.get_project(project_id):
        raise HTTPException(404, "project not found")
    if stage not in STAGES:
        raise HTTPException(400, "unknown stage")
    if stage == "export-md":
        raise HTTPException(400, "export-md 是同步瞬时任务，无法停止")
    if not _is_running(project_id, stage):
        raise HTTPException(409, f"{stage} 未在运行")
    _request_stop(project_id, stage)
    logbus.log(project_id, stage, "已收到停止请求，将在当前批次跑完后停止。", level="warn")
    return {"ok": True, "stage": stage, "status": "stopping"}


@router.get("/projects/{project_id}/status")
def project_status(project_id: str):
    meta = workspace.get_project(project_id)
    if meta is None:
        raise HTTPException(404, "project not found")
    running: list[str] = []
    with _running_lock:
        for s in _running.get(project_id, set()):
            running.append(s)
    return {
        "stages": meta.get("stages", {}),
        "running": running,
    }


app.include_router(router)
