import smtplib
from datetime import date
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

from flask import current_app, has_request_context, request

from models import Place
from utils.db import db


def _is_local_url(value):
    hostname = urlparse(value).hostname
    return hostname in {"localhost", "127.0.0.1", "::1"}


def public_app_url():
    """Resolve a origem pública do app sem deixar localhost vazar para e-mails."""
    configured = str(current_app.config.get("APP_URL") or "").strip().rstrip("/")
    if configured and not _is_local_url(configured):
        return configured

    if has_request_context():
        forwarded_host = str(request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        host = forwarded_host or request.host
        forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        protocol = forwarded_proto or request.scheme or "https"
        request_url = f"{protocol}://{host}" if host else ""
        if request_url and not _is_local_url(request_url):
            return request_url.rstrip("/")

    return configured or "http://localhost:5173"


def confirmation_email_html(player, event, place, link):
    player_name = escape(player.name)
    event_title = escape(event.title)
    place_name = escape(place.name)
    safe_link = escape(link, quote=True)
    year = date.today().year
    return f"""\
<!doctype html>
<html lang="pt-BR">
  <body style="margin:0;padding:0;background:#eef2ee;color:#163026;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:32px 16px;">
      <tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #d9e4dc;border-radius:20px;overflow:hidden;">
          <tr><td style="padding:28px 32px;background:#123d2d;color:#ffffff;">
            <div style="font-size:12px;font-weight:700;letter-spacing:1.5px;color:#d4f55b;">VOLEIFLOW</div>
            <div style="margin-top:9px;font-size:25px;font-weight:700;line-height:1.2;">Confirme sua presença 🏐</div>
          </td></tr>
          <tr><td style="padding:32px;">
            <p style="margin:0 0 14px;font-size:16px;line-height:1.55;">Olá, <strong>{player_name}</strong>!</p>
            <p style="margin:0 0 22px;font-size:15px;line-height:1.55;color:#496057;">Sua inscrição foi recebida. Confirme a presença para garantir sua prioridade na vaga.</p>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 26px;background:#f2f7f3;border-radius:14px;">
              <tr><td style="padding:18px 20px;">
                <div style="font-size:12px;font-weight:700;letter-spacing:.8px;color:#507064;">PRÓXIMO JOGO</div>
                <div style="margin-top:6px;font-size:18px;font-weight:700;color:#163026;">{event_title}</div>
                <div style="margin-top:8px;font-size:14px;color:#496057;">📍 {place_name}<br>📅 {event.game_date:%d/%m/%Y} às {event.starts_at:%H:%M}</div>
              </td></tr>
            </table>
            <table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="border-radius:10px;background:#d4f55b;">
              <a href="{safe_link}" style="display:inline-block;padding:14px 22px;color:#122a23;text-decoration:none;font-size:15px;font-weight:700;">Confirmar presença →</a>
            </td></tr></table>
            <p style="margin:26px 0 0;font-size:12px;line-height:1.55;color:#6a7c74;">Se o botão não funcionar, copie e cole este link no navegador:<br><a href="{safe_link}" style="color:#1b5b46;word-break:break-all;">{safe_link}</a></p>
          </td></tr>
          <tr><td style="padding:18px 32px;border-top:1px solid #e2ebe5;color:#829087;font-size:11px;text-align:center;">© {year} VoleiFlow · Gestão de jogos e inscrições</td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def send_confirmation(player, event, token):
    base_url = public_app_url()
    place = db.session.get(Place, event.place_id)
    link = f"https://voleiflow.hubbix.com.br/{place.slug}/confirmar/{token}"
    host = current_app.config.get("SMTP_HOST")
    if not host:
        current_app.logger.warning("SMTP não configurado; confirmação disponível em %s", link)
        return False
    message = EmailMessage()
    message["Subject"] = f"Confirme sua inscrição — {event.title}"
    message["From"] = current_app.config["SMTP_FROM"]
    message["To"] = player.email
    message.set_content(
        f"Olá, {player.name}!\n\nConfirme sua participação em {event.title} "
        f"no dia {event.game_date:%d/%m/%Y} às {event.starts_at:%H:%M}:\n{link}\n"
    )
    message.add_alternative(confirmation_email_html(player, event, place, link), subtype="html")
    with smtplib.SMTP(host, current_app.config["SMTP_PORT"], timeout=10) as smtp:
        smtp.starttls()
        if current_app.config.get("SMTP_USER"):
            smtp.login(current_app.config["SMTP_USER"], current_app.config["SMTP_PASSWORD"])
        smtp.send_message(message)
    return True
