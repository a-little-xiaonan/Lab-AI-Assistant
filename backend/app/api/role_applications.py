"""用户成员申请与管理员审批接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user, require_roles
from app.models.database import RoleApplication, User
from app.models.schemas import RoleApplicationCreate, RoleApplicationOut, RoleApplicationReview
from app.services.role_application import cancel_application, create_application, review_application
from app.store.db import get_db

router = APIRouter(tags=["role-applications"])
require_admin = require_roles("admin")


@router.post("/users/me/role-applications", response_model=RoleApplicationOut, status_code=201)
def apply(body: RoleApplicationCreate, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)) -> RoleApplication:
    return create_application(db, user, body.reason, body.evidence_text)


@router.get("/users/me/role-applications", response_model=list[RoleApplicationOut])
def my_applications(db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)) -> list[RoleApplication]:
    return list(db.scalars(select(RoleApplication).where(
        RoleApplication.user_id == user.id
    ).order_by(RoleApplication.created_at.desc())))


@router.post("/users/me/role-applications/{application_id}/cancel",
             response_model=RoleApplicationOut)
def cancel(application_id: str, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)) -> RoleApplication:
    return cancel_application(db, user, application_id)


@router.get("/admin/role-applications")
def admin_list(status: str | None = "pending", offset: int = Query(0, ge=0),
               limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
               _admin: User = Depends(require_admin)) -> dict:
    conditions = [RoleApplication.status == status] if status else []
    total = db.scalar(select(func.count(RoleApplication.id)).where(*conditions)) or 0
    rows = db.execute(select(RoleApplication, User.username, User.nickname).join(
        User, User.id == RoleApplication.user_id
    ).where(*conditions).order_by(RoleApplication.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit, "items": [
        {**RoleApplicationOut.model_validate(row).model_dump(),
         "username": username, "nickname": nickname}
        for row, username, nickname in rows
    ]}


@router.get("/admin/role-applications/{application_id}")
def admin_detail(application_id: str, db: Session = Depends(get_db),
                 _admin: User = Depends(require_admin)) -> dict:
    row = db.get(RoleApplication, application_id)
    if row is None:
        from app.api.errors import NotFoundError
        raise NotFoundError("role_application_not_found", "申请不存在")
    user = db.get(User, row.user_id)
    return {**RoleApplicationOut.model_validate(row).model_dump(),
            "username": user.username if user else "", "nickname": user.nickname if user else ""}


@router.post("/admin/role-applications/{application_id}/approve", response_model=RoleApplicationOut)
def approve(application_id: str, body: RoleApplicationReview,
            db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> RoleApplication:
    return review_application(db, admin, application_id, True, body.comment)


@router.post("/admin/role-applications/{application_id}/reject", response_model=RoleApplicationOut)
def reject(application_id: str, body: RoleApplicationReview,
           db: Session = Depends(get_db), admin: User = Depends(require_admin)) -> RoleApplication:
    return review_application(db, admin, application_id, False, body.comment)
