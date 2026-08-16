import secrets

import pytest

from app import create_app
from models import Admin, Place, Position
from utils.db import db
from utils.password_security import hash_password
from utils.token import create_admin_token


@pytest.fixture()
def app():
    test_admin_password = f"Aa!9{secrets.token_urlsafe(18)}"
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "PASSWORD_PEPPER": "x" * 48,
        "SECRET_KEY": secrets.token_urlsafe(48),
        "TEST_ADMIN_PASSWORD": test_admin_password,
        "AUTH_DISABLED": False,
        "SMTP_HOST": "",
    })
    with application.app_context():
        db.create_all()
        db.session.add(Place(name="Nilo", slug="nilo", active=True))
        db.session.add_all([
            Position(name="Ponteiro", required_per_team=1),
            Position(name="Central", required_per_team=0),
            Position(name="Líbero", required_per_team=0),
            Position(name="Levantador", required_per_team=0),
            Position(name="Oposto", required_per_team=0),
        ])
        db.session.add(Admin(name="Administrador", email="admin@example.com", password_hash=hash_password(test_admin_password)))
        db.session.commit()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    test_client = app.test_client()
    with app.app_context():
        admin = Admin.query.filter_by(email="admin@example.com").one()
        test_client.environ_base["HTTP_ACCESS_TOKEN"] = create_admin_token(admin)
    return test_client
