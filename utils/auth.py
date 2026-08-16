from functools import wraps

from flask import current_app, g, request
from jwt import ExpiredSignatureError, InvalidTokenError

from models import Admin
from utils.db import db
from utils.errors import ApiError
from utils.token import decode_token


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config.get("AUTH_DISABLED"):
            return view(*args, **kwargs)
        token = request.headers.get("Access-Token", "")
        if not token:
            authorization = request.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not token:
            raise ApiError("Login administrativo necessário.", 401)
        try:
            payload = decode_token(token)
        except ExpiredSignatureError:
            raise ApiError("Sessão administrativa expirada.", 401) from None
        except InvalidTokenError:
            raise ApiError("Sessão administrativa inválida.", 401) from None
        admin = db.session.get(Admin, int(payload.get("sub", 0)))
        if not admin or not admin.active or payload.get("role") != "ADMIN" or int(payload.get("ver", -1)) != admin.token_version:
            raise ApiError("Sessão administrativa invalidada.", 401)
        g.admin = admin
        return view(*args, **kwargs)

    return wrapped
