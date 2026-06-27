import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path


class ProjectLogBus:
    """Per-project in-memory log buffer + WebSocket fan-out + disk append.

    seq is scoped PER PROJECT and seeded from disk on first use, so a server
    restart never resets the counter (which would collide with old seqs and
    make the client's dedup drop new live logs).
    """

    def __init__(self, max_buffer=5000):
        self._buffers: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_buffer))
        self._subscribers: dict[str, set] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq_lock = threading.Lock()
        self._seq_per_project: dict[str, int] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def _log_dir(self, project_id: str) -> Path:
        from .config import WORKSPACES_DIR
        return WORKSPACES_DIR / project_id / "logs"

    def _seed_seq_from_disk(self, project_id: str) -> int:
        log_dir = self._log_dir(project_id)
        max_seq = 0
        if log_dir.exists():
            for fp in log_dir.glob("*.log"):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                s = json.loads(line).get("seq", 0)
                                if isinstance(s, int) and s > max_seq:
                                    max_seq = s
                            except Exception:
                                pass
                except Exception:
                    pass
        return max_seq

    def _next_seq(self, project_id: str) -> int:
        with self._seq_lock:
            if project_id not in self._seq_per_project:
                self._seq_per_project[project_id] = self._seed_seq_from_disk(project_id)
            self._seq_per_project[project_id] += 1
            return self._seq_per_project[project_id]

    def _append_disk(self, project_id: str, stage: str, entry: dict):
        log_dir = self._log_dir(project_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{stage}.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def publish(self, project_id: str, stage: str, level: str, message: str):
        ts = time.time()
        entry = {"seq": self._next_seq(project_id), "ts": ts, "stage": stage, "level": level, "message": message}
        self._buffers[project_id].append(entry)
        try:
            self._append_disk(project_id, stage, entry)
        except Exception:
            pass
        msg = json.dumps(entry, ensure_ascii=False)
        for ws in list(self._subscribers.get(project_id, set())):
            try:
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.send_text(msg), self._loop)
                else:
                    asyncio.create_task(ws.send_text(msg))
            except Exception:
                pass

    def log(self, project_id: str, stage: str, message: str, level: str = "info"):
        self.publish(project_id, stage, level, message)

    async def subscribe(self, project_id: str, ws):
        last_seq = 0
        try:
            first = await asyncio.wait_for(ws.receive_text(), timeout=10)
            last_seq = int(str(first).strip() or "0")
        except Exception:
            last_seq = 0

        async with self._lock:
            self._subscribers[project_id].add(ws)

        try:
            for entry in list(self._buffers.get(project_id, [])):
                if entry.get("seq", 0) > last_seq:
                    try:
                        await ws.send_text(json.dumps(entry, ensure_ascii=False))
                    except Exception:
                        pass
            while True:
                await ws.receive_text()
        except Exception:
            pass
        finally:
            async with self._lock:
                self._subscribers[project_id].discard(ws)

    def tail_from_disk(self, project_id: str, stage: str | None, n: int = 200) -> list[dict]:
        log_dir = self._log_dir(project_id)
        if not log_dir.exists():
            return []
        files = sorted(log_dir.glob("*.log"))
        entries: list[dict] = []
        for fp in files:
            if stage and fp.stem != stage:
                continue
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        return entries[-n:]


logbus = ProjectLogBus()
