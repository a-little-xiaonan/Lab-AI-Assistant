"""请求级截止时间与故障记录；ContextVar 隔离并发请求。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Event


@dataclass
class ExecutionBudget:
    deadline: float
    cancelled: Event = field(default_factory=Event)
    failures: list[str] = field(default_factory=list)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if self.cancelled.is_set() or left <= 0:
            raise TimeoutError("request budget exhausted")
        return left


current_budget: ContextVar[ExecutionBudget | None] = ContextVar("agent_budget", default=None)
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-bounded")
_SLOTS = BoundedSemaphore(8)


def timeout_seconds(default: float) -> float:
    budget = current_budget.get()
    return min(default, budget.remaining()) if budget else default


def record_failure(code: str) -> None:
    budget = current_budget.get()
    if budget:
        budget.failures.append(code)


def submit_context(pool, fn, *args):
    """线程池不会自动继承 ContextVar，显式复制每个任务的上下文。"""
    return pool.submit(copy_context().run, fn, *args)


def run_bounded(budget: ExecutionBudget, fn, *args):
    """限流、停止等待、丢弃迟到结果；槽位等底层任务真正结束后释放。"""
    budget.remaining()
    if not _SLOTS.acquire(blocking=False):
        raise RuntimeError("agent workers busy")

    def run():
        token = current_budget.set(budget)
        try:
            budget.remaining()
            return fn(*args)
        finally:
            current_budget.reset(token)

    try:
        future = _POOL.submit(run)
    except BaseException:
        _SLOTS.release()
        raise
    future.add_done_callback(lambda _: _SLOTS.release())
    try:
        result = future.result(timeout=budget.remaining())
        budget.remaining()
        return result
    except TimeoutError:
        budget.cancelled.set()
        future.cancel()
        raise
