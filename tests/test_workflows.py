from datetime import date, timedelta

from models import Admin, BlacklistEntry, Event, Place, PlacePlayer, Player, Registration
from utils.db import db


def create_base(client, players=2):
    positions = client.get("/api/positions?per_page=20").get_json()["items"]
    ponteiro = next(item for item in positions if item["name"] == "Ponteiro")
    shift = client.post("/api/shifts", json={"name": "Noite", "starts_at": "19:00", "ends_at": "22:00"}).get_json()
    created_players = []
    for index in range(players):
        response = client.post("/api/players", json={
            "name": f"Jogador {index + 1}", "email": f"j{index + 1}@example.com", "phone": "11999999999",
            "primary_position_id": ponteiro["id"], "knowledge_level": 4 + index,
            "reception": 4 + index, "setting": 4 + index, "blocking": 4 + index,
            "serving": 4 + index, "attack": 4 + index, "defense": 4 + index,
        })
        assert response.status_code == 201
        created_players.append(response.get_json())
    game_date = date.today() + timedelta(days=7)
    event = client.post("/api/events", json={
        "title": "Treino", "game_date": game_date.isoformat(), "starts_at": "19:00",
        "registration_opens_at": f"{date.today().isoformat()}T00:00:00-03:00",
        "team_count": 2, "shift_ids": [shift["id"]],
    }).get_json()
    return ponteiro, shift, created_players, event


def register(client, event, shift, position, player):
    response = client.post("/api/registrations", json={
        "event_id": event["id"], "shift_id": shift["id"], "player_id": player["id"],
        "primary_position_id": position["id"], "notes": "Chego no horário",
    })
    assert response.status_code == 201
    return response.get_json()


def test_crud_and_deduplicated_registration(client):
    position, shift, players, event = create_base(client, 1)
    registration = register(client, event, shift, position, players[0])
    assert registration["status"] == "pending_confirmation"
    duplicate = client.post("/api/registrations", json={
        "event_id": event["id"], "shift_id": shift["id"], "player_id": players[0]["id"],
        "primary_position_id": position["id"],
    })
    assert duplicate.status_code == 409
    toggled = client.patch(f"/api/players/{players[0]['id']}/active", json={"active": False})
    assert toggled.status_code == 200
    assert toggled.get_json()["active"] is False


def test_confirmation_formation_and_balance(client, app):
    position, shift, players, event = create_base(client, 2)
    registrations = [register(client, event, shift, position, player) for player in players]
    with app.app_context():
        tokens = [db.session.get(Registration, item["id"]).email_confirmation_token for item in registrations]
    for token in tokens:
        response = client.get(f"/api/registrations/confirm/{token}")
        assert response.status_code == 200
        assert response.get_json()["status"] == "confirmed"
    formation = client.post(f"/api/events/{event['id']}/shifts/{shift['id']}/formation")
    assert formation.status_code == 200
    data = formation.get_json()
    assert len(data["teams"]) == 2
    assert all(len(team["members"]) == 1 for team in data["teams"])
    assert data["missing"] == []
    assert data["differences"]["overall"] == 1.0


def test_unjustified_absence_blacklists_and_removal_preserves_history(client, app):
    position, shift, players, event = create_base(client, 2)
    first = register(client, event, shift, position, players[0])
    absence = client.patch(f"/api/registrations/{first['id']}/status", json={
        "status": "unjustified_absence", "reason": "Não compareceu",
    })
    assert absence.status_code == 200
    with app.app_context():
        entry = BlacklistEntry.query.filter_by(player_id=players[0]["id"]).one()
        entry_id = entry.id
        assert entry.source_event_id == event["id"]
    removed = client.delete(f"/api/blacklist/{entry_id}", json={"reason": "Penalidade cumprida"})
    assert removed.status_code == 200
    assert removed.get_json()["active"] is False
    with app.app_context():
        assert BlacklistEntry.query.count() == 1


def test_blacklisted_player_is_waitlisted_and_offline_sync_is_idempotent(client, app):
    position, shift, players, event = create_base(client, 1)
    assert client.post("/api/blacklist", json={"player_id": players[0]["id"], "reason": "Bloqueio manual"}).status_code == 201
    registration = register(client, event, shift, position, players[0])
    assert registration["status"] == "waitlist"
    operation = {"id": "op-fixed-1", "type": "notes", "payload": {
        "registration_id": registration["id"], "notes": "Atualizado offline",
    }}
    first = client.post("/api/offline/sync", json={"operations": [operation]}).get_json()
    second = client.post("/api/offline/sync", json={"operations": [operation]}).get_json()
    assert first == second
    with app.app_context():
        assert db.session.get(Registration, registration["id"]).notes == "Atualizado offline"


def test_confirmed_player_displaces_pending_when_position_is_full(client, app):
    position, shift, players, event = create_base(client, 3)
    registrations = [register(client, event, shift, position, player) for player in players]
    with app.app_context():
        token = db.session.get(Registration, registrations[2]["id"]).email_confirmation_token
    confirmed = client.get(f"/api/registrations/confirm/{token}")
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "confirmed"
    with app.app_context():
        statuses = {item.id: item.status for item in Registration.query.all()}
    assert list(statuses.values()).count("confirmed") == 1
    assert list(statuses.values()).count("pending_confirmation") == 1
    assert list(statuses.values()).count("waitlist") == 1


def test_admin_login_requires_argon2id_pepper_and_jwt(client, app):
    anonymous = app.test_client()
    assert anonymous.get("/api/settings").status_code == 401
    assert anonymous.post("/api/auth/login", json={"email": "admin@example.com", "password": "invalid"}).status_code == 401
    password = app.config["TEST_ADMIN_PASSWORD"]
    login = anonymous.post("/api/auth/login", json={"email": "ADMIN@example.com", "password": password})
    assert login.status_code == 200
    assert login.get_json()["access_token"]
    with app.app_context():
        admin = Admin.query.filter_by(email="admin@example.com").one()
        assert admin.password_hash.startswith("$argon2id$")
        assert password not in admin.password_hash


def test_event_can_be_removed_individually_or_by_recurrence(client, app):
    _position, shift, _players, _event = create_base(client, 0)
    first_date = (date.today() + timedelta(days=10)).isoformat()
    second_date = (date.today() + timedelta(days=17)).isoformat()
    response = client.post("/api/events/recurring", json={
        "title": "Liga recorrente", "dates": [first_date, second_date], "game_date": first_date,
        "starts_at": "19:00", "registration_opens_at": f"{date.today().isoformat()}T00:00:00-03:00",
        "team_count": 2, "shift_ids": [shift["id"]],
    })
    assert response.status_code == 201
    items = response.get_json()["items"]
    assert items[0]["recurrence_group"] == items[1]["recurrence_group"]
    single = client.delete(f"/api/events/{items[0]['id']}?scope=single")
    assert single.status_code == 200
    assert single.get_json()["removed_count"] == 1
    recurrence = client.delete(f"/api/events/{items[1]['id']}?scope=recurrence")
    assert recurrence.status_code == 200
    assert recurrence.get_json()["removed_count"] == 1
    with app.app_context():
        assert db.session.get(Event, items[0]["id"]).status == "deleted"
        assert db.session.get(Event, items[1]["id"]).status == "deleted"


def test_unlimited_recurrence_has_no_occurrence_limit_and_stops_when_removed(client, app):
    _position, shift, _players, _event = create_base(client, 0)
    start = date.today() + timedelta(days=1)
    response = client.post("/api/events/recurring", json={
        "title": "Treino semanal permanente",
        "start_date": start.isoformat(),
        "game_date": start.isoformat(),
        "weekdays": [start.weekday()],
        "starts_at": "19:00",
        "registration_opens_at": f"{date.today().isoformat()}T00:00:00-03:00",
        "team_count": 2,
        "shift_ids": [shift["id"]],
    })
    assert response.status_code == 201
    items = response.get_json()["items"]
    assert len(items) > 10
    assert all(item["recurrence_rule"]["unlimited"] is True for item in items)
    assert all("occurrences" not in item["recurrence_rule"] for item in items)

    listing = client.get("/api/events?per_page=100").get_json()
    recurrence = next(item for item in listing["recurrences"] if item["recurrence_group"] == items[0]["recurrence_group"])
    assert recurrence["unlimited"] is True
    assert recurrence["occurrences_created"] == len(items)

    removed = client.delete(f"/api/events/{items[0]['id']}?scope=recurrence").get_json()
    assert removed["removed_count"] == len(items)
    refreshed = client.get("/api/events?per_page=100").get_json()
    assert all(item["recurrence_group"] != items[0]["recurrence_group"] for item in refreshed["recurrences"])
    with app.app_context():
        assert Event.query.filter_by(recurrence_group=items[0]["recurrence_group"], status="scheduled").count() == 0


def test_guest_and_attendance_history_define_registration_priority(client, app):
    position, shift, players, event = create_base(client, 3)
    member, regular, guest = players
    with app.app_context():
        member_row = PlacePlayer.query.filter_by(player_id=member["id"]).one()
        member_row.attendance_count = 3
        guest_row = PlacePlayer.query.filter_by(player_id=guest["id"]).one()
        guest_row.is_guest = True
        db.session.commit()

    member_registration = register(client, event, shift, position, member)
    regular_registration = register(client, event, shift, position, regular)
    guest_response = client.post("/api/registrations", json={
        "event_id": event["id"], "shift_id": shift["id"], "player_id": guest["id"],
        "primary_position_id": position["id"], "is_guest": True,
    })
    assert guest_response.status_code == 201
    guest_registration = guest_response.get_json()

    assert member_registration["priority_level"] == 1
    assert member_registration["is_guest"] is False
    assert guest_registration["priority_level"] == 3
    assert guest_registration["is_guest"] is True
    assert member_registration["status"] == "pending_confirmation"
    assert regular_registration["status"] == "pending_confirmation"
    assert guest_registration["status"] == "waitlist"

    marked_present = client.patch(
        f"/api/registrations/{guest_registration['id']}/status", json={"status": "present"}
    )
    assert marked_present.status_code == 200
    with app.app_context():
        assert PlacePlayer.query.filter_by(player_id=guest["id"]).one().attendance_count == 1

    marked_absent = client.patch(
        f"/api/registrations/{guest_registration['id']}/status",
        json={"status": "justified_absence", "reason": "Avisou antes"},
    )
    assert marked_absent.status_code == 200
    with app.app_context():
        guest_row = PlacePlayer.query.filter_by(player_id=guest["id"]).one()
        assert guest_row.attendance_count == 0
        assert guest_row.absence_count == 1

    public_guest = next(
        item for item in client.get("/api/public/bootstrap").get_json()["players"]["items"]
        if item["id"] == guest["id"]
    )
    assert public_guest["is_guest"] is True
    assert "birth_date" not in public_guest
    assert "attendance_count" not in public_guest


def test_players_and_membership_are_isolated_by_place_route(client, app):
    position, _shift, players, _event = create_base(client, 1)
    with app.app_context():
        db.session.add(Place(name="Outra quadra", slug="outra-quadra", active=True))
        db.session.commit()

    client.environ_base["HTTP_X_PLACE_SLUG"] = "outra-quadra"
    assert client.get("/api/players?per_page=100").get_json()["pagination"]["total"] == 0
    linked = client.post("/api/players", json={
        "name": players[0]["name"], "email": players[0]["email"], "phone": players[0]["phone"],
        "primary_position_id": position["id"], "is_guest": True,
    })
    assert linked.status_code == 201
    assert linked.get_json()["is_guest"] is True

    client.environ_base["HTTP_X_PLACE_SLUG"] = "nilo"
    nilo_player = client.get(f"/api/players/{players[0]['id']}").get_json()
    assert nilo_player["is_guest"] is False
    with app.app_context():
        assert PlacePlayer.query.filter_by(player_id=players[0]["id"]).count() == 2

    updated_place = client.patch("/api/place", json={
        "name": "Nilo", "slug": "nilo", "address": "Rua de teste, 10", "city": "Londrina", "state": "pr",
    })
    assert updated_place.status_code == 200
    assert updated_place.get_json()["state"] == "PR"
    assert client.get("/api/public/bootstrap").get_json()["place"]["address"] == "Rua de teste, 10"


def test_public_player_search_is_limited_and_returns_name_with_email(client):
    position, _shift, _players, _event = create_base(client, 0)
    for index in range(30):
        response = client.post("/api/players", json={
            "name": f"Jogador {index:02d}",
            "email": f"busca{index:02d}@example.com",
            "phone": f"4399999{index:04d}",
            "primary_position_id": position["id"],
        })
        assert response.status_code == 201

    initial = client.get("/api/public/players?per_page=25").get_json()
    assert len(initial["items"]) == 25
    assert initial["pagination"]["total"] == 30
    assert set(initial["items"][0]).issuperset({"id", "name", "email"})
    assert "phone" not in initial["items"][0]

    searched = client.get("/api/public/players?per_page=25&search=busca27@example.com").get_json()
    assert [item["email"] for item in searched["items"]] == ["busca27@example.com"]

def test_overlapping_shifts_share_formation_and_expose_selected_periods(client):
    position, late_shift, players, _original_event = create_base(client, 3)
    early_shift = client.post("/api/shifts", json={
        "name": "Início", "starts_at": "15:00", "ends_at": "18:00",
    }).get_json()
    middle_shift = client.post("/api/shifts", json={
        "name": "Meio", "starts_at": "17:00", "ends_at": "20:00",
    }).get_json()
    game_date = date.today() + timedelta(days=14)
    event = client.post("/api/events", json={
        "title": "Rachão integrado", "game_date": game_date.isoformat(), "starts_at": "15:00",
        "registration_opens_at": f"{date.today().isoformat()}T00:00:00-03:00",
        "team_count": 3, "shift_ids": [early_shift["id"], middle_shift["id"], late_shift["id"]],
    }).get_json()
    selected_shifts = [early_shift, middle_shift, late_shift]
    for player, shift in zip(players, selected_shifts):
        register(client, event, shift, position, player)

    generated = client.post(f"/api/events/{event['id']}/shifts/{early_shift['id']}/formation")
    assert generated.status_code == 200
    formation = generated.get_json()
    assert [item["id"] for item in formation["linked_shifts"]] == [item["id"] for item in selected_shifts]
    assert sum(len(team["members"]) for team in formation["teams"]) == 3
    periods = {
        member["registration"]["player_name"]: member["registration"]["selected_periods"][0]["name"]
        for team in formation["teams"] for member in team["members"]
    }
    assert periods == {players[index]["name"]: selected_shifts[index]["name"] for index in range(3)}

    same_formation = client.get(f"/api/events/{event['id']}/shifts/{late_shift['id']}/formation").get_json()
    assert [team["id"] for team in same_formation["teams"]] == [team["id"] for team in formation["teams"]]

    situation = client.get(f"/api/players/{players[0]['id']}/events/{event['id']}/situation").get_json()
    assert situation["items"][0]["selected_period"]["name"] == early_shift["name"]
    assert len(situation["formations"]) == 1
    visible_players = {
        member["registration"]["player_name"]
        for team in situation["formations"][0]["teams"] for member in team["members"]
    }
    assert visible_players == {player["name"] for player in players}
