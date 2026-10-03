"""应用异常。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ParseError(AppError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details=None):
        super().__init__(code, message, 422, details)
        self.retryable = retryable
