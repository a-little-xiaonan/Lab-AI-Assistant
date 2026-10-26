"""有界队列与进程独立 JSONL 文件；导出故障不阻断问答。"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import ROOT, settings

logger = logging.getLogger(__name__)


class JsonlExporter:
    def __init__(self, directory: Path, *, queue_size: int, max_bytes: int,
                 backup_count: int, retention_days: int,
                 max_total_bytes: int | None = None,
                 cleanup_interval_seconds: int = 3600) -> None:
        self.directory = directory
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.cleanup_interval_seconds = max(60, cleanup_interval_seconds)
        self._prefix = f"chat-trace-{os.getpid()}-{uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        self._last_warning = float("-inf")
        self._last_cleanup = float("-inf")
        self._known_total_bytes: int | None = None
        self.dropped_events = 0
        self.write_failures = 0
        self.disk_capped = False
        self._thread = threading.Thread(target=self._run, name="chat-trace-writer", daemon=True)
        self._thread.start()

    def _warn(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_warning < 60:
                return
            self._last_warning = now
            dropped, failed = self.dropped_events, self.write_failures
        logger.warning("聊天追踪导出不完整：dropped_events=%d write_failures=%d disk_capped=%s",
                       dropped, failed, self.disk_capped)

    def emit(self, event: dict) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self.queue.put_nowait(event)
                    return
                except queue.Full:
                    pass
            self.dropped_events += 1
        self._warn()

    def stats(self) -> dict:
        with self._lock:
            return {"dropped_events": self.dropped_events, "write_failures": self.write_failures}

    def capacity_stats(self) -> dict:
        with self._lock:
            return {"disk_capped": self.disk_capped}

    def _cleanup(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for path in self.directory.glob("chat-trace-*.jsonl*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        self._known_total_bytes = None
        self._last_cleanup = time.monotonic()

    def _total_bytes(self) -> int:
        if self._known_total_bytes is None:
            total = 0
            for path in self.directory.glob("chat-trace-*.jsonl*"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
            self._known_total_bytes = total
        return self._known_total_bytes

    def _write(self, event: dict) -> None:
        payload = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("trace event exceeds file limit")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (time.monotonic() - self._last_cleanup) >= self.cleanup_interval_seconds:
            self._cleanup()
        if (self.max_total_bytes is not None
                and self._total_bytes() + len(payload) > self.max_total_bytes):
            with self._lock:
                self.disk_capped = True
                self.dropped_events += 1
            self._warn()
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = self.directory / f"{self._prefix}-{day}.jsonl"
        if path.exists() and path.stat().st_size + len(payload) > self.max_bytes:
            for index in range(self.backup_count, 0, -1):
                source = path if index == 1 else Path(f"{path}.{index - 1}")
                target = Path(f"{path}.{index}")
                if source.exists():
                    source.replace(target)
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "ab") as output:
            output.write(payload)
        self._known_total_bytes = self._total_bytes() + len(payload)

    def _run(self) -> None:
        while not self._stopping.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write(event)
            except Exception:
                with self._lock:
                    self.write_failures += 1
                self._warn()
            finally:
                self.queue.task_done()

    def close(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._closed = True
            self._stopping.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("聊天追踪关闭等待超时，剩余队列可能未写完：pending=%d", self.queue.qsize())
            return False
        return True


_exporter: JsonlExporter | None = None


def get_exporter():
    return _exporter


def start_exporter() -> None:
    global _exporter
    if settings.observability_enabled and _exporter is None:
        try:
            _exporter = JsonlExporter(
                ROOT / "data/logs/traces", queue_size=settings.trace_queue_size,
                max_bytes=settings.trace_max_file_bytes, backup_count=settings.trace_backup_count,
                retention_days=settings.trace_retention_days,
                max_total_bytes=settings.trace_max_total_bytes,
                cleanup_interval_seconds=settings.trace_cleanup_interval_seconds,
            )
        except Exception:
            logger.warning("聊天追踪导出器启动失败，问答继续运行")


def stop_exporter() -> None:
    global _exporter
    active, _exporter = _exporter, None
    if active is not None:
        active.close()
