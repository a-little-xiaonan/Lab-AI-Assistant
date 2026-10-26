"""统一聊天回合服务：生成前抢占租约，完成时原子保存消息与依据。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.api.errors import BadRequestError, ConflictError
from app.config import settings
from app.core import intent_router
from app.llm.prompt_templates import CONVERSATION_PLAN_VERSION
from app.memory.memory_manager import memory_manager
from app.middleware.request_context import current_request_id
from app.models.database import (
    AnswerProvenance, ChatSession, ChatTurn, Message, utcnow,
)
from app.observability.tracing import (
    PIPELINE_VERSION, bind_turn, current_trace, mark_business_outcome, mark_saved, stage,
)
from app.services.conversation.context import ConversationContext
from app.services.conversation.history import HistorySnapshot, build_history_snapshot

logger = logging.getLogger(__name__)


class TurnOwnershipLost(RuntimeError):
    """当前请求的会话租约已过期或被新回合接管。"""

    code = "turn_ownership_lost"


class TurnLeaseKeeper:
    """使用独立短事务续租，绝不跨线程共享请求 Session。"""

    def __init__(self, factory, session_id: str, turn_id: str,
                 cancellation: threading.Event) -> None:
        self._factory = factory
        self._session_id = session_id
        self._turn_id = turn_id
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._cancellation = cancellation
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"chat-lease-{turn_id[-8:]}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def _run(self) -> None:
        interval = min(settings.chat_turn_renew_interval_seconds,
                       max(5, settings.chat_turn_lease_seconds // 2))
        while not self._stop.wait(interval):
            db = self._factory()
            try:
                expires = utcnow() + timedelta(seconds=settings.chat_turn_lease_seconds)
                result = db.execute(
                    update(ChatSession)
                    .where(ChatSession.id == self._session_id,
                           ChatSession.active_turn_id == self._turn_id)
                    .values(lease_expires_at=expires)
                )
                db.commit()
                if result.rowcount != 1:
                    self.lost.set()
                    self._cancellation.set()
                    return
            except Exception:
                db.rollback()
                self.lost.set()
                self._cancellation.set()
                logger.exception("聊天回合续租失败：session=%s turn=%s",
                                 self._session_id, self._turn_id)
                return
            finally:
                db.close()


class ChatService:
    """一次请求一个实例；API 只负责协议，业务生命周期集中在这里。"""

    def __init__(
        self,
        db: Session,
        session: ChatSession,
        query: str,
        readable_kb_ids: list[str],
        user_id: str | None,
        audience_scope: str,
        target_message_id: int | None = None,
    ) -> None:
        self.db = db
        self.session = session
        self.query = query
        self.readable_kb_ids = readable_kb_ids
        self.user_id = user_id
        self.audience_scope = audience_scope
        self.target_message_id = target_message_id
        self.turn_id = f"turn_{uuid4().hex[:20]}"
        self.snapshot: HistorySnapshot | None = None
        self.db_factory = None
        self.completed = False
        self.closed = False
        self.cancellation = threading.Event()
        self._lease: TurnLeaseKeeper | None = None

    def start(self) -> HistorySnapshot:
        """原子抢占会话并保存 processing 回合与用户消息。"""
        now = utcnow()
        expires = now + timedelta(seconds=settings.chat_turn_lease_seconds)
        self.db.refresh(self.session)
        if self.target_message_id is not None:
            target = self.db.get(Message, self.target_message_id)
            if (target is None or target.session_id != self.session.id
                    or target.role != "assistant"):
                raise BadRequestError("invalid_target_message", "引用的回答不属于当前会话")
        previous_turn = self.session.active_turn_id
        previous_expiry = self.session.lease_expires_at
        revision = self.session.history_revision or 0
        parent_turn_id = self.db.scalar(
            select(ChatTurn.id)
            .where(ChatTurn.session_id == self.session.id, ChatTurn.status == "completed")
            .order_by(ChatTurn.finished_at.desc(), ChatTurn.id.desc())
            .limit(1)
        )
        with stage("turn.acquire", history_revision=revision) as observed:
            result = self.db.execute(
                update(ChatSession)
                .where(
                    ChatSession.id == self.session.id,
                    ChatSession.deleted_at.is_(None),
                    or_(ChatSession.active_turn_id.is_(None),
                        ChatSession.lease_expires_at.is_(None),
                        ChatSession.lease_expires_at <= now),
                )
                .values(active_turn_id=self.turn_id, lease_expires_at=expires)
            )
            if result.rowcount != 1:
                self.db.rollback()
                observed.finish("error", error_code="session_busy", lease_acquired=False)
                raise ConflictError("session_busy", "该会话正在生成上一条回答，请稍后再试")
            if previous_turn and (previous_expiry is None or previous_expiry <= now):
                self.db.execute(
                    update(ChatTurn)
                    .where(ChatTurn.id == previous_turn, ChatTurn.status == "processing")
                    .values(status="abandoned", error_code="lease_expired", finished_at=now)
                )
            trace = current_trace()
            target_ids = [self.target_message_id] if self.target_message_id else []
            self.db.add(ChatTurn(
                id=self.turn_id,
                session_id=self.session.id,
                trace_id=trace.id if trace else None,
                request_id=current_request_id(),
                parent_turn_id=parent_turn_id,
                history_revision=revision,
                status="processing",
                action="answer",
                target_message_ids_json=json.dumps(target_ids),
                plan_version=(
                    CONVERSATION_PLAN_VERSION
                    if settings.conversation_plan_enabled else "intent-v1"
                ),
            ))
            self.db.add(Message(
                session_id=self.session.id,
                turn_id=self.turn_id,
                role="user",
                content=self.query,
            ))
            self.db.commit()
            self.db.refresh(self.session)
            bind_turn(self.turn_id, history_revision=revision, lease_acquired=True)
            observed.set_attributes(lease_acquired=True)

        self.snapshot = build_history_snapshot(self.db, self.session)
        # 请求数据库绑定用于独立续租；expire_on_commit=False 避免触碰请求对象。
        factory = sessionmaker(bind=self.db.get_bind(), autoflush=False, expire_on_commit=False)
        self.db_factory = factory
        self._lease = TurnLeaseKeeper(
            factory, self.session.id, self.turn_id, self.cancellation
        )
        self._lease.start()
        return self.snapshot

    def _conversation_context(self) -> ConversationContext:
        if self.snapshot is None or self.db_factory is None:
            raise RuntimeError("聊天回合尚未开始")
        return ConversationContext(
            session_id=self.session.id,
            snapshot=self.snapshot,
            target_message_id=self.target_message_id,
            db_factory=self.db_factory,
            readable_kb_ids=tuple(self.readable_kb_ids),
        )

    def answer(self) -> dict:
        self._ensure_started()
        return intent_router.answer(
            self.query,
            self.readable_kb_ids,
            session_id=self.session.id,
            user_id=self.user_id,
            audience_scope=self.audience_scope,
            history_context=self.snapshot.text,
            cancellation=self.cancellation,
            include_provenance=True,
            conversation_context=self._conversation_context(),
        )

    def answer_stream(self):
        self._ensure_started()
        return intent_router.answer_stream(
            self.query,
            self.readable_kb_ids,
            session_id=self.session.id,
            user_id=self.user_id,
            audience_scope=self.audience_scope,
            history_context=self.snapshot.text,
            cancellation=self.cancellation,
            include_provenance=True,
            conversation_context=self._conversation_context(),
        )

    @property
    def ownership_lost(self) -> bool:
        return bool(self._lease and self._lease.lost.is_set())

    def complete(self, result: dict) -> Message:
        """助手消息、依据、回合完成与历史版本推进在一个事务提交。"""
        self._ensure_started()
        self._stop_lease()
        if self.ownership_lost:
            raise TurnOwnershipLost("聊天回合租约已丢失")
        provenance = dict(result.get("_provenance") or {})
        now = utcnow()
        assistant = Message(
            session_id=self.session.id,
            turn_id=self.turn_id,
            role="assistant",
            content=result["answer"] if "answer" in result else result["full_text"],
        )
        reference_ids = self._reference_ids(result)
        derived_ids = self._derived_ids(result)
        clarification = result.get("_clarification")
        business_outcome = self._business_outcome(provenance, result)
        with stage("turn.finish", turn_status="completed") as observed:
            try:
                self.db.add(assistant)
                self.db.flush()
                self.db.add(AnswerProvenance(
                    assistant_message_id=assistant.id,
                    turn_id=self.turn_id,
                    trace_id=(current_trace().id if current_trace() else None),
                    pipeline_version=PIPELINE_VERSION,
                    planner_version=provenance.get(
                        "planner_version", result.get("_plan_version", "intent-v1")
                    ),
                    answer_kind=provenance.get("answer_kind", "unknown"),
                    basis_status=provenance.get("basis_status", "unknown"),
                    derived_from_message_ids_json=json.dumps(derived_ids),
                    parts_json=json.dumps(provenance.get("parts", []), ensure_ascii=False),
                    evidence_refs_json=json.dumps(self._evidence_refs(result), ensure_ascii=False),
                ))
                turn = self.db.get(ChatTurn, self.turn_id)
                if turn is None or turn.status != "processing":
                    raise TurnOwnershipLost("聊天回合已不再处理中")
                turn.status = "completed"
                turn.action = provenance.get("action", result.get("_plan_action", "answer"))
                turn.plan_version = result.get("_plan_version", turn.plan_version)
                turn.target_message_ids_json = json.dumps(reference_ids)
                if clarification:
                    turn.pending_clarification_json = json.dumps(
                        self._clarification_state(clarification, now),
                        ensure_ascii=False,
                    )
                turn.finished_at = now
                released = self.db.execute(
                    update(ChatSession)
                    .where(
                        ChatSession.id == self.session.id,
                        ChatSession.active_turn_id == self.turn_id,
                        ChatSession.history_revision == self.snapshot.revision,
                        ChatSession.lease_expires_at > now,
                    )
                    .values(
                        active_turn_id=None,
                        lease_expires_at=None,
                        history_revision=self.snapshot.revision + 1,
                    )
                )
                if released.rowcount != 1:
                    raise TurnOwnershipLost("聊天回合租约或历史版本已变化")
                self.db.commit()
                mark_business_outcome(business_outcome)
            except Exception:
                self.db.rollback()
                observed.finish("error", error_code="turn_commit_failed")
                raise
        self.completed = self.closed = True
        memory_manager.invalidate(self.session.id)
        mark_saved(assistant.id)
        return assistant

    def fail(self, error_code: str, status: str = "failed") -> None:
        """保留用户消息用于界面展示，但失败回合不会进入历史快照。"""
        if self.closed:
            return
        self.cancellation.set()
        self._stop_lease()
        now = utcnow()
        try:
            turn = self.db.get(ChatTurn, self.turn_id)
            if turn is not None and turn.status == "processing":
                turn.status = status
                turn.error_code = error_code[:128]
                turn.finished_at = now
            self.db.execute(
                update(ChatSession)
                .where(ChatSession.id == self.session.id,
                       ChatSession.active_turn_id == self.turn_id)
                .values(active_turn_id=None, lease_expires_at=None)
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception("聊天失败回合收尾失败：session=%s turn=%s",
                             self.session.id, self.turn_id)
        finally:
            self.closed = True

    def cancel(self, error_code: str = "cancelled") -> None:
        self.fail(error_code, status="cancelled")

    def _stop_lease(self) -> None:
        if self._lease is not None:
            self._lease.stop()

    def _ensure_started(self) -> None:
        if self.snapshot is None:
            raise RuntimeError("聊天回合尚未开始")

    @staticmethod
    def _business_outcome(provenance: dict, result: dict) -> str:
        action = provenance.get("action")
        kind = provenance.get("answer_kind")
        parts = provenance.get("parts") or []
        if action == "clarify":
            return "needs_input"
        if kind == "unavailable":
            return "insufficient_evidence"
        if any(isinstance(part, dict) and part.get("status") == "failed" for part in parts):
            return "partial"
        sources = result.get("sources") or []
        refs = result.get("_evidence_refs") or []
        if kind == "knowledge" and not sources and not refs:
            return "no_data"
        return "answered"

    def _reference_ids(self, result: dict) -> list[int]:
        if "_plan_action" in result:
            values = result.get("_reference_message_ids") or []
            return [value for value in values if isinstance(value, int)][:3]
        return [self.target_message_id] if self.target_message_id else []

    def _derived_ids(self, result: dict) -> list[int]:
        if "_plan_action" in result:
            values = result.get("_derived_from_message_ids") or []
            return [value for value in values if isinstance(value, int)][:3]
        # P2 兼容：显式引用目标只作为派生关系保存，P3 由计划明确给出。
        return [self.target_message_id] if self.target_message_id else []

    def _clarification_state(self, clarification: dict, now) -> dict:
        missing = clarification.get("missing_parameters")
        references = clarification.get("reference_message_ids")
        draft = clarification.get("safe_draft")
        try:
            remaining = max(0, int(clarification.get("remaining_turns", 0)))
        except (TypeError, ValueError):
            remaining = 0
        return {
            "version": "clarification-v1",
            "original_question": self.query[:4000],
            "question": str(clarification.get("question", ""))[:200],
            "missing_parameters": [
                str(item)[:64] for item in missing[:3]
            ] if isinstance(missing, list) else [],
            "safe_draft": draft if isinstance(draft, dict) else {},
            "reference_message_ids": [
                item for item in references[:3]
                if isinstance(item, int)
            ] if isinstance(references, list) else [],
            "created_turn_id": self.turn_id,
            "created_at": now.isoformat(),
            "expires_at": (
                now + timedelta(seconds=settings.clarification_ttl_seconds)
            ).isoformat(),
            "remaining_turns": remaining,
        }

    @staticmethod
    def _evidence_refs(result: dict) -> list[dict]:
        internal = result.get("_evidence_refs")
        if isinstance(internal, list):
            return internal[:100]
        refs = []
        for source in result.get("sources", []):
            refs.append({
                "type": "knowledge",
                "source_file": str(source.get("source_file", ""))[:255],
                "page": source.get("page"),
            })
        for source in result.get("tool_sources", []):
            evidence_refs = source.get("evidence_refs", [])
            refs.append({
                "type": str(source.get("type", "tool"))[:32],
                "evidence_refs": evidence_refs[:20] if isinstance(evidence_refs, list) else [],
            })
        return refs
