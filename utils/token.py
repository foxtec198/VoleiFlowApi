from datetime import datetime, timedelta, timezone

import jwt
from flask import current_app


def create_admin_token(admin):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(admin.id),
            "role": "ADMIN",
            "ver": int(admin.token_version or 0),
            "iat": now,
            "exp": now + timedelta(hours=8),
        },
        current_app.config["SECRET_KEY"],
        algorithm="HS256",
    )


def decode_token(token):
    return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
