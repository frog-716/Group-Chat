from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from group_info_system.db.models import (
    CollectionRunRow,
    ExecutionScopeGroupRow,
    ExecutionScopeRow,
)
from group_info_system.db.repositories import db_datetime_as_utc
from group_info_system.group_registry import load_group_registry


class ExecutionScopeError(RuntimeError):
    error_code = "EXECUTION_SCOPE_INVALID"


class ExecutionScopeNotFoundError(ExecutionScopeError):
    error_code = "EXECUTION_SCOPE_NOT_FOUND"


class ScopeGroupNotFoundError(ExecutionScopeError):
    error_code = "SCOPE_GROUP_NOT_FOUND"


class ScopeCollectionIncompleteError(ExecutionScopeError):
    error_code = "SCOPE_COLLECTION_INCOMPLETE"


@dataclass(frozen=True)
class ScopeGroup:
    registry_key: str
    provider: str
    external_id: str
    ordinal: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "registry_key": self.registry_key,
            "provider": self.provider,
            "external_id": self.external_id,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class ExecutionScope:
    id: int
    kind: str
    window_start: datetime
    window_end: datetime
    scope_fingerprint: str
    created_at: datetime
    groups: tuple[ScopeGroup, ...]

    def as_dict(self, *, reused: bool | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "scope_id": self.id,
            "kind": self.kind,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "scope_fingerprint": self.scope_fingerprint,
            "created_at": self.created_at.isoformat(),
            "groups": [group.as_dict() for group in self.groups],
        }
        if reused is not None:
            payload["reused"] = reused
        return payload


def _scope_from_row(session: Session, row: ExecutionScopeRow) -> ExecutionScope:
    groups = tuple(
        ScopeGroup(
            registry_key=group.registry_key,
            provider=group.provider,
            external_id=group.external_id,
            ordinal=group.ordinal,
        )
        for group in session.scalars(
            select(ExecutionScopeGroupRow)
            .where(ExecutionScopeGroupRow.execution_scope_id == row.id)
            .order_by(ExecutionScopeGroupRow.ordinal)
        ).all()
    )
    return ExecutionScope(
        id=row.id,
        kind=row.kind,
        window_start=db_datetime_as_utc(row.window_start),
        window_end=db_datetime_as_utc(row.window_end),
        scope_fingerprint=row.scope_fingerprint,
        created_at=db_datetime_as_utc(row.created_at),
        groups=groups,
    )


def get_scope(session: Session, scope_id: int) -> ExecutionScope:
    row = session.get(ExecutionScopeRow, scope_id)
    if row is None:
        raise ExecutionScopeNotFoundError(f"Execution Scope 不存在：{scope_id}")
    return _scope_from_row(session, row)


def get_scope_group(scope: ExecutionScope, registry_key: str) -> ScopeGroup:
    for group in scope.groups:
        if group.registry_key == registry_key:
            return group
    raise ScopeGroupNotFoundError(f"Scope 中不存在群：{registry_key}")


def _fingerprint(kind: str, start: datetime, end: datetime, groups: tuple[ScopeGroup, ...]) -> str:
    material = {
        "kind": kind,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "groups": [group.as_dict() for group in groups],
    }
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_or_get_scope(
    *,
    session: Session,
    kind: str,
    window_start: datetime,
    window_end: datetime,
    config_path: Path,
) -> tuple[ExecutionScope, bool]:
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ExecutionScopeError("Execution Scope 时间必须包含时区")
    if window_start >= window_end:
        raise ExecutionScopeError("Execution Scope start 必须早于 end")
    with session.no_autoflush:
        existing = session.scalar(
            select(ExecutionScopeRow).where(
                ExecutionScopeRow.kind == kind,
                ExecutionScopeRow.window_start == window_start,
                ExecutionScopeRow.window_end == window_end,
            )
        )
    if existing is not None:
        return _scope_from_row(session, existing), True

    registry = load_group_registry(config_path)
    groups = tuple(
        ScopeGroup(
            registry_key=group.key,
            provider=group.provider,
            external_id=group.external_id,
            ordinal=ordinal,
        )
        for ordinal, group in enumerate(registry.active_groups)
    )
    fingerprint = _fingerprint(kind, window_start, window_end, groups)
    row = ExecutionScopeRow(
        kind=kind,
        window_start=window_start,
        window_end=window_end,
        scope_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        session.flush()
        for group in groups:
            session.add(
                ExecutionScopeGroupRow(
                    execution_scope_id=row.id,
                    registry_key=group.registry_key,
                    provider=group.provider,
                    external_id=group.external_id,
                    ordinal=group.ordinal,
                )
            )
        session.flush()
        session.commit()
        return _scope_from_row(session, row), False
    except IntegrityError:
        session.rollback()
        with session.no_autoflush:
            existing = session.scalar(
                select(ExecutionScopeRow).where(
                    ExecutionScopeRow.kind == kind,
                    ExecutionScopeRow.window_start == window_start,
                    ExecutionScopeRow.window_end == window_end,
                )
            )
        if existing is None:
            raise
        return _scope_from_row(session, existing), True


def ensure_scope_collections_complete(session: Session, scope: ExecutionScope) -> None:
    missing = []
    for group in scope.groups:
        exists = session.scalar(
            select(CollectionRunRow.id)
            .where(
                CollectionRunRow.execution_scope_id == scope.id,
                CollectionRunRow.provider == group.provider,
                CollectionRunRow.chat_external_id == group.external_id,
                CollectionRunRow.requested_since == scope.window_start,
                CollectionRunRow.requested_until == scope.window_end,
                CollectionRunRow.status == "succeeded",
            )
            .limit(1)
        )
        if exists is None:
            missing.append(group.registry_key)
    if missing:
        error = ScopeCollectionIncompleteError(
            "Scope 缺少成功 CollectionRun：" + ", ".join(missing)
        )
        error.__dict__["missing_groups"] = missing
        raise error
