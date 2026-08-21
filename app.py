import os

import click
from dotenv import load_dotenv
from flask import Flask, jsonify
from sqlalchemy import inspect, text
from flask_cors import CORS
from flask.cli import with_appcontext
from utils.bps import blueprints
from utils.db import db
from utils.errors import ApiError
from utils.socekt import socketio
load_dotenv()


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET", ""),
        SQLALCHEMY_DATABASE_URI=os.getenv("DB_URI", "sqlite:///voleiflow.db"),
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,
            "pool_recycle": 240,
        },
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        JSON_SORT_KEYS=False,
        PASSWORD_PEPPER=os.getenv("PASSWORD_PEPPER", ""),
        AUTH_DISABLED=False,
        SMTP_HOST=os.getenv("SMTP_HOST", ""),
        SMTP_PORT=int(os.getenv("SMTP_PORT", "587")),
        SMTP_USER=os.getenv("SMTP_USER", ""),
        SMTP_PASSWORD=os.getenv("SMTP_PASSWORD", ""),
        SMTP_FROM=os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
        APP_URL=os.getenv("APP_URL", "http://localhost:5173"),
        DEFAULT_PLACE_SLUG=os.getenv("DEFAULT_PLACE_SLUG", "nilo"),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    CORS(app, resources={r"/api/*": {"origins": "*"}})
    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent")

    with app.app_context():
        inspector = inspect(db.engine)
        if "place_players" in inspector.get_table_names() and db.engine.dialect.name == "postgresql":
            db.session.execute(text(
                "ALTER TABLE place_players ADD COLUMN IF NOT EXISTS priority_override INTEGER"
            ))
            db.session.commit()
        elif "place_players" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("place_players")}
            if "priority_override" not in columns:
                db.session.execute(text(
                    "ALTER TABLE place_players ADD COLUMN priority_override INTEGER"
                ))
                db.session.commit()

    for bp, prefix in blueprints.items():
        app.register_blueprint(bp, url_prefix=prefix)

    @app.get("/")
    def health():
        return {"name": "VoleiFlow API", "status": "ok"}

    @app.errorhandler(ApiError)
    def api_error(error):
        return jsonify({"error": error.message, "details": error.details}), error.status

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Recurso não encontrado."}), 404

    @app.errorhandler(500)
    def server_error(error):
        if app.config["TESTING"]:
            raise error
        return jsonify({"error": "Erro interno do servidor."}), 500

    @app.cli.command("create-admin")
    @click.option("--name", prompt="Nome")
    @click.option("--email", prompt="E-mail")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @with_appcontext
    def create_admin(name, email, password):
        from models import Admin
        from utils.password_security import hash_password, is_strong_password

        email = email.strip().lower()
        if Admin.query.filter(db.func.lower(Admin.email) == email).first():
            raise click.ClickException("Já existe um administrador com este e-mail.")
        if not is_strong_password(password):
            raise click.ClickException("Use ao menos 8 caracteres, com maiúscula, minúscula, número e símbolo.")
        admin = Admin(name=name.strip(), email=email, password_hash=hash_password(password))
        db.session.add(admin)
        db.session.commit()
        click.echo(f"Administrador {email} criado com sucesso.")

    @app.cli.command("init-db")
    @with_appcontext
    def init_db():
        from models import Place, PlaceSetting, Position, Setting

        db.create_all()
        place = Place.query.filter_by(slug=app.config["DEFAULT_PLACE_SLUG"]).first()
        if not place:
            place = Place(name="Nilo", slug=app.config["DEFAULT_PLACE_SLUG"], active=True)
            db.session.add(place)
            db.session.flush()
        defaults = [
            ("Ponteiro", 2), ("Central", 2), ("Líbero", 1),
            ("Levantador", 1), ("Oposto", 1),
        ]
        for name, required in defaults:
            if not Position.query.filter(db.func.lower(Position.name) == name.lower()).first():
                db.session.add(Position(name=name, required_per_team=required, active=True))
        settings = {
            "max_teams_per_event": 3,
            "confirmation_deadline_days": 1,
            "admin_whatsapp": "",
            "imbalance_threshold": 1.5,
        }
        for key, value in settings.items():
            if not db.session.get(Setting, key):
                db.session.add(Setting(key=key, value=value))
            if not PlaceSetting.query.filter_by(place_id=place.id, key=key).first():
                db.session.add(PlaceSetting(place_id=place.id, key=key, value=value))
        db.session.commit()
        click.echo("Banco inicializado sem remover ou sobrescrever registros existentes.")

    return app


app = create_app()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "7000")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
    )
