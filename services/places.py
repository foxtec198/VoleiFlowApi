import re

from flask import current_app, g, request

from models import Place
from utils.db import db
from utils.errors import ApiError


def place_dict(place):
    return place.to_dict()


def current_place():
    if getattr(g, "place", None) is not None:
        return g.place
    slug = request.headers.get("X-Place-Slug", "").strip().lower()
    slug = slug or current_app.config.get("DEFAULT_PLACE_SLUG", "nilo")
    place = Place.query.filter(db.func.lower(Place.slug) == slug, Place.active.is_(True)).first()
    if not place:
        raise ApiError("Local não encontrado ou inativo.", 404)
    g.place = place
    return place


class PlaceService:
    @staticmethod
    def list():
        return {"items": [place_dict(item) for item in Place.query.filter_by(active=True).order_by(Place.name)]}

    @staticmethod
    def update(place, data):
        for field in ("name", "address", "neighborhood", "city", "state", "postal_code", "maps_url"):
            if field in data:
                value = str(data.get(field, "")).strip() or None
                if field == "name" and not value:
                    raise ApiError("O nome do local é obrigatório.", 422)
                if field == "state" and value:
                    value = value.upper()[:2]
                setattr(place, field, value)
        if "slug" in data:
            slug = str(data["slug"]).strip().lower()
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                raise ApiError("Use apenas letras minúsculas, números e hífens na rota.", 422)
            duplicate = Place.query.filter(db.func.lower(Place.slug) == slug, Place.id != place.id).first()
            if duplicate:
                raise ApiError("Esta rota já está em uso.", 409)
            place.slug = slug
        db.session.commit()
        return place_dict(place)
