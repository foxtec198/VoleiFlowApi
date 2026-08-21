import sqlite3

from sqlalchemy import inspect

from app import create_app
from utils.db import db


def test_existing_database_receives_priority_override_without_migration_history(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE place_players (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
        "PASSWORD_PEPPER": "x" * 48,
        "SECRET_KEY": "test-schema-upgrade",
    })
    with application.app_context():
        columns = {column["name"] for column in inspect(db.engine).get_columns("place_players")}
    assert "priority_override" in columns
