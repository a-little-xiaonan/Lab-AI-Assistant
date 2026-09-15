"""权限兼容入口。

实际规则统一定义在 policy.py；保留本模块导出，避免一次性修改所有旧调用方。
新增代码应直接使用 authorize/enforce_action/require_action。
"""
from app.authorization.policy import (
    ACCESS_LEVELS,
    LEVEL_VALUE,
    AuthorizationDecision,
    authorize,
    can_create_kb,
    can_operate_kb,
    can_read_kb,
    effective_level,
    enforce_action,
    list_operable_kbs,
    list_readable_kbs,
    require_action,
    require_kb_permission,
)

__all__ = [
    "ACCESS_LEVELS", "LEVEL_VALUE", "AuthorizationDecision", "authorize",
    "can_create_kb", "can_operate_kb", "can_read_kb", "effective_level",
    "enforce_action", "list_operable_kbs", "list_readable_kbs",
    "require_action", "require_kb_permission",
]
