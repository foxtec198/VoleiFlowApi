from datetime import date, datetime, time, timezone

from flask import request

from utils.errors import ApiError


def utcnow():
    return datetime.now(timezone.utc)


def parse_date(value, field="date"):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field} deve estar no formato AAAA-MM-DD.", 422) from None


def parse_time(value, field="time"):
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field} deve estar no formato HH:MM.", 422) from None


def parse_datetime(value, field="datetime"):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ApiError(f"{field} deve ser uma data/hora ISO 8601.", 422) from None


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "sim"}


def paginate(query, serializer=lambda item: item.to_dict()):
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 25, type=int), 1), 100)
    result = query.paginate(page=page, per_page=per_page, error_out=False)
    return {
        "items": [serializer(item) for item in result.items],
        "pagination": {"page": page, "per_page": per_page, "total": result.total, "pages": result.pages},
    }


def score(value, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field} deve ser um número inteiro entre 0 e 10.", 422) from None
    if not 0 <= number <= 10:
        raise ApiError(f"{field} deve estar entre 0 e 10.", 422)
    return number
