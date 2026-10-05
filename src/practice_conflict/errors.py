"""后端领域错误类型，映射到 HTTP 状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    """入参或时态校验失败。"""

    status_code = 400


class PermissionDeniedError(DomainError):
    """角色或机构隔离校验失败。"""

    status_code = 403


class NotFoundError(DomainError):
    """目标实体不存在。"""

    status_code = 404


class StateConflictError(DomainError):
    """案件状态机不允许的流转。"""

    status_code = 409
