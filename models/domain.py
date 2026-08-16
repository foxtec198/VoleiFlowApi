from datetime import datetime, timezone

from models.baseModel import BaseModel
from utils.db import db


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Admin(TimestampMixin, BaseModel):
    __tablename__ = "admins"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    token_version = db.Column(db.Integer, nullable=False, default=0)
    last_login = db.Column(db.DateTime(timezone=True))


event_shifts = db.Table(
    "event_shifts",
    db.Column("event_id", db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    db.Column("shift_id", db.Integer, db.ForeignKey("shifts.id"), primary_key=True),
)


class Position(TimestampMixin, BaseModel):
    __tablename__ = "positions"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    required_per_team = db.Column(db.Integer, nullable=False, default=1)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)


class Player(TimestampMixin, BaseModel):
    __tablename__ = "players"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(30), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    primary_position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), nullable=False, index=True)
    secondary_position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), index=True)
    knowledge_level = db.Column(db.Integer, nullable=False, default=5)
    reception = db.Column(db.Integer, nullable=False, default=5)
    setting = db.Column(db.Integer, nullable=False, default=5)
    blocking = db.Column(db.Integer, nullable=False, default=5)
    serving = db.Column(db.Integer, nullable=False, default=5)
    attack = db.Column(db.Integer, nullable=False, default=5)
    defense = db.Column(db.Integer, nullable=False, default=5)

    primary_position = db.relationship("Position", foreign_keys=[primary_position_id])
    secondary_position = db.relationship("Position", foreign_keys=[secondary_position_id])


class Shift(TimestampMixin, BaseModel):
    __tablename__ = "shifts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    starts_at = db.Column(db.Time, nullable=False)
    ends_at = db.Column(db.Time, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)


class Event(TimestampMixin, BaseModel):
    __tablename__ = "events"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False, default="Jogo de vôlei")
    game_date = db.Column(db.Date, nullable=False, index=True)
    starts_at = db.Column(db.Time, nullable=False)
    registration_opens_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    confirmation_deadline = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    team_count = db.Column(db.Integer, nullable=False, default=3)
    status = db.Column(db.String(30), nullable=False, default="scheduled", index=True)
    recurrence_group = db.Column(db.String(36), index=True)
    recurrence_rule = db.Column(db.JSON)

    shifts = db.relationship("Shift", secondary=event_shifts, lazy="selectin")


class Registration(TimestampMixin, BaseModel):
    __tablename__ = "registrations"
    __table_args__ = (
        db.UniqueConstraint("event_id", "player_id", "shift_id", name="uq_registration_event_player_shift"),
    )
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False, index=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), nullable=False, index=True)
    primary_position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), nullable=False, index=True)
    secondary_position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), index=True)
    assigned_position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), index=True)
    status = db.Column(db.String(40), nullable=False, default="pending_confirmation", index=True)
    notes = db.Column(db.Text)
    absence_reason = db.Column(db.Text)
    email_confirmation_token = db.Column(db.String(80), unique=True, index=True)
    email_confirmed_at = db.Column(db.DateTime(timezone=True))
    confirmed_at = db.Column(db.DateTime(timezone=True), index=True)
    snapshot_name = db.Column(db.String(160), nullable=False)
    snapshot_knowledge_level = db.Column(db.Integer, nullable=False)
    snapshot_reception = db.Column(db.Integer, nullable=False)
    snapshot_setting = db.Column(db.Integer, nullable=False)
    snapshot_blocking = db.Column(db.Integer, nullable=False)
    snapshot_serving = db.Column(db.Integer, nullable=False)
    snapshot_attack = db.Column(db.Integer, nullable=False)
    snapshot_defense = db.Column(db.Integer, nullable=False)

    event = db.relationship("Event")
    player = db.relationship("Player")
    shift = db.relationship("Shift")
    primary_position = db.relationship("Position", foreign_keys=[primary_position_id])
    secondary_position = db.relationship("Position", foreign_keys=[secondary_position_id])
    assigned_position = db.relationship("Position", foreign_keys=[assigned_position_id])

    @property
    def overall(self):
        values = [self.snapshot_knowledge_level, self.snapshot_reception, self.snapshot_setting,
                  self.snapshot_blocking, self.snapshot_serving, self.snapshot_attack, self.snapshot_defense]
        return round(sum(values) / len(values), 2)


class BlacklistEntry(TimestampMixin, BaseModel):
    __tablename__ = "blacklist_entries"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    included_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    origin = db.Column(db.String(40), nullable=False, default="manual", index=True)
    source_event_id = db.Column(db.Integer, db.ForeignKey("events.id"), index=True)
    removed_at = db.Column(db.DateTime(timezone=True), index=True)
    removal_reason = db.Column(db.Text)

    player = db.relationship("Player")
    source_event = db.relationship("Event")


class Team(TimestampMixin, BaseModel):
    __tablename__ = "teams"
    __table_args__ = (db.UniqueConstraint("event_id", "shift_id", "number", name="uq_team_event_shift_number"),)
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    balance_score = db.Column(db.Float, nullable=False, default=0)

    members = db.relationship("TeamMember", back_populates="team", cascade="all, delete-orphan", lazy="selectin")


class TeamMember(TimestampMixin, BaseModel):
    __tablename__ = "team_members"
    __table_args__ = (db.UniqueConstraint("registration_id", name="uq_team_member_registration"),)
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = db.Column(db.Integer, db.ForeignKey("registrations.id", ondelete="CASCADE"), nullable=False, index=True)
    position_id = db.Column(db.Integer, db.ForeignKey("positions.id"), nullable=False, index=True)

    team = db.relationship("Team", back_populates="members")
    registration = db.relationship("Registration")
    position = db.relationship("Position")


class Setting(TimestampMixin, BaseModel):
    __tablename__ = "settings"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.JSON, nullable=False)


class OfflineOperation(BaseModel):
    __tablename__ = "offline_operations"
    operation_id = db.Column(db.String(80), primary_key=True)
    operation_type = db.Column(db.String(50), nullable=False, index=True)
    payload = db.Column(db.JSON, nullable=False)
    response = db.Column(db.JSON)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
