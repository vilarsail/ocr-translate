import json
import shutil
import time
import uuid
from pathlib import Path

from .config import STAGES, WORKSPACES_DIR
from . import pdf_utils


def _now() -> float:
    return time.time()


def list_projects() -> list[dict]:
    projects = []
    if not WORKSPACES_DIR.exists():
        return projects
    for d in sorted(WORKSPACES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            projects.append({
                "id": d.name,
                "name": meta.get("name", d.name),
                "created_at": meta.get("created_at", 0),
                "pdf_pages": meta.get("pdf_pages", 0),
                "stages": meta.get("stages", {}),
            })
        except Exception:
            continue
    return projects


def create_project(name: str, pdf_bytes: bytes, filename: str = "origin.pdf") -> dict:
    project_id = uuid.uuid4().hex[:12]
    root = WORKSPACES_DIR / project_id
    (root / "origin").mkdir(parents=True, exist_ok=True)
    (root / "toc-ocr").mkdir(parents=True, exist_ok=True)
    (root / "body-ocr").mkdir(parents=True, exist_ok=True)
    (root / "chapters").mkdir(parents=True, exist_ok=True)
    (root / "merged").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    pdf_path = root / "origin" / "origin.pdf"
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    try:
        pages = pdf_utils.get_page_count(pdf_path)
    except Exception:
        pages = 0

    meta = {
        "id": project_id,
        "name": name or f"项目-{project_id[:6]}",
        "created_at": _now(),
        "pdf_pages": pages,
        "origin_filename": filename,
        "stages": {s: {"status": "idle"} for s in STAGES},
    }
    save_meta(project_id, meta)
    return meta


def get_project(project_id: str) -> dict | None:
    root = WORKSPACES_DIR / project_id
    if not root.exists():
        return None
    meta_path = root / "meta.json"
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_meta(project_id: str, meta: dict):
    root = WORKSPACES_DIR / project_id
    with open(root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def set_stage(project_id: str, stage: str, status: str, **extra):
    meta = get_project(project_id)
    if meta is None:
        return
    entry = dict(meta.get("stages", {}).get(stage, {}))
    entry["status"] = status
    entry["updated_at"] = _now()
    entry.update(extra)
    meta.setdefault("stages", {})[stage] = entry
    save_meta(project_id, meta)


def delete_project(project_id: str) -> bool:
    root = WORKSPACES_DIR / project_id
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True


def project_root(project_id: str) -> Path:
    return WORKSPACES_DIR / project_id


def safe_relpath(project_id: str, requested: str) -> Path | None:
    root = project_root(project_id).resolve()
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def list_project_files(project_id: str) -> list[dict]:
    root = project_root(project_id)
    if not root.exists():
        return []
    out = []
    for fp in sorted(root.rglob("*")):
        if fp.is_dir():
            continue
        if fp.name == "meta.json":
            continue
        rel = fp.relative_to(root).as_posix()
        out.append({
            "path": rel,
            "size": fp.stat().st_size,
        })
    return out
