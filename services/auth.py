from datetime import datetime, timezone

from flask import g

from models import Admin
from utils.db import db
from utils.errors import ApiError, require_fields
from utils.password_security import hash_password, verify_password
from utils.token import create_admin_token


def admin_dict(admin):
    return {
        "id": admin.id,
        "name": admin.name,
        "email": admin.email,
        "role": "ADMIN",
        "last_login": admin.last_login.isoformat() if admin.last_login else None,
    }


class AuthService:
    @staticmethod
    def login(data):
        require_fields(data, "email", "password")
        email = str(data["email"]).strip().lower()
        admin = Admin.query.filter(db.func.lower(Admin.email) == email).first()
        if not admin or not admin.active:
            raise ApiError("E-mail ou senha incorretos.", 401)
        valid, needs_rehash = verify_password(data["password"], admin.password_hash)
        if not valid:
            raise ApiError("E-mail ou senha incorretos.", 401)
        if needs_rehash:
            admin.password_hash = hash_password(data["password"])
        admin.last_login = datetime.now(timezone.utc)
        db.session.commit()
        return {**admin_dict(admin), "access_token": create_admin_token(admin)}

    @staticmethod
    def logout():
        g.admin.token_version += 1
        db.session.commit()
        return {"message": "Sessão encerrada."}
