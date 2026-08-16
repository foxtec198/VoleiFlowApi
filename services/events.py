import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import BlacklistEntry, Event, Player, Position, Registration, Shift
from services.catalog import get_settings
from services.common import paginate, parse_date, parse_datetime, parse_time, utcnow
from services.mailer import send_confirmation
from utils.db import db
from utils.errors import ApiError, require_fields

REGISTRATION_STATUSES = {
    "confirmed", "pending_confirmation", "waitlist", "cancelled", "present",
    "justified_absence", "unjustified_absence",
}


def event_dict(event, detailed=False):
    data = event.to_dict()
    data["shifts"] = [shift.to_dict() for shift in event.shifts]
    if detailed:
        registrations = Registration.query.filter_by(event_id=event.id).order_by(Registration.created_at).all()
        data["registrations"] = [registration_dict(item, admin=True) for item in registrations]
        data["summary"] = registration_summary(registrations)
        data["vacancies"] = vacancy_summary(event, registrations)
    return data


def registration_dict(registration, admin=False):
    data = registration.to_dict()
    data.update({
        "player_name": registration.snapshot_name,
        "primary_position": registration.primary_position.name,
        "secondary_position": registration.secondary_position.name if registration.secondary_position else None,
        "assigned_position": registration.assigned_position.name if registration.assigned_position else None,
        "shift": registration.shift.name,
        "overall": registration.overall,
    })
    data.pop("email_confirmation_token", None)
    if not admin:
        for key in tuple(data):
            if key.startswith("snapshot_") and key != "snapshot_name":
                data.pop(key)
    return data


def registration_summary(registrations):
    result = {status: 0 for status in REGISTRATION_STATUSES}
    for item in registrations:
        result[item.status] = result.get(item.status, 0) + 1
    result["total"] = len(registrations)
    return result


def vacancy_summary(event, registrations=None):
    registrations = registrations if registrations is not None else Registration.query.filter_by(event_id=event.id).all()
    result = []
    for shift in event.shifts:
        shift_regs = [r for r in registrations if r.shift_id == shift.id and r.status not in {"cancelled", "waitlist"}]
        for position in Position.query.filter_by(active=True).order_by(Position.name):
            occupied = sum(r.primary_position_id == position.id for r in shift_regs)
            capacity = position.required_per_team * event.team_count
            result.append({"shift_id": shift.id, "shift": shift.name, "position_id": position.id,
                           "position": position.name, "capacity": capacity, "occupied": occupied,
                           "available": max(capacity - occupied, 0)})
    return result


class EventService:
    @staticmethod
    def list():
        from flask import request
        query = Event.query
        if request.args.get("status"):
            query = query.filter_by(status=request.args["status"])
        else:
            query = query.filter(Event.status != "deleted")
        if request.args.get("from"):
            query = query.filter(Event.game_date >= parse_date(request.args["from"], "from"))
        return paginate(query.order_by(Event.game_date.desc(), Event.starts_at), event_dict)

    @staticmethod
    def remove(event, scope="single"):
        if scope not in {"single", "recurrence"}:
            raise ApiError("Escopo de remoção inválido.", 422)
        query = Event.query.filter_by(id=event.id)
        if scope == "recurrence" and event.recurrence_group:
            query = Event.query.filter_by(recurrence_group=event.recurrence_group)
        affected = query.filter(Event.status != "deleted").update(
            {Event.status: "deleted", Event.updated_at: utcnow()}, synchronize_session=False
        )
        db.session.commit()
        return {
            "message": "Evento removido." if affected == 1 else f"{affected} eventos da recorrência removidos.",
            "removed_count": affected,
            "scope": scope,
            "recurrence_group": event.recurrence_group,
        }

    @staticmethod
    def save(data, event=None):
        require_fields(data, "title", "game_date", "starts_at", "registration_opens_at", "shift_ids")
        game_date = parse_date(data["game_date"], "game_date")
        starts_at = parse_time(data["starts_at"], "starts_at")
        settings = get_settings()
        team_count = int(data.get("team_count", settings["max_teams_per_event"]))
        if not 1 <= team_count <= int(settings["max_teams_per_event"]):
            raise ApiError(f"A quantidade de times deve estar entre 1 e {settings['max_teams_per_event']}.", 422)
        shifts = Shift.query.filter(Shift.id.in_(data["shift_ids"]), Shift.active.is_(True)).all()
        if len(shifts) != len(set(data["shift_ids"])):
            raise ApiError("Um ou mais turnos são inválidos ou inativos.", 422)
        deadline = data.get("confirmation_deadline")
        if deadline:
            deadline = parse_datetime(deadline, "confirmation_deadline")
        else:
            deadline = datetime.combine(game_date, starts_at, tzinfo=timezone.utc) - timedelta(
                days=int(settings["confirmation_deadline_days"])
            )
        event = event or Event()
        event.title = str(data["title"]).strip()
        event.game_date = game_date
        event.starts_at = starts_at
        event.registration_opens_at = parse_datetime(data["registration_opens_at"], "registration_opens_at")
        event.confirmation_deadline = deadline
        event.team_count = team_count
        event.status = data.get("status", "scheduled")
        event.shifts = shifts
        db.session.add(event)
        db.session.commit()
        return event_dict(event, detailed=True)

    @staticmethod
    def create_recurring(data):
        dates = data.get("dates", [])
        if not dates:
            require_fields(data, "start_date", "occurrences", "weekdays")
            cursor = parse_date(data["start_date"], "start_date")
            weekdays = {int(day) for day in data["weekdays"]}
            occurrences = min(int(data["occurrences"]), 52)
            while len(dates) < occurrences:
                if cursor.weekday() in weekdays:
                    dates.append(cursor.isoformat())
                cursor += timedelta(days=1)
        group = str(uuid.uuid4())
        created = []
        try:
            for raw_date in dates[:52]:
                payload = {**data, "game_date": raw_date}
                payload.pop("dates", None)
                event_data = EventService.save(payload)
                event = db.session.get(Event, event_data["id"])
                event.recurrence_group = group
                event.recurrence_rule = {key: data.get(key) for key in ("weekdays", "occurrences")}
                created.append(event)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return [event_dict(item) for item in created]


def active_blacklist(player_id):
    return BlacklistEntry.query.filter_by(player_id=player_id, removed_at=None).order_by(BlacklistEntry.included_at.desc()).first()


def position_has_capacity(event, shift_id, position_id, exclude_registration_id=None):
    position = db.session.get(Position, position_id)
    if not position:
        return False
    query = Registration.query.filter(
        Registration.event_id == event.id,
        Registration.shift_id == shift_id,
        Registration.primary_position_id == position_id,
        Registration.status.in_(["confirmed", "pending_confirmation", "present"]),
    )
    if exclude_registration_id:
        query = query.filter(Registration.id != exclude_registration_id)
    return query.count() < position.required_per_team * event.team_count


class RegistrationService:
    @staticmethod
    def create(data):
        require_fields(data, "event_id", "player_id", "shift_id", "primary_position_id")
        event = db.session.get(Event, data["event_id"])
        player = db.session.get(Player, data["player_id"])
        shift = db.session.get(Shift, data["shift_id"])
        primary = db.session.get(Position, data["primary_position_id"])
        secondary = db.session.get(Position, data.get("secondary_position_id")) if data.get("secondary_position_id") else None
        if not event or event.status != "scheduled":
            raise ApiError("Evento indisponível para inscrição.", 422)
        opens_at = event.registration_opens_at
        if opens_at.tzinfo is None:
            opens_at = opens_at.replace(tzinfo=timezone.utc)
        if utcnow() < opens_at:
            raise ApiError("As inscrições ainda não foram liberadas.", 422)
        if not player or not player.active:
            raise ApiError("Jogador inválido ou inativo.", 422)
        if not shift or shift not in event.shifts:
            raise ApiError("Turno não disponível neste evento.", 422)
        if not primary or not primary.active or (secondary and not secondary.active):
            raise ApiError("Posição inválida ou inativa.", 422)
        blocked = active_blacklist(player.id)
        has_capacity = position_has_capacity(event, shift.id, primary.id)
        registration = Registration(
            event=event, player=player, shift=shift, primary_position=primary, secondary_position=secondary,
            status="waitlist" if blocked or not has_capacity else "pending_confirmation",
            notes=str(data.get("notes", "")).strip() or None,
            email_confirmation_token=secrets.token_urlsafe(32),
            snapshot_name=player.name,
            snapshot_knowledge_level=player.knowledge_level,
            snapshot_reception=player.reception,
            snapshot_setting=player.setting,
            snapshot_blocking=player.blocking,
            snapshot_serving=player.serving,
            snapshot_attack=player.attack,
            snapshot_defense=player.defense,
        )
        try:
            db.session.add(registration)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise ApiError("Jogador já inscrito neste evento e turno.", 409) from None
        try:
            sent = send_confirmation(player, event, registration.email_confirmation_token)
        except Exception:
            from flask import current_app
            current_app.logger.exception("Falha ao enviar confirmação da inscrição %s", registration.id)
            sent = False
        response = registration_dict(registration)
        response["confirmation_email_sent"] = sent
        return response

    @staticmethod
    def confirm(token):
        registration = Registration.query.filter_by(email_confirmation_token=token).first()
        if not registration:
            raise ApiError("Link de confirmação inválido.", 404)
        if registration.status == "cancelled":
            raise ApiError("Esta inscrição foi cancelada.", 409)
        registration.email_confirmed_at = registration.email_confirmed_at or utcnow()
        registration.confirmed_at = utcnow()
        if active_blacklist(registration.player_id):
            registration.status = "waitlist"
        else:
            position = registration.primary_position
            confirmed_count = Registration.query.filter(
                Registration.event_id == registration.event_id,
                Registration.shift_id == registration.shift_id,
                Registration.primary_position_id == registration.primary_position_id,
                Registration.id != registration.id,
                Registration.status.in_(["confirmed", "present"]),
            ).count()
            capacity = position.required_per_team * registration.event.team_count
            if confirmed_count < capacity:
                registration.status = "confirmed"
                pending = Registration.query.filter(
                    Registration.event_id == registration.event_id,
                    Registration.shift_id == registration.shift_id,
                    Registration.primary_position_id == registration.primary_position_id,
                    Registration.status == "pending_confirmation",
                ).order_by(Registration.created_at.desc(), Registration.id.desc()).all()
                overflow = confirmed_count + 1 + len(pending) - capacity
                for item in pending[:max(overflow, 0)]:
                    item.status = "waitlist"
            else:
                registration.status = "waitlist"
        db.session.commit()
        return registration_dict(registration)

    @staticmethod
    def update_status(registration, status, reason=None):
        if status not in REGISTRATION_STATUSES:
            raise ApiError("Status de inscrição inválido.", 422)
        registration.status = status
        registration.absence_reason = str(reason).strip() if reason else None
        if status == "confirmed":
            registration.confirmed_at = utcnow()
        if status == "unjustified_absence" and not active_blacklist(registration.player_id):
            db.session.add(BlacklistEntry(
                player_id=registration.player_id,
                reason=registration.absence_reason or "Falta injustificada",
                origin="unjustified_absence",
                source_event_id=registration.event_id,
            ))
        db.session.commit()
        return registration_dict(registration, admin=True)


class BlacklistService:
    @staticmethod
    def list():
        from flask import request
        query = BlacklistEntry.query
        if request.args.get("active", "").lower() in {"true", "1"}:
            query = query.filter(BlacklistEntry.removed_at.is_(None))
        return paginate(query.order_by(BlacklistEntry.included_at.desc()), BlacklistService.serialize)

    @staticmethod
    def serialize(entry):
        data = entry.to_dict()
        data["player_name"] = entry.player.name
        data["active"] = entry.removed_at is None
        return data

    @staticmethod
    def add(data):
        require_fields(data, "player_id", "reason")
        if not db.session.get(Player, data["player_id"]):
            raise ApiError("Jogador não encontrado.", 404)
        if active_blacklist(data["player_id"]):
            raise ApiError("Jogador já está na Lista Negra.", 409)
        entry = BlacklistEntry(player_id=data["player_id"], reason=str(data["reason"]).strip(),
                               origin=data.get("origin", "manual"), source_event_id=data.get("source_event_id"))
        db.session.add(entry)
        db.session.commit()
        return BlacklistService.serialize(entry)

    @staticmethod
    def remove(entry, reason=None):
        if entry.removed_at:
            raise ApiError("Este bloqueio já foi removido.", 409)
        entry.removed_at = utcnow()
        entry.removal_reason = str(reason).strip() if reason else None
        db.session.commit()
        return BlacklistService.serialize(entry)
