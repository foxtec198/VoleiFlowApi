import os

import click
from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate
from flask.cli import with_appcontext
from utils.bps import blueprints
from utils.db import db
from utils.errors import ApiError
from utils.socekt import socketio
load_dotenv()

migrate = Migrate()

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET", ""),
        SQLALCHEMY_DATABASE_URI=os.getenv("DB_URI", "sqlite:///voleiflow.db"),
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
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    CORS(app, resources={r"/api/*": {"origins": "*"}})
    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent")

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

    return app


app = create_app()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "7000")),
        debug=os.getenv("DEBUG", "false").lower() == "true",
    )
