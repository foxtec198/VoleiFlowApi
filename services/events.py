import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import BlacklistEntry, Event, PlacePlayer, Player, Position, Registration, Shift, TeamMember
from services.catalog import get_settings
from services.common import as_bool, paginate, parse_date, parse_datetime, parse_time, utcnow
from services.mailer import send_confirmation
from services.places import current_place
from utils.db import db
from utils.errors import ApiError, require_fields

REGISTRATION_STATUSES = {
    "confirmed", "pending_confirmation", "waitlist", "cancelled", "present",
    "justified_absence", "unjustified_absence",
}
RECURRENCE_HORIZON_DAYS = 120


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
        "selected_period": {
            "id": registration.shift.id,
            "name": registration.shift.name,
            "starts_at": registration.shift.starts_at.isoformat(),
            "ends_at": registration.shift.ends_at.isoformat(),
        },
        "overall": registration.overall,
        "membership": "guest" if registration.is_guest else "member",
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
    def materialize_recurring(place_id=None, horizon_days=RECURRENCE_HORIZON_DAYS):
        place_id = place_id or current_place().id
        groups = db.session.query(Event.recurrence_group).filter(
            Event.place_id == place_id,
            Event.recurrence_group.isnot(None),
            Event.recurrence_rule.isnot(None),
            Event.status != "deleted",
        ).distinct().all()
        created = []
        for (group,) in groups:
            template = Event.query.filter(
                Event.place_id == place_id,
                Event.recurrence_group == group,
                Event.status != "deleted",
            ).order_by(Event.game_date, Event.id).first()
            rule = (template.recurrence_rule or {}) if template else {}
            if not template or not rule.get("unlimited"):
                continue
            weekdays = {int(day) for day in rule.get("weekdays", [])}
            if not weekdays:
                continue
            start_date = parse_date(rule.get("start_date") or template.game_date.isoformat(), "start_date")
            horizon = max(date.today(), start_date) + timedelta(days=horizon_days)
            existing_dates = {
                row.game_date for row in Event.query.filter_by(place_id=place_id, recurrence_group=group).all()
            }
            cursor = max(start_date, date.today())
            while cursor <= horizon:
                if cursor.weekday() in weekdays and cursor not in existing_dates:
                    day_delta = timedelta(days=(cursor - template.game_date).days)
                    occurrence = Event(
                        place_id=place_id,
                        title=template.title,
                        game_date=cursor,
                        starts_at=template.starts_at,
                        registration_opens_at=template.registration_opens_at + day_delta,
                        confirmation_deadline=template.confirmation_deadline + day_delta,
                        team_count=template.team_count,
                        status="scheduled",
                        recurrence_group=group,
                        recurrence_rule=rule,
                        shifts=list(template.shifts),
                    )
                    db.session.add(occurrence)
                    created.append(occurrence)
                    existing_dates.add(cursor)
                cursor += timedelta(days=1)
        if created:
            db.session.commit()
        return created

    @staticmethod
    def recurrence_summaries(place_id=None):
        place_id = place_id or current_place().id
        groups = db.session.query(Event.recurrence_group).filter(
            Event.place_id == place_id,
            Event.recurrence_group.isnot(None),
            Event.status != "deleted",
        ).distinct().all()
        summaries = []
        for (group,) in groups:
            occurrences = Event.query.filter(
                Event.place_id == place_id,
                Event.recurrence_group == group,
                Event.status != "deleted",
            ).order_by(Event.game_date, Event.starts_at).all()
            if not occurrences:
                continue
            template = occurrences[0]
            upcoming = next((item for item in occurrences if item.game_date >= date.today()), template)
            rule = template.recurrence_rule or {}
            summaries.append({
                "recurrence_group": group,
                "representative_event_id": upcoming.id,
                "title": template.title,
                "starts_at": template.starts_at.isoformat(),
                "start_date": rule.get("start_date") or template.game_date.isoformat(),
                "next_date": upcoming.game_date.isoformat(),
                "materialized_until": occurrences[-1].game_date.isoformat(),
                "weekdays": rule.get("weekdays", []),
                "unlimited": bool(rule.get("unlimited")),
                "occurrences_created": len(occurrences),
                "shifts": [shift.to_dict() for shift in template.shifts],
            })
        return summaries

    @staticmethod
    def list():
        from flask import request
        place = current_place()
        EventService.materialize_recurring(place.id)
        query = Event.query.filter_by(place_id=place.id)
        if request.args.get("status"):
            query = query.filter_by(status=request.args["status"])
        else:
            query = query.filter(Event.status != "deleted")
        if request.args.get("from"):
            query = query.filter(Event.game_date >= parse_date(request.args["from"], "from"))
        result = paginate(query.order_by(Event.game_date.desc(), Event.starts_at), event_dict)
        result["recurrences"] = EventService.recurrence_summaries(place.id)
        return result

    @staticmethod
    def remove(event, scope="single"):
        if scope not in {"single", "recurrence"}:
            raise ApiError("Escopo de remoção inválido.", 422)
        place = current_place()
        if event.place_id != place.id:
            raise ApiError("Evento não encontrado neste local.", 404)
        query = Event.query.filter_by(id=event.id, place_id=place.id)
        if scope == "recurrence" and event.recurrence_group:
            query = Event.query.filter_by(recurrence_group=event.recurrence_group, place_id=place.id)
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
        place = current_place()
        if event and event.place_id != place.id:
            raise ApiError("Evento não encontrado neste local.", 404)
        shifts = Shift.query.filter(
            Shift.id.in_(data["shift_ids"]), Shift.active.is_(True), Shift.place_id == place.id
        ).all()
        if len(shifts) != len(set(data["shift_ids"])):
            raise ApiError("Um ou mais turnos são inválidos ou inativos.", 422)
        deadline = data.get("confirmation_deadline")
        if deadline:
            deadline = parse_datetime(deadline, "confirmation_deadline")
        else:
            deadline = datetime.combine(game_date, starts_at, tzinfo=timezone.utc) - timedelta(
                days=int(settings["confirmation_deadline_days"])
            )
        event = event or Event(place_id=place.id)
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
        dates = list(data.get("dates", []))
        unlimited = not bool(dates)
        if not dates:
            require_fields(data, "start_date", "weekdays")
            cursor = parse_date(data["start_date"], "start_date")
            weekdays = {int(day) for day in data["weekdays"]}
            if not weekdays or any(day < 0 or day > 6 for day in weekdays):
                raise ApiError("Selecione ao menos um dia válido para a recorrência.", 422)
            horizon = max(date.today(), cursor) + timedelta(days=RECURRENCE_HORIZON_DAYS)
            while cursor <= horizon:
                if cursor.weekday() in weekdays:
                    dates.append(cursor.isoformat())
                cursor += timedelta(days=1)
            if not dates:
                raise ApiError("A recorrência não possui datas dentro da janela de programação.", 422)
        group = str(uuid.uuid4())
        created = []
        base_game_date = parse_date(data.get("game_date") or data.get("start_date") or dates[0], "game_date")
        base_registration_opens = parse_datetime(data["registration_opens_at"], "registration_opens_at")
        base_confirmation_deadline = parse_datetime(
            data["confirmation_deadline"], "confirmation_deadline"
        ) if data.get("confirmation_deadline") else None
        rule = {
            "frequency": "weekly",
            "weekdays": sorted(int(day) for day in data.get("weekdays", [])),
            "start_date": data.get("start_date") or dates[0],
            "unlimited": unlimited,
        }
        try:
            for raw_date in dates:
                occurrence_date = parse_date(raw_date, "game_date")
                day_delta = timedelta(days=(occurrence_date - base_game_date).days)
                payload = {
                    **data,
                    "game_date": occurrence_date.isoformat(),
                    "registration_opens_at": (base_registration_opens + day_delta).isoformat(),
                }
                if base_confirmation_deadline:
                    payload["confirmation_deadline"] = (base_confirmation_deadline + day_delta).isoformat()
                payload.pop("dates", None)
                event_data = EventService.save(payload)
                event = db.session.get(Event, event_data["id"])
                event.recurrence_group = group
                event.recurrence_rule = rule
                created.append(event)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return [event_dict(item) for item in created]


def active_blacklist(player_id, place_id=None):
    place_id = place_id or current_place().id
    return BlacklistEntry.query.filter_by(
        place_id=place_id, player_id=player_id, removed_at=None
    ).order_by(BlacklistEntry.included_at.desc()).first()


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
    def _rebalance_pending(event, shift_id, position_id):
        position = db.session.get(Position, position_id)
        capacity = position.required_per_team * event.team_count
        occupied = Registration.query.filter(
            Registration.event_id == event.id,
            Registration.shift_id == shift_id,
            Registration.primary_position_id == position_id,
            Registration.status.in_(["confirmed", "present"]),
        ).count()
        contenders = Registration.query.filter(
            Registration.event_id == event.id,
            Registration.shift_id == shift_id,
            Registration.primary_position_id == position_id,
            Registration.status.in_(["pending_confirmation", "waitlist"]),
        ).order_by(Registration.priority_level, Registration.created_at, Registration.id).all()
        eligible = [item for item in contenders if not active_blacklist(item.player_id, event.place_id)]
        available = max(capacity - occupied, 0)
        selected = {item.id for item in eligible[:available]}
        for item in contenders:
            item.status = "pending_confirmation" if item.id in selected else "waitlist"

    @staticmethod
    def create(data):
        require_fields(data, "event_id", "player_id", "shift_id", "primary_position_id")
        event = db.session.get(Event, data["event_id"])
        player = db.session.get(Player, data["player_id"])
        shift = db.session.get(Shift, data["shift_id"])
        primary = db.session.get(Position, data["primary_position_id"])
        secondary = db.session.get(Position, data.get("secondary_position_id")) if data.get("secondary_position_id") else None
        place = current_place()
        membership = PlacePlayer.query.filter_by(place_id=place.id, player_id=data["player_id"]).first()
        if not event or event.place_id != place.id or event.status != "scheduled":
            raise ApiError("Evento indisponível para inscrição.", 422)
        opens_at = event.registration_opens_at
        if opens_at.tzinfo is None:
            opens_at = opens_at.replace(tzinfo=timezone.utc)
        if utcnow() < opens_at:
            raise ApiError("As inscrições ainda não foram liberadas.", 422)
        if not player or not membership or not membership.active:
            raise ApiError("Jogador inválido ou inativo.", 422)
        if not shift or shift not in event.shifts:
            raise ApiError("Turno não disponível neste evento.", 422)
        if not primary or not primary.active or (secondary and not secondary.active):
            raise ApiError("Posição inválida ou inativa.", 422)
        blocked = active_blacklist(player.id, place.id)
        is_guest = as_bool(data.get("is_guest", membership.is_guest))
        registration = Registration(
            event=event, player=player, shift=shift, primary_position=primary, secondary_position=secondary,
            status="waitlist" if blocked else "pending_confirmation",
            is_guest=is_guest,
            priority_level=membership.priority_for(is_guest),
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
            db.session.flush()
            RegistrationService._rebalance_pending(event, shift.id, primary.id)
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
        if not registration or registration.event.place_id != current_place().id:
            raise ApiError("Link de confirmação inválido.", 404)
        if registration.status == "cancelled":
            raise ApiError("Esta inscrição foi cancelada.", 409)
        registration.email_confirmed_at = registration.email_confirmed_at or utcnow()
        registration.confirmed_at = utcnow()
        if active_blacklist(registration.player_id, registration.event.place_id):
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
                RegistrationService._rebalance_pending(
                    registration.event, registration.shift_id, registration.primary_position_id
                )
            else:
                registration.status = "waitlist"
        db.session.commit()
        return registration_dict(registration)

    @staticmethod
    def update_status(registration, status, reason=None):
        if status not in REGISTRATION_STATUSES:
            raise ApiError("Status de inscrição inválido.", 422)
        previous_status = registration.status
        attendance_statuses = {"present"}
        absence_statuses = {"justified_absence", "unjustified_absence"}
        membership = PlacePlayer.query.filter_by(
            place_id=registration.event.place_id, player_id=registration.player_id
        ).one()
        if previous_status in attendance_statuses:
            membership.attendance_count = max((membership.attendance_count or 0) - 1, 0)
        if previous_status in absence_statuses:
            membership.absence_count = max((membership.absence_count or 0) - 1, 0)
        registration.status = status
        if status in attendance_statuses:
            membership.attendance_count = (membership.attendance_count or 0) + 1
        if status in absence_statuses:
            membership.absence_count = (membership.absence_count or 0) + 1
        registration.absence_reason = str(reason).strip() if reason else None
        if status == "confirmed":
            registration.confirmed_at = utcnow()
        if status == "unjustified_absence" and not active_blacklist(registration.player_id, registration.event.place_id):
            db.session.add(BlacklistEntry(
                place_id=registration.event.place_id,
                player_id=registration.player_id,
                reason=registration.absence_reason or "Falta injustificada",
                origin="unjustified_absence",
                source_event_id=registration.event_id,
            ))
        db.session.commit()
        return registration_dict(registration, admin=True)

    @staticmethod
    def update_admin(registration, data):
        editable_fields = {"priority_level", "primary_position_id"}
        if not editable_fields.intersection(data):
            raise ApiError("Informe a prioridade ou a posição que deseja alterar.", 422)

        priority_level = registration.priority_level
        if "priority_level" in data:
            try:
                priority_level = int(data["priority_level"])
            except (TypeError, ValueError):
                raise ApiError("A prioridade deve ser um número entre 1 e 3.", 422) from None
            if priority_level not in {1, 2, 3}:
                raise ApiError("A prioridade deve estar entre 1 e 3.", 422)

        old_position_id = registration.primary_position_id
        target_position = registration.primary_position
        team_member = TeamMember.query.filter_by(registration_id=registration.id).first()
        if "primary_position_id" in data:
            try:
                position_id = int(data["primary_position_id"])
            except (TypeError, ValueError):
                raise ApiError("Posição inválida ou inativa.", 422) from None
            target_position = db.session.get(Position, position_id)
            if not target_position or not target_position.active:
                raise ApiError("Posição inválida ou inativa.", 422)

            if position_id != old_position_id and registration.status in {
                "confirmed", "pending_confirmation", "present"
            } and not position_has_capacity(
                registration.event, registration.shift_id, position_id, registration.id
            ):
                occupants = Registration.query.filter(
                    Registration.event_id == registration.event_id,
                    Registration.shift_id == registration.shift_id,
                    Registration.primary_position_id == position_id,
                    Registration.id != registration.id,
                    Registration.status.in_(["confirmed", "pending_confirmation", "present"]),
                ).order_by(Registration.priority_level, Registration.snapshot_name).all()
                capacity = target_position.required_per_team * registration.event.team_count
                names = [item.snapshot_name for item in occupants]
                raise ApiError(
                    f"{target_position.name} já atingiu o limite de {capacity} vaga(s). "
                    f"Troque primeiro a posição de {names[0] if names else 'outro jogador'} para liberar a vaga.",
                    409,
                    {
                        "requires_position_change": True,
                        "position_id": position_id,
                        "position": target_position.name,
                        "capacity": capacity,
                        "occupants": [{"id": item.id, "name": item.snapshot_name} for item in occupants],
                    },
                )

            if team_member and position_id != team_member.position_id:
                team_occupants = TeamMember.query.filter(
                    TeamMember.team_id == team_member.team_id,
                    TeamMember.position_id == position_id,
                    TeamMember.id != team_member.id,
                ).order_by(TeamMember.id).all()
                if len(team_occupants) >= target_position.required_per_team:
                    names = [item.registration.snapshot_name for item in team_occupants]
                    raise ApiError(
                        f"{target_position.name} já está preenchida neste time. "
                        f"Troque primeiro a posição de {names[0] if names else 'outro jogador'} para salvar.",
                        409,
                        {
                            "requires_position_change": True,
                            "position_id": position_id,
                            "position": target_position.name,
                            "occupants": [
                                {"id": item.registration_id, "name": item.registration.snapshot_name}
                                for item in team_occupants
                            ],
                        },
                    )

        registration.priority_level = priority_level
        if target_position.id != old_position_id:
            registration.primary_position = target_position
            if team_member:
                team_member.position = target_position
                registration.assigned_position = target_position
            elif registration.assigned_position_id:
                registration.assigned_position_id = None
            RegistrationService._rebalance_pending(
                registration.event, registration.shift_id, old_position_id
            )
        RegistrationService._rebalance_pending(
            registration.event, registration.shift_id, target_position.id
        )
        db.session.commit()
        return registration_dict(registration, admin=True)


class BlacklistService:
    @staticmethod
    def list():
        from flask import request
        query = BlacklistEntry.query.filter_by(place_id=current_place().id)
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
        place = current_place()
        if not PlacePlayer.query.filter_by(place_id=place.id, player_id=data["player_id"]).first():
            raise ApiError("Jogador não encontrado.", 404)
        if active_blacklist(data["player_id"]):
            raise ApiError("Jogador já está na Lista Negra.", 409)
        entry = BlacklistEntry(place_id=place.id, player_id=data["player_id"], reason=str(data["reason"]).strip(),
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
