from ._ocr_runner import run as _run


def run(project_id: str, start_page: int, end_page: int, should_stop,
        backend: str | None = None) -> bool:
    return _run(project_id, "toc-ocr", "toc-ocr", "toc",
                start_page, end_page, should_stop, backend=backend)
