import smtplib
from email.message import EmailMessage

from flask import current_app

from models import Place
from utils.db import db


def send_confirmation(player, event, token):
    base_url = current_app.config["APP_URL"].rstrip("/")
    place = db.session.get(Place, event.place_id)
    link = f"{base_url}/{place.slug}/confirmar/{token}"
    host = current_app.config.get("SMTP_HOST")
    if not host:
        current_app.logger.warning("SMTP não configurado; confirmação disponível em %s", link)
        return False
    message = EmailMessage()
    message["Subject"] = f"Confirme sua inscrição — {event.title}"
    message["From"] = current_app.config["SMTP_FROM"]
    message["To"] = player.email
    message.set_content(
        f"Olá, {player.name}!\n\nConfirme sua participação em {event.game_date:%d/%m/%Y}:\n{link}\n"
    )
    with smtplib.SMTP(host, current_app.config["SMTP_PORT"], timeout=10) as smtp:
        smtp.starttls()
        if current_app.config.get("SMTP_USER"):
            smtp.login(current_app.config["SMTP_USER"], current_app.config["SMTP_PASSWORD"])
        smtp.send_message(message)
    return True
