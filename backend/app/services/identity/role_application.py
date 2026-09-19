"""实验室成员申请业务：申请、撤销与管理员审批均在事务内完成。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import ApiError, ConflictError, NotFoundError
from app.authorization.policy import effective_level
from app.models.database import Role, RoleApplication, User, UserRole, utcnow
from app.services.operations.audit import record_in_transaction


def create_application(db: Session, user: User, reason: str,
                       evidence_text: str | None = None) -> RoleApplication:
    if effective_level(user) in {"editor", "admin"}:
        raise ConflictError("role_already_granted", "当前账号已具备实验室成员或更高权限")
    row = RoleApplication(
        id=f"app_{uuid4().hex[:20]}", user_id=user.id, target_role="editor",
        reason=reason.strip(), evidence_text=evidence_text.strip() if evidence_text else None,
        pending_key=user.id,
    )
    db.add(row)
    record_in_transaction(db, user, "role_application.submit", "role_application", row.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("pending_application_exists", "已有待审批申请，请勿重复提交")
    db.refresh(row)
    return row


def cancel_application(db: Session, user: User, application_id: str) -> RoleApplication:
    row = db.scalar(select(RoleApplication).where(
        RoleApplication.id == application_id, RoleApplication.user_id == user.id
    ).with_for_update())
    if row is None:
        raise NotFoundError("role_application_not_found", "申请不存在")
    if row.status != "pending":
        raise ConflictError("application_not_pending", "只有待审批申请可以撤销")
    row.status, row.pending_key, row.updated_at = "cancelled", None, utcnow()
    record_in_transaction(db, user, "role_application.cancel", "role_application", row.id)
    db.commit()
    return row


def review_application(db: Session, admin: User, application_id: str,
                       approve: bool, comment: str | None) -> RoleApplication:
    row = db.scalar(select(RoleApplication).where(
        RoleApplication.id == application_id
    ).with_for_update())
    if row is None:
        raise NotFoundError("role_application_not_found", "申请不存在")
    if row.status != "pending":
        raise ConflictError("application_already_reviewed", "该申请已被处理")
    if row.user_id == admin.id:
        raise ApiError(403, "cannot_review_self", "不能审批自己的申请")
    applicant = db.get(User, row.user_id)
    if applicant is None or applicant.status != "active" or applicant.deleted_at is not None:
        raise ConflictError("applicant_unavailable", "申请账号已不可用")
    if not approve and not (comment or "").strip():
        raise ApiError(422, "review_comment_required", "驳回申请必须填写原因")
    if approve:
        editor = db.scalar(select(Role).where(Role.code == "editor"))
        if editor is None:
            raise ApiError(500, "roles_not_initialized", "editor 角色尚未初始化")
        exists = db.scalar(select(UserRole).where(
            UserRole.user_id == applicant.id, UserRole.role_id == editor.id
        ))
        if exists is None:
            db.add(UserRole(user_id=applicant.id, role_id=editor.id))
    now = utcnow()
    row.status = "approved" if approve else "rejected"
    row.pending_key = None
    row.reviewed_by = admin.id
    row.review_comment = (comment or "").strip() or None
    row.reviewed_at = row.updated_at = now
    action = "role_application.approve" if approve else "role_application.reject"
    record_in_transaction(db, admin, action, "role_application", row.id,
                          detail={"applicant_id": applicant.id, "target_role": "editor"})
    db.commit()
    db.refresh(row)
    return row
