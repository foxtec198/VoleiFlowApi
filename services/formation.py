from collections import defaultdict

from models import Event, Position, Registration, Team, TeamMember
from services.events import active_blacklist, registration_dict
from services.places import current_place
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


def shift_period_dict(shift):
    return {
        "id": shift.id,
        "name": shift.name,
        "starts_at": shift.starts_at.isoformat(),
        "ends_at": shift.ends_at.isoformat(),
    }


def linked_shifts(event, shift_id):
    selected = next((shift for shift in event.shifts if shift.id == shift_id), None)
    if not selected:
        raise ApiError("Evento ou turno inválido.", 404)
    connected = {selected.id}
    pending = [selected]
    while pending:
        current = pending.pop()
        for shift in event.shifts:
            overlaps = current.starts_at < shift.ends_at and shift.starts_at < current.ends_at
            if shift.id not in connected and overlaps:
                connected.add(shift.id)
                pending.append(shift)
    return sorted((shift for shift in event.shifts if shift.id in connected), key=lambda item: (item.starts_at, item.id))


def team_dict(team, include_private=False, periods_by_player=None):
    members = []
    for member in sorted(team.members, key=lambda item: (item.position.name, item.registration.snapshot_name)):
        registration = registration_dict(member.registration, admin=include_private)
        registration["selected_periods"] = (periods_by_player or {}).get(member.registration.player_id, [
            shift_period_dict(member.registration.shift)
        ])
        members.append({"id": member.id, "position_id": member.position_id,
                        "position": member.position.name, "registration": registration})
    return {**team.to_dict(), "members": members, "metrics": team_metrics(team)}


def formation_payload(event_id, shift_id):
    event = db.session.get(Event, event_id)
    if not event or event.place_id != current_place().id:
        raise ApiError("Evento ou turno inválido.", 404)
    group_shifts = linked_shifts(event, shift_id)
    group_ids = [shift.id for shift in group_shifts]
    formation_shift_id = group_shifts[0].id
    registrations = Registration.query.filter(
        Registration.event_id == event_id,
        Registration.shift_id.in_(group_ids),
        Registration.status != "cancelled",
    ).order_by(Registration.created_at, Registration.id).all()
    periods_by_player = defaultdict(list)
    for registration in registrations:
        period = shift_period_dict(registration.shift)
        if period not in periods_by_player[registration.player_id]:
            periods_by_player[registration.player_id].append(period)
    teams = Team.query.filter_by(event_id=event_id, shift_id=formation_shift_id).order_by(Team.number).all()
    metrics = [team_metrics(team) for team in teams]
    differences = {}
    for label in METRICS:
        values = [item[label] for item in metrics]
        differences[label] = round(max(values) - min(values), 2) if values else 0
    assigned_player_ids = {member.registration.player_id for team in teams for member in team.members}
    waitlist = []
    waiting_players = set()
    for registration in registrations:
        if registration.player_id in assigned_player_ids or registration.player_id in waiting_players:
            continue
        waiting_players.add(registration.player_id)
        row = registration_dict(registration)
        row["selected_periods"] = periods_by_player[registration.player_id]
        row["can_assign"] = registration.status in {
            "confirmed", "pending_confirmation", "present"
        } or (
            registration.status == "waitlist"
            and not active_blacklist(registration.player_id, event.place_id)
        )
        waitlist.append(row)
    return {
        "event_id": event_id,
        "shift_id": shift_id,
        "formation_shift_id": formation_shift_id,
        "linked_shifts": [shift_period_dict(shift) for shift in group_shifts],
        "teams": [team_dict(team, periods_by_player=periods_by_player) for team in teams],
        "positions": [position.to_dict() for position in Position.query.filter(
            Position.active.is_(True), Position.required_per_team > 0
        ).order_by(Position.name).all()],
        "differences": differences,
        "waitlist": waitlist,
    }


class FormationService:
    @staticmethod
    def generate(event_id, shift_id):
        event = db.session.get(Event, event_id)
        if not event or event.place_id != current_place().id:
            raise ApiError("Evento ou turno inválido.", 404)
        group_shifts = linked_shifts(event, shift_id)
        group_ids = [shift.id for shift in group_shifts]
        formation_shift_id = group_shifts[0].id
        candidates = Registration.query.filter(
            Registration.event_id == event_id,
            Registration.shift_id.in_(group_ids),
            Registration.status.in_(["confirmed", "pending_confirmation", "present"]),
        ).all()
        candidates.sort(key=lambda item: (
            0 if item.status in {"confirmed", "present"} else 1,
            item.priority_level,
            -item.overall,
            item.created_at,
            item.id,
        ))
        unique_candidates = {}
        for candidate in candidates:
            unique_candidates.setdefault(candidate.player_id, candidate)
        candidates = list(unique_candidates.values())
        positions = Position.query.filter(Position.active.is_(True), Position.required_per_team > 0).order_by(Position.id).all()
        existing_team_ids = [item.id for item in Team.query.filter(
            Team.event_id == event_id, Team.shift_id.in_(group_ids)
        )]
        if existing_team_ids:
            TeamMember.query.filter(TeamMember.team_id.in_(existing_team_ids)).delete(synchronize_session=False)
            Team.query.filter(Team.id.in_(existing_team_ids)).delete(synchronize_session=False)
        db.session.flush()
        teams = [Team(event_id=event_id, shift_id=formation_shift_id, number=number, name=f"Time {number}")
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
                        item.priority_level,
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
        if not position or not position.active or position.required_per_team <= 0:
            raise ApiError("Posição de destino inválida.", 422)
        allowed_positions = {
            member.registration.primary_position_id,
            member.registration.secondary_position_id,
        }
        if position.id not in allowed_positions:
            raise ApiError(
                "O jogador só pode atuar na posição principal ou secundária cadastrada.", 422
            )
        if source.id == target.id and source_position.id == position.id:
            return formation_payload(target.event_id, target.shift_id)
        occupants = TeamMember.query.filter_by(team_id=target.id, position_id=position.id).filter(TeamMember.id != member.id).all()
        if len(occupants) >= position.required_per_team:
            eligible_swaps = [item for item in occupants if source_position.id in {
                item.registration.primary_position_id,
                item.registration.secondary_position_id,
            }]
            if not eligible_swaps:
                raise ApiError(
                    f"{position.name} já atingiu o limite neste time. "
                    "Altere primeiro a posição de outro jogador para liberar a vaga.",
                    409,
                )
            swap = sorted(eligible_swaps, key=lambda item: (item.registration.overall, item.id))[0]
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
    def add_from_waitlist(registration, target_team_id, position_id, replace_member_id=None):
        target = db.session.get(Team, target_team_id)
        position = db.session.get(Position, position_id)
        if not target or target.event_id != registration.event_id:
            raise ApiError("O time de destino deve pertencer ao mesmo evento.", 422)
        event = db.session.get(Event, target.event_id)
        if not event or event.place_id != current_place().id:
            raise ApiError("Time de destino não encontrado.", 404)
        group_ids = [shift.id for shift in linked_shifts(event, target.shift_id)]
        if registration.shift_id not in group_ids:
            raise ApiError("O jogador não pertence aos turnos interligados deste time.", 422)
        if registration.status not in {"confirmed", "pending_confirmation", "present", "waitlist"}:
            raise ApiError("Somente jogadores disponíveis podem sair do banco para um time.", 409)
        if active_blacklist(registration.player_id, event.place_id):
            raise ApiError("Jogador bloqueado não pode ser escalado manualmente.", 409)
        if not position or not position.active or position.required_per_team <= 0:
            raise ApiError("Posição de destino inválida.", 422)
        if position.id not in {
            registration.primary_position_id,
            registration.secondary_position_id,
        }:
            raise ApiError(
                "O jogador só pode atuar na posição principal ou secundária cadastrada.", 422
            )
        already_assigned = TeamMember.query.join(
            Team, TeamMember.team_id == Team.id
        ).join(
            Registration, TeamMember.registration_id == Registration.id
        ).filter(
            Team.event_id == target.event_id,
            Team.shift_id == target.shift_id,
            Registration.player_id == registration.player_id,
        ).first()
        if already_assigned:
            raise ApiError("Este jogador já está escalado nesta formação.", 409)

        occupants = TeamMember.query.filter_by(
            team_id=target.id, position_id=position.id
        ).order_by(TeamMember.id).all()
        if len(occupants) >= position.required_per_team:
            replacement = db.session.get(TeamMember, replace_member_id) if replace_member_id else None
            if not replacement or replacement not in occupants:
                raise ApiError(
                    f"{position.name} já atingiu o limite neste time. "
                    "Confirme qual jogador deve ir para o banco.",
                    409,
                    {"occupants": [
                        {"id": item.id, "name": item.registration.snapshot_name}
                        for item in occupants
                    ]},
                )
            replacement.registration.assigned_position_id = None
            if registration.status == "waitlist":
                registration.status = (
                    "confirmed"
                    if replacement.registration.status in {"confirmed", "present"}
                    else "pending_confirmation"
                )
                replacement.registration.status = "waitlist"
            db.session.delete(replacement)

        elif registration.status == "waitlist":
            registration.status = "pending_confirmation"

        registration.assigned_position_id = position.id
        db.session.add(TeamMember(
            team=target, registration=registration, position=position
        ))
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
        group_ids = [item["id"] for item in payload["linked_shifts"]]
        registrations = Registration.query.filter(
            Registration.event_id == event_id,
            Registration.shift_id.in_(group_ids),
        ).order_by(Registration.shift_id, Registration.priority_level, Registration.snapshot_name).all()
        active_statuses = {"confirmed", "pending_confirmation", "present"}
        active_registrations = [item for item in registrations if item.status in active_statuses]
        waiting = [item for item in registrations if item.status == "waitlist"]
        pending = [item for item in registrations if item.status == "pending_confirmation"]
        unavailable = [item for item in registrations if item.status not in active_statuses | {"waitlist"}]
        assigned_player_ids = {
            member["registration"]["player_id"]
            for team in payload["teams"]
            for member in team["members"]
        }
        place = current_place()
        separator = "━━━━━━━━━━━━━━"
        lines = [
            f"🏐 *VÔLEI {place.name.upper()}*",
            f"📅 *{event.title}* — {event.game_date:%d/%m/%Y}",
            f"🕐 {event.starts_at:%H:%M}",
            "",
            "📊 *RESUMO DE INSCRIÇÕES*",
            f"👥 Inscritos: {len(active_registrations)}",
            f"✅ Com vaga no time: {len(assigned_player_ids)}",
            f"⏳ Lista de espera: {len(waiting)}",
            f"🟡 Aguardando confirmação: {len(pending)}",
            f"❌ Cancelados/ausentes: {len(unavailable)}",
            "",
            separator,
            "",
        ]

        emoji_by_status = {
            "confirmed": "✅",
            "present": "✅",
            "pending_confirmation": "🟡",
        }
        for index, period in enumerate(payload["linked_shifts"]):
            period_registrations = [
                item for item in active_registrations if item.shift_id == period["id"]
            ]
            lines.append(
                f"{'🟢' if index % 2 == 0 else '🔵'} *{period['name'].upper()}* "
                f"({period['starts_at'][:5]} às {period['ends_at'][:5]}) "
                f"— {len(period_registrations)}"
            )
            if period_registrations:
                for registration in period_registrations:
                    lines.append(
                        f"{emoji_by_status[registration.status]} {registration.snapshot_name} "
                        f"— {registration.primary_position.name}"
                    )
            else:
                lines.append("▫️ Nenhum inscrito neste período.")
            lines.extend(["", separator, ""])

        if waiting:
            lines.append(f"⏳ *LISTA DE ESPERA* ({len(waiting)})")
            lines.extend(
                f"⏳ {registration.snapshot_name} — {registration.primary_position.name}"
                for registration in waiting
            )
            lines.extend(["", separator, ""])

        if unavailable:
            lines.append(f"❌ *CANCELADOS/AUSENTES* ({len(unavailable)})")
            lines.extend(
                f"❌ {registration.snapshot_name} — {registration.primary_position.name}"
                for registration in unavailable
            )
            lines.extend(["", separator, ""])
        vacancies = []
        positions = Position.query.filter(Position.active.is_(True), Position.required_per_team > 0).all()
        for team in payload["teams"]:
            for position in positions:
                occupied = sum(member["position_id"] == position.id for member in team["members"])
                if occupied < position.required_per_team:
                    vacancies.append(f"• {team['name']}: {position.required_per_team - occupied} vaga(s) de {position.name}")
        if vacancies:
            lines.extend(["🪑 *VAGAS DISPONÍVEIS*", *vacancies])
        return {"text": "\n".join(lines).strip()}
