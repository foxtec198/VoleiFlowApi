from flask import Blueprint, request

from models import BlacklistEntry, Event, OfflineOperation, PlacePlayer, Player, Position, Registration, Shift, TeamMember
from services.auth import AuthService, admin_dict
from services.catalog import PlayerService, PositionService, ShiftService, get_settings, update_settings
from services.events import BlacklistService, EventService, RegistrationService, event_dict, registration_dict
from services.formation import FormationService, formation_payload
from services.places import PlaceService, current_place, place_dict
from services.common import parse_datetime, utcnow
from utils.auth import admin_required
from utils.db import db
from utils.errors import ApiError, require_fields
from utils.socekt import socketio

api_bp = Blueprint("api", __name__)


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Corpo JSON inválido.", 400)
    return data


def get_or_404(model, item_id, label="Recurso"):
    item = db.session.get(model, item_id)
    if not item:
        raise ApiError(f"{label} não encontrado.", 404)
    return item


def get_player_or_404(item_id):
    membership = PlacePlayer.query.filter_by(place_id=current_place().id, player_id=item_id).first()
    if not membership:
        raise ApiError("Jogador não encontrado.", 404)
    return membership.player


def get_place_item_or_404(model, item_id, label):
    item = db.session.get(model, item_id)
    if not item or item.place_id != current_place().id:
        raise ApiError(f"{label} não encontrado.", 404)
    return item


def get_registration_or_404(item_id):
    item = db.session.get(Registration, item_id)
    if not item or item.event.place_id != current_place().id:
        raise ApiError("Inscrição não encontrada.", 404)
    return item


@api_bp.post("/auth/login")
def admin_login():
    return AuthService.login(body())


@api_bp.get("/auth/me")
@admin_required
def admin_me():
    from flask import g
    return admin_dict(g.admin)


@api_bp.post("/auth/logout")
@admin_required
def admin_logout():
    return AuthService.logout()


@api_bp.get("/public/bootstrap")
def public_bootstrap():
    place = current_place()
    EventService.materialize_recurring(place.id)
    # A tela pública de inscrição só recebe ocorrências cuja abertura já chegou.
    # O painel administrativo usa /events e, por isso, continua enxergando toda
    # a agenda recorrente, inclusive jogos com inscrição ainda fechada.
    upcoming = Event.query.filter(
        Event.place_id == place.id,
        Event.status == "scheduled",
        Event.registration_opens_at <= utcnow(),
    ).order_by(Event.game_date, Event.starts_at).limit(20).all()
    return {
        "place": place_dict(place),
        "events": [event_dict(item) for item in upcoming],
        "positions": [item.to_dict() for item in Position.query.filter_by(active=True).order_by(Position.name)],
        "players": PlayerService.list(public=True, include_email=True),
        "settings": {"admin_whatsapp": get_settings()["admin_whatsapp"]},
    }


@api_bp.get("/places")
def places():
    return PlaceService.list()


@api_bp.get("/public/players")
def public_players():
    return PlayerService.list(public=True, include_email=True)


@api_bp.get("/place")
def place():
    return place_dict(current_place())


@api_bp.patch("/place")
@admin_required
def update_place():
    return PlaceService.update(current_place(), body())


@api_bp.get("/positions")
def positions():
    return PositionService.list()


@api_bp.post("/positions")
@admin_required
def create_position():
    return PositionService.save(body()), 201


@api_bp.patch("/positions/<int:item_id>")
@admin_required
def update_position(item_id):
    return PositionService.save(body(), get_or_404(Position, item_id, "Posição"))


@api_bp.delete("/positions/<int:item_id>")
@admin_required
def deactivate_position(item_id):
    item = get_or_404(Position, item_id, "Posição")
    item.active = False
    db.session.commit()
    return item.to_dict()


@api_bp.get("/players")
@admin_required
def players():
    return PlayerService.list()


@api_bp.post("/players")
@admin_required
def create_player():
    return PlayerService.save(body()), 201


@api_bp.get("/players/<int:item_id>")
@admin_required
def player(item_id):
    from services.catalog import player_dict
    return player_dict(get_player_or_404(item_id))


@api_bp.patch("/players/<int:item_id>")
@admin_required
def update_player(item_id):
    return PlayerService.save(body(), get_player_or_404(item_id))


@api_bp.patch("/players/<int:item_id>/active")
@admin_required
def activate_player(item_id):
    data = body()
    require_fields(data, "active")
    return PlayerService.set_active(get_player_or_404(item_id), data["active"])


@api_bp.delete("/players/<int:item_id>")
@admin_required
def delete_player(item_id):
    return PlayerService.set_active(get_player_or_404(item_id), False)


@api_bp.get("/shifts")
def shifts():
    return ShiftService.list(active_only=request.args.get("public") == "true")


@api_bp.post("/shifts")
@admin_required
def create_shift():
    return ShiftService.save(body()), 201


@api_bp.patch("/shifts/<int:item_id>")
@admin_required
def update_shift(item_id):
    return ShiftService.save(body(), get_place_item_or_404(Shift, item_id, "Turno"))


@api_bp.delete("/shifts/<int:item_id>")
@admin_required
def delete_shift(item_id):
    item = get_place_item_or_404(Shift, item_id, "Turno")
    item.active = False
    db.session.commit()
    return item.to_dict()


@api_bp.get("/settings")
@admin_required
def settings():
    return get_settings()


@api_bp.patch("/settings")
@admin_required
def patch_settings():
    return update_settings(body())


@api_bp.get("/events")
def events():
    return EventService.list()


@api_bp.post("/events")
@admin_required
def create_event():
    return EventService.save(body()), 201


@api_bp.post("/events/recurring")
@admin_required
def recurring_events():
    return {"items": EventService.create_recurring(body())}, 201


@api_bp.get("/events/<int:item_id>")
@admin_required
def event(item_id):
    return event_dict(get_place_item_or_404(Event, item_id, "Evento"), detailed=True)


@api_bp.patch("/events/<int:item_id>")
@admin_required
def update_event(item_id):
    return EventService.save(body(), get_place_item_or_404(Event, item_id, "Evento"))


@api_bp.delete("/events/<int:item_id>")
@admin_required
def cancel_event(item_id):
    return EventService.remove(
        get_place_item_or_404(Event, item_id, "Evento"),
        request.args.get("scope", "single"),
    )


@api_bp.post("/registrations")
def create_registration():
    result = RegistrationService.create(body())
    socketio.emit("registration:changed", {"event_id": result["event_id"]})
    return result, 201


@api_bp.get("/registrations/confirm/<token>")
def confirm_registration(token):
    result = RegistrationService.confirm(token)
    socketio.emit("registration:changed", {"event_id": result["event_id"]})
    return result


@api_bp.patch("/registrations/<int:item_id>/status")
@admin_required
def registration_status(item_id):
    data = body()
    require_fields(data, "status")
    result = RegistrationService.update_status(
        get_registration_or_404(item_id), data["status"], data.get("reason")
    )
    socketio.emit("registration:changed", {"event_id": result["event_id"]})
    return result


@api_bp.patch("/registrations/<int:item_id>/notes")
@admin_required
def registration_notes(item_id):
    item = get_registration_or_404(item_id)
    item.notes = str(body().get("notes", "")).strip() or None
    db.session.commit()
    return registration_dict(item, admin=True)


@api_bp.get("/players/<int:player_id>/events/<int:event_id>/situation")
def player_situation(player_id, event_id):
    get_player_or_404(player_id)
    get_place_item_or_404(Event, event_id, "Evento")
    registrations = Registration.query.filter_by(player_id=player_id, event_id=event_id).all()
    result = []
    formations = {}
    for item in registrations:
        row = registration_dict(item)
        member = TeamMember.query.filter_by(registration_id=item.id).first()
        row["team"] = member.team.name if member else None
        row["assigned_position"] = member.position.name if member else None
        result.append(row)
        payload = formation_payload(event_id, item.shift_id)
        if payload["teams"]:
            formations[payload["formation_shift_id"]] = payload
    return {"items": result, "formations": list(formations.values())}


@api_bp.get("/blacklist")
@admin_required
def blacklist():
    return BlacklistService.list()


@api_bp.post("/blacklist")
@admin_required
def add_blacklist():
    return BlacklistService.add(body()), 201


@api_bp.delete("/blacklist/<int:item_id>")
@admin_required
def remove_blacklist(item_id):
    return BlacklistService.remove(get_place_item_or_404(BlacklistEntry, item_id, "Bloqueio"), body().get("reason"))


@api_bp.post("/events/<int:event_id>/shifts/<int:shift_id>/formation")
@admin_required
def generate_formation(event_id, shift_id):
    result = FormationService.generate(event_id, shift_id)
    socketio.emit("formation:changed", {"event_id": event_id, "shift_id": shift_id})
    return result


@api_bp.get("/events/<int:event_id>/shifts/<int:shift_id>/formation")
def get_formation(event_id, shift_id):
    return formation_payload(event_id, shift_id)


@api_bp.patch("/team-members/<int:item_id>")
@admin_required
def move_team_member(item_id):
    data = body()
    require_fields(data, "team_id", "position_id")
    member = get_or_404(TeamMember, item_id, "Jogador escalado")
    if member.team.event_id not in {item.id for item in Event.query.filter_by(place_id=current_place().id)}:
        raise ApiError("Jogador escalado não encontrado.", 404)
    result = FormationService.move(member, data["team_id"], data["position_id"])
    socketio.emit("formation:changed", {"event_id": member.team.event_id, "shift_id": member.team.shift_id})
    return result


@api_bp.post("/team-members")
@admin_required
def add_team_member():
    data = body()
    require_fields(data, "registration_id", "team_id", "position_id")
    registration = get_registration_or_404(data["registration_id"])
    result = FormationService.add_from_waitlist(
        registration,
        data["team_id"],
        data["position_id"],
        data.get("replace_member_id"),
    )
    socketio.emit("formation:changed", {
        "event_id": registration.event_id,
        "shift_id": result["formation_shift_id"],
    })
    return result


@api_bp.get("/events/<int:event_id>/shifts/<int:shift_id>/whatsapp")
@admin_required
def whatsapp_text(event_id, shift_id):
    return FormationService.whatsapp(event_id, shift_id)


@api_bp.post("/offline/sync")
@admin_required
def sync_offline():
    operations = body().get("operations", [])
    if not isinstance(operations, list) or len(operations) > 100:
        raise ApiError("Envie uma lista com no máximo 100 operações.", 422)
    results = []
    for operation in operations:
        operation_id = str(operation.get("id", ""))
        if not operation_id:
            results.append({"status": "error", "error": "Operação sem identificador."})
            continue
        previous = db.session.get(OfflineOperation, operation_id)
        if previous:
            results.append(previous.response)
            continue
        try:
            payload = operation.get("payload", {})
            registration = get_registration_or_404(payload.get("registration_id"))
            base_updated_at = payload.get("base_updated_at")
            server_updated_at = registration.updated_at
            if server_updated_at and server_updated_at.tzinfo is None:
                from datetime import timezone
                server_updated_at = server_updated_at.replace(tzinfo=timezone.utc)
            if base_updated_at and server_updated_at and parse_datetime(base_updated_at) < server_updated_at:
                response = {"id": operation_id, "status": "conflict", "server": registration_dict(registration, admin=True)}
            elif operation.get("type") == "attendance":
                updated = RegistrationService.update_status(registration, payload.get("status"), payload.get("reason"))
                response = {"id": operation_id, "status": "synced", "data": updated}
            elif operation.get("type") == "notes":
                registration.notes = str(payload.get("notes", "")).strip() or None
                db.session.commit()
                response = {"id": operation_id, "status": "synced", "data": registration_dict(registration, admin=True)}
            else:
                response = {"id": operation_id, "status": "error", "error": "Tipo de operação não suportado."}
        except ApiError as error:
            db.session.rollback()
            response = {"id": operation_id, "status": "error", "error": error.message}
        db.session.add(OfflineOperation(operation_id=operation_id, operation_type=operation.get("type", "unknown"),
                                        payload=operation.get("payload", {}), response=response, processed_at=utcnow()))
        db.session.commit()
        results.append(response)
    return {"operations": results}
