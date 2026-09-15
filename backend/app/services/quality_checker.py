"""文档入库前质量检查：阻断明显不可发布的内容，警告交给人工判断。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.database import Document, DocumentQualityCheck, DocumentVersion

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(password|passwd|密码)\s*[:=]\s*[^\s]{6,}"),
    re.compile(r"(?i)mysql(?:\+\w+)?://[^\s]+"),
    re.compile(r"(?i)(jwt_secret|api_key)\s*[:=]\s*[^\s]{12,}"),
)
_PRIVACY_PATTERNS = {
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "身份证号": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "学号名单": re.compile(r"(?i)(学号|学生名单|联系方式)[：:\s]{0,8}\d{6,}"),
}


def _result(version_id: str, kind: str, severity: str, passed: bool, details: dict) -> DocumentQualityCheck:
    return DocumentQualityCheck(
        id=f"qc_{uuid4().hex[:20]}", document_version_id=version_id,
        check_type=kind, severity=severity, result="passed" if passed else "failed",
        details_json=json.dumps(details, ensure_ascii=False),
    )


def run_quality_checks(db: Session, version: DocumentVersion, chunks: list) -> bool:
    """重建当前版本检查结果，返回是否存在 blocking 失败。调用方负责提交事务。"""
    db.execute(delete(DocumentQualityCheck).where(
        DocumentQualityCheck.document_version_id == version.id
    ))
    full_text = "\n".join(chunk.text for chunk in chunks).strip()
    document = db.get(Document, version.document_id)
    version.content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest() if full_text else None

    checks: list[DocumentQualityCheck] = []
    checks.append(_result(version.id, "content_not_empty", "blocking", bool(full_text), {
        "message": "已提取正文" if full_text else "未提取到可用正文",
        "characters": len(full_text), "chunks": len(chunks),
    }))

    secret_hits = [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(full_text)]
    checks.append(_result(version.id, "sensitive_content", "blocking", not secret_hits, {
        "message": "未发现明显密钥或口令" if not secret_hits else "发现疑似密钥、口令或数据库连接串",
        "matched_rules": secret_hits,
    }))

    # 对外资料不得包含手机号、身份证号或带学号的人员名单；内部资料仅提示人工复核。
    privacy_hits = [name for name, pattern in _PRIVACY_PATTERNS.items() if pattern.search(full_text)]
    public_document = document is None or document.sensitivity_level in {"guest", "student"}
    checks.append(_result(
        version.id, "privacy_content", "blocking" if public_document else "warning",
        not privacy_hits,
        {"message": "未发现明显个人敏感信息" if not privacy_hits else "发现疑似个人敏感信息，请脱敏后发布",
         "matched_rules": privacy_hits, "public_document": public_document},
    ))

    metadata_ok = bool(
        document and (document.content_owner or "").strip()
        and (document.source_name or "").strip()
    )
    checks.append(_result(version.id, "governance_metadata", "warning", metadata_ok, {
        "message": "责任人与来源信息完整" if metadata_ok else "缺少内容责任人或资料来源",
    }))

    now = datetime.utcnow()
    time_window_ok = not (
        document and document.effective_at and document.expires_at
        and document.expires_at <= document.effective_at
    )
    expired = bool(document and document.expires_at and document.expires_at <= now)
    checks.append(_result(version.id, "effective_window", "blocking", time_window_ok and not expired, {
        "message": "生效窗口有效" if time_window_ok and not expired else "生效时间配置错误或资料已过期",
        "effective_at": str(document.effective_at) if document and document.effective_at else None,
        "expires_at": str(document.expires_at) if document and document.expires_at else None,
    }))

    bad_chunks = sum(1 for chunk in chunks if len(chunk.text.strip()) < 20)
    ratio = bad_chunks / len(chunks) if chunks else 1.0
    checks.append(_result(version.id, "chunk_quality", "warning", ratio <= 0.3, {
        "message": "分块长度分布正常" if ratio <= 0.3 else "短分块比例偏高，建议人工检查",
        "short_chunks": bad_chunks, "ratio": round(ratio, 4),
    }))

    duplicate = None
    if version.content_hash:
        duplicate = db.scalar(select(DocumentVersion).where(
            DocumentVersion.content_hash == version.content_hash,
            DocumentVersion.id != version.id,
        ).limit(1))
    checks.append(_result(version.id, "duplicate_content", "warning", duplicate is None, {
        "message": "未发现重复正文" if duplicate is None else "正文与已有版本重复",
        "duplicate_version_id": duplicate.id if duplicate else None,
    }))
    db.add_all(checks)
    return any(row.severity == "blocking" and row.result == "failed" for row in checks)
