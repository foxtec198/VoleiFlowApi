from sqlalchemy import func, or_

from models import PlacePlayer, PlaceSetting, Player, Position, Shift
from services.common import as_bool, paginate, parse_date, parse_time, score
from services.places import current_place
from utils.db import db
from utils.errors import ApiError, require_fields

SKILL_FIELDS = ("knowledge_level", "reception", "setting", "blocking", "serving", "attack", "defense")


def position_dict(position):
    return position.to_dict()


def player_dict(player, private=True, membership=None, include_email=False):
    membership = membership or PlacePlayer.query.filter_by(
        place_id=current_place().id, player_id=player.id
    ).first()
    data = player.to_dict()
    if membership:
        data.update({
            "active": membership.active,
            "is_guest": membership.is_guest,
            "invited_by": membership.invited_by,
            "attendance_count": membership.attendance_count,
            "absence_count": membership.absence_count,
            "priority_level": membership.priority_level,
            "membership": "guest" if membership.is_guest else "member",
            "place_id": membership.place_id,
        })
    data["primary_position"] = player.primary_position.name
    data["secondary_position"] = player.secondary_position.name if player.secondary_position else None
    if not private:
        for field in ("email", "phone", "birth_date", "invited_by", "attendance_count", "absence_count", *SKILL_FIELDS):
            data.pop(field, None)
        if include_email:
            data["email"] = player.email
    return data


class PositionService:
    @staticmethod
    def list():
        query = Position.query.order_by(Position.name)
        if "active" in __import__("flask").request.args:
            query = query.filter_by(active=as_bool(__import__("flask").request.args["active"]))
        return paginate(query)

    @staticmethod
    def save(data, position=None):
        require_fields(data, "name", "required_per_team")
        qty = int(data["required_per_team"])
        if qty < 0 or qty > 12:
            raise ApiError("Quantidade por time deve estar entre 0 e 12.", 422)
        duplicate = Position.query.filter(func.lower(Position.name) == str(data["name"]).strip().lower())
        if position:
            duplicate = duplicate.filter(Position.id != position.id)
        if duplicate.first():
            raise ApiError("Já existe uma posição com esse nome.", 409)
        position = position or Position()
        position.name = str(data["name"]).strip()
        position.required_per_team = qty
        position.active = as_bool(data.get("active", True))
        db.session.add(position)
        db.session.commit()
        return position.to_dict()


class PlayerService:
    @staticmethod
    def list(public=False, include_email=False):
        from flask import request
        place = current_place()
        query = Player.query.join(PlacePlayer, PlacePlayer.player_id == Player.id).filter(PlacePlayer.place_id == place.id)
        if public:
            query = query.filter(PlacePlayer.active.is_(True))
        elif "active" in request.args:
            query = query.filter(PlacePlayer.active.is_(as_bool(request.args["active"])))
        search = request.args.get("search", "").strip()
        if search:
            query = query.filter(or_(Player.name.ilike(f"%{search}%"), Player.email.ilike(f"%{search}%")))
        return paginate(query.order_by(Player.name), lambda item: player_dict(
            item, private=not public,
            membership=PlacePlayer.query.filter_by(place_id=place.id, player_id=item.id).one(),
            include_email=include_email,
        ))

    @staticmethod
    def save(data, player=None):
        require_fields(data, "name", "email", "phone", "primary_position_id")
        place = current_place()
        email = str(data["email"]).strip().lower()
        duplicate = Player.query.filter(func.lower(Player.email) == email)
        if player:
            duplicate = duplicate.filter(Player.id != player.id)
        existing = duplicate.first()
        if player and existing:
            raise ApiError("E-mail já cadastrado.", 409)
        if existing and PlacePlayer.query.filter_by(place_id=place.id, player_id=existing.id).first():
            raise ApiError("E-mail já cadastrado.", 409)
        primary = db.session.get(Position, data["primary_position_id"])
        secondary = db.session.get(Position, data.get("secondary_position_id")) if data.get("secondary_position_id") else None
        if not primary or not primary.active or (secondary and not secondary.active):
            raise ApiError("Posição inválida ou inativa.", 422)
        player = player or existing or Player()
        membership = PlacePlayer.query.filter_by(place_id=place.id, player_id=player.id).first() if player.id else None
        membership = membership or PlacePlayer(place_id=place.id, player=player)
        player.name = str(data["name"]).strip()
        player.email = email
        player.phone = str(data["phone"]).strip()
        membership.active = as_bool(data.get("active", membership.active if membership.id else True))
        membership.is_guest = as_bool(data.get("is_guest", membership.is_guest or False))
        membership.invited_by = str(data.get("invited_by", "")).strip() or None
        player.active = True
        if data.get("birth_date"):
            player.birth_date = parse_date(data["birth_date"], "birth_date")
        elif "birth_date" in data:
            player.birth_date = None
        player.primary_position = primary
        player.secondary_position = secondary
        for field in SKILL_FIELDS:
            setattr(player, field, score(data.get(field, 5), field))
        db.session.add(player)
        db.session.add(membership)
        db.session.commit()
        return player_dict(player, membership=membership)

    @staticmethod
    def set_active(player, active):
        membership = PlacePlayer.query.filter_by(place_id=current_place().id, player_id=player.id).first()
        if not membership:
            raise ApiError("Jogador não encontrado neste local.", 404)
        membership.active = as_bool(active)
        db.session.commit()
        return player_dict(player, membership=membership)


class ShiftService:
    @staticmethod
    def list(active_only=False):
        query = Shift.query.filter_by(place_id=current_place().id)
        if active_only:
            query = query.filter_by(active=True)
        return paginate(query.order_by(Shift.starts_at))

    @staticmethod
    def save(data, shift=None):
        require_fields(data, "name", "starts_at", "ends_at")
        starts_at = parse_time(data["starts_at"], "starts_at")
        ends_at = parse_time(data["ends_at"], "ends_at")
        if ends_at <= starts_at:
            raise ApiError("O fim do turno deve ser posterior ao início.", 422)
        place = current_place()
        if shift and shift.place_id != place.id:
            raise ApiError("Turno não encontrado neste local.", 404)
        shift = shift or Shift(place_id=place.id)
        shift.name = str(data["name"]).strip()
        shift.starts_at = starts_at
        shift.ends_at = ends_at
        shift.active = as_bool(data.get("active", True))
        db.session.add(shift)
        db.session.commit()
        return shift.to_dict()


def get_settings():
    defaults = {"max_teams_per_event": 3, "confirmation_deadline_days": 1,
                "admin_whatsapp": "", "imbalance_threshold": 1.5}
    stored = {item.key: item.value for item in PlaceSetting.query.filter_by(place_id=current_place().id)}
    return {**defaults, **stored}


def update_settings(data):
    allowed = {"max_teams_per_event", "confirmation_deadline_days", "admin_whatsapp", "imbalance_threshold"}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key in {"max_teams_per_event", "confirmation_deadline_days"}:
            value = int(value)
            if value < 0 or (key == "max_teams_per_event" and value < 1):
                raise ApiError(f"Valor inválido para {key}.", 422)
        place = current_place()
        row = PlaceSetting.query.filter_by(place_id=place.id, key=key).first() or PlaceSetting(place_id=place.id, key=key)
        row.value = value
        db.session.add(row)
    db.session.commit()
    return get_settings()
