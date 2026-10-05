"""服务层异常类型，API 层据此映射 HTTP 状态码。"""


class ServiceError(Exception):
    """服务层基础异常。"""


class ValidationError(ServiceError):
    """入参不合法，对应 400。"""


class PermissionDeniedError(ServiceError):
    """越权操作，对应 403。"""


class NotFoundError(ServiceError):
    """对象不存在，对应 404。"""


class StateError(ServiceError):
    """当前状态不允许该操作，对应 409。"""
