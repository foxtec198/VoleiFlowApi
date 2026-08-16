class ApiError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}


def require_fields(data, *fields):
    missing = [field for field in fields if data.get(field) in (None, "")]
    if missing:
        raise ApiError("Campos obrigatórios ausentes.", 422, {"fields": missing})
