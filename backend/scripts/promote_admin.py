"""本地管理员提升工具。

用法：
    cd backend
    ../.venv/bin/python scripts/promote_admin.py <username>

仅供部署机器的维护者执行；普通 HTTP 接口无法自行提升 admin。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.models.database import Role, User, UserRole  # noqa: E402
from app.store.db import SessionLocal, init_db  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python scripts/promote_admin.py <username>")
        return 2
    username = sys.argv[1].strip().lower()
    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        role = db.scalar(select(Role).where(Role.code == "admin"))
        if user is None:
            print(f"未找到用户：{username}")
            return 1
        if role is None:
            print("admin 角色未初始化")
            return 1
        exists = db.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id))
        if exists is None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
            db.commit()
            print(f"已将 {username} 提升为管理员")
        else:
            print(f"{username} 已是管理员")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
