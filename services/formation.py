from collections import defaultdict

from models import Event, Position, Registration, Team, TeamMember
from services.events import registration_dict
from utils.db import db
from utils.errors import ApiError

METRICS = {
    "overall": None,
    "reception": "snapshot_reception",
    "setting": "snapshot_setting",
    "blocking": "snapshot_blocking",
    "serving": "snapshot_serving",
    "attack": "snapshot_attack",
    "defense": "snapshot_defense",
}


def member_score(member):
    return member.registration.overall


def team_metrics(team):
    registrations = [member.registration for member in team.members]
    metrics = {}
    for label, field in METRICS.items():
        values = [registration.overall if field is None else getattr(registration, field)
                  for registration in registrations]
        metrics[label] = round(sum(values) / len(values), 2) if values else 0
    return metrics


def team_dict(team, include_private=False):
    members = []
    for member in sorted(team.members, key=lambda item: (item.position.name, item.registration.snapshot_name)):
        registration = registration_dict(member.registration, admin=include_private)
        members.append({"id": member.id, "position_id": member.position_id,
                        "position": member.position.name, "registration": registration})
    return {**team.to_dict(), "members": members, "metrics": team_metrics(team)}


def formation_payload(event_id, shift_id):
    teams = Team.query.filter_by(event_id=event_id, shift_id=shift_id).order_by(Team.number).all()
    metrics = [team_metrics(team) for team in teams]
    differences = {}
    for label in METRICS:
        values = [item[label] for item in metrics]
        differences[label] = round(max(values) - min(values), 2) if values else 0
    assigned_ids = {member.registration_id for team in teams for member in team.members}
    waitlist = Registration.query.filter(
        Registration.event_id == event_id,
        Registration.shift_id == shift_id,
        ~Registration.id.in_(assigned_ids) if assigned_ids else True,
        Registration.status != "cancelled",
    ).order_by(Registration.created_at).all()
    return {
        "event_id": event_id,
        "shift_id": shift_id,
        "teams": [team_dict(team, include_private=True) for team in teams],
        "differences": differences,
        "waitlist": [registration_dict(item) for item in waitlist],
    }


class FormationService:
    @staticmethod
    def generate(event_id, shift_id):
        event = db.session.get(Event, event_id)
        if not event or shift_id not in {shift.id for shift in event.shifts}:
            raise ApiError("Evento ou turno inválido.", 404)
        candidates = Registration.query.filter(
            Registration.event_id == event_id,
            Registration.shift_id == shift_id,
            Registration.status.in_(["confirmed", "pending_confirmation", "present"]),
        ).all()
        candidates.sort(key=lambda item: (
            0 if item.status in {"confirmed", "present"} else 1,
            -item.overall,
            item.created_at,
            item.id,
        ))
        positions = Position.query.filter(Position.active.is_(True), Position.required_per_team > 0).order_by(Position.id).all()
        existing_team_ids = [item.id for item in Team.query.filter_by(event_id=event_id, shift_id=shift_id)]
        if existing_team_ids:
            TeamMember.query.filter(TeamMember.team_id.in_(existing_team_ids)).delete(synchronize_session=False)
            Team.query.filter(Team.id.in_(existing_team_ids)).delete(synchronize_session=False)
        db.session.flush()
        teams = [Team(event_id=event_id, shift_id=shift_id, number=number, name=f"Time {number}")
                 for number in range(1, event.team_count + 1)]
        db.session.add_all(teams)
        db.session.flush()
        totals = defaultdict(float)
        used = set()
        missing = []
        for position in positions:
            for _round in range(position.required_per_team):
                for team in sorted(teams, key=lambda item: (totals[item.id], item.number)):
                    qualified = [candidate for candidate in candidates if candidate.id not in used and
                                 (candidate.primary_position_id == position.id or candidate.secondary_position_id == position.id)]
                    if not qualified:
                        missing.append({"team_id": team.id, "team": team.name, "position_id": position.id,
                                        "position": position.name, "missing": 1})
                        continue
                    candidate = min(qualified, key=lambda item: (
                        0 if item.primary_position_id == position.id else 1,
                        0 if item.status in {"confirmed", "present"} else 1,
                        abs((totals[team.id] + item.overall) - min(totals.values() or [0])),
                        -item.overall,
                        item.id,
                    ))
                    used.add(candidate.id)
                    candidate.assigned_position_id = position.id
                    db.session.add(TeamMember(team=team, registration=candidate, position=position))
                    totals[team.id] += candidate.overall
        db.session.flush()
        for team in teams:
            team.balance_score = team_metrics(team)["overall"]
        db.session.commit()
        payload = formation_payload(event_id, shift_id)
        collapsed = {}
        for item in missing:
            key = (item["team_id"], item["position_id"])
            if key in collapsed:
                collapsed[key]["missing"] += 1
            else:
                collapsed[key] = item
        payload["missing"] = list(collapsed.values())
        return payload

    @staticmethod
    def move(member, target_team_id, position_id):
        target = db.session.get(Team, target_team_id)
        position = db.session.get(Position, position_id)
        source = member.team
        source_position = member.position
        if not target or target.event_id != member.team.event_id or target.shift_id != member.team.shift_id:
            raise ApiError("O time de destino deve pertencer ao mesmo evento e turno.", 422)
        if not position or not position.active:
            raise ApiError("Posição de destino inválida.", 422)
        occupants = TeamMember.query.filter_by(team_id=target.id, position_id=position.id).filter(TeamMember.id != member.id).all()
        if len(occupants) >= position.required_per_team:
            if source.id == target.id:
                raise ApiError("O jogador já ocupa essa vaga.", 409)
            swap = sorted(occupants, key=lambda item: (item.registration.overall, item.id))[0]
            swap.team = source
            swap.position = source_position
            swap.registration.assigned_position_id = source_position.id
        member.team = target
        member.position = position
        member.registration.assigned_position_id = position.id
        db.session.flush()
        for team in Team.query.filter_by(event_id=target.event_id, shift_id=target.shift_id):
            team.balance_score = team_metrics(team)["overall"]
        db.session.commit()
        return formation_payload(target.event_id, target.shift_id)

    @staticmethod
    def whatsapp(event_id, shift_id):
        event = db.session.get(Event, event_id)
        payload = formation_payload(event_id, shift_id)
        if not event or not payload["teams"]:
            raise ApiError("Formação ainda não disponível.", 404)
        shift = next((item for item in event.shifts if item.id == shift_id), None)
        lines = [f"🏐 {event.title}", f"📅 {event.game_date:%d/%m/%Y} às {event.starts_at:%H:%M}",
                 f"🕐 Turno: {shift.name if shift else '-'}", ""]
        for team in payload["teams"]:
            lines.append(f"*{team['name']}*")
            for member in team["members"]:
                lines.append(f"• {member['registration']['player_name']} — {member['position']}")
            lines.append("")
        if payload["waitlist"]:
            lines.append("*Lista de espera*")
            lines.extend(f"• {item['player_name']} — {item['primary_position']}" for item in payload["waitlist"])
        vacancies = []
        positions = Position.query.filter(Position.active.is_(True), Position.required_per_team > 0).all()
        for team in payload["teams"]:
            for position in positions:
                occupied = sum(member["position_id"] == position.id for member in team["members"])
                if occupied < position.required_per_team:
                    vacancies.append(f"• {team['name']}: {position.required_per_team - occupied} vaga(s) de {position.name}")
        if vacancies:
            lines.extend(["", "*Vagas disponíveis*", *vacancies])
        return {"text": "\n".join(lines).strip()}
