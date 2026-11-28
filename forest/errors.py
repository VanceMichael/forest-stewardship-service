"""业务错误：携带稳定错误码与中文说明，HTTP 层据此生成状态码。"""


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def bad_request(code: str, message: str) -> AppError:
    return AppError(400, code, message)


def unauthorized(message: str = "缺少有效的演练身份") -> AppError:
    return AppError(401, "unauthorized", message)


def forbidden(message: str) -> AppError:
    return AppError(403, "forbidden", message)


def not_found(message: str) -> AppError:
    return AppError(404, "not_found", message)


def conflict(code: str, message: str) -> AppError:
    return AppError(409, code, message)


def unprocessable(code: str, message: str) -> AppError:
    return AppError(422, code, message)


def bad_gateway(message: str) -> AppError:
    return AppError(502, "bad_gateway", message)
