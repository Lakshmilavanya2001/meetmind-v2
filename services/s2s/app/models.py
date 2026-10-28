"""The tables (migrations/*.sql) as SQLAlchemy models."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    join_code: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, server_default="Asia/Kolkata")
    preferred_language: Mapped[str] = mapped_column(Text, server_default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _id()
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    host_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(Text)
    join_code: Mapped[str] = mapped_column(Text, unique=True)
    room_name: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default="SCHEDULED")
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    save_transcript: Mapped[bool] = mapped_column(Boolean, server_default="false")
    transcript_saving_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcript_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    meeting_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, server_default="ATTENDEE")
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    speak: Mapped[str | None] = mapped_column(Text)
    hear: Mapped[str | None] = mapped_column(Text)
    voice: Mapped[str | None] = mapped_column(Text)
    noise: Mapped[str | None] = mapped_column(Text)
    rsvp: Mapped[str] = mapped_column(Text, server_default="PENDING")          # PENDING | ACCEPTED | DECLINED
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id"))
    speaker_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    speaker_name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    utterance_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SegmentTranslation(Base):
    __tablename__ = "segment_translations"
    segment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("transcript_segments.id"), primary_key=True)
    language: Mapped[str] = mapped_column(Text, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class MeetingSummary(Base):
    __tablename__ = "meeting_summaries"
    meeting_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("meetings.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    decisions: Mapped[list] = mapped_column(JSONB, server_default="[]")
    action_items: Mapped[list] = mapped_column(JSONB, server_default="[]")
    segment_count: Mapped[int] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
