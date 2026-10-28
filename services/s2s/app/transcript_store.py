"""The call agent's side of saved transcripts: which meeting a room belongs to, whether its host has
"Save transcript" on, and writing lines and their translations. Saving is best effort: a database
problem never disturbs the call (the line is just not saved, and the log says so)."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from . import db
from .models import Meeting, SegmentTranslation, TranscriptSegment

log = logging.getLogger("s2s.transcripts")
CHECK_EVERY_S = 5                    # how often the host's choice is re-read


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


class RoomTranscript:
    """One room's saved transcript, as seen by the agent in that call."""

    def __init__(self, room_name: str):
        self.room_name = room_name
        self.meeting_id: uuid.UUID | None = None
        self.saving = False
        self.checked_at = 0.0
        self.segment_ids: dict[str, int] = {}          # the agent's utterance id -> saved line

    async def refresh(self) -> bool:
        """Is saving on right now? (re-read at most every few seconds)"""
        if not enabled():
            return False
        if time.time() - self.checked_at < CHECK_EVERY_S:
            return self.saving
        self.checked_at = time.time()
        try:
            async with db.sessionmaker()() as s:
                m = (await s.execute(select(Meeting.id, Meeting.save_transcript, Meeting.status)
                                     .where(Meeting.room_name == self.room_name))).first()
            self.meeting_id = m.id if m else None
            self.saving = bool(m and m.save_transcript and m.status == "LIVE")
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't read the transcript setting for %s: %s", self.room_name, e)
        return self.saving

    async def line(self, utt_key: str, speaker_id: str, speaker_name: str, lang: str, text: str,
                   started: float, ended: float) -> int | None:
        if not text or not await self.refresh() or self.meeting_id is None:
            return None
        try:
            uid = uuid.UUID(speaker_id)
        except ValueError:
            uid = None                                   # not an account (a test caller): keep the name only
        ts = lambda t: datetime.fromtimestamp(t, timezone.utc)  # noqa: E731
        try:
            async with db.sessionmaker()() as s:
                seg_id = (await s.execute(
                    insert(TranscriptSegment).values(meeting_id=self.meeting_id, speaker_user_id=uid, speaker_name=speaker_name,
                                                     language=lang, text=text, started_at=ts(started), ended_at=ts(max(ended, started)),
                                                     utterance_key=utt_key)
                    .on_conflict_do_nothing(index_elements=["meeting_id", "utterance_key"]).returning(TranscriptSegment.id))).scalar()
                await s.execute(update(Meeting).where(Meeting.id == self.meeting_id).values(transcript_updated_at=datetime.now(timezone.utc)))
                await s.commit()
            if seg_id:
                self.segment_ids[utt_key] = seg_id
                if len(self.segment_ids) > 2000:
                    self.segment_ids.pop(next(iter(self.segment_ids)))
            return seg_id
        except Exception as e:  # noqa: BLE001
            log.warning("transcript line for %s not saved: %s", self.room_name, e)
            return None

    async def translation(self, utt_key: str, lang: str, text: str):
        seg_id = self.segment_ids.get(utt_key)
        if not seg_id or not text:
            return
        try:
            async with db.sessionmaker()() as s:
                await s.execute(insert(SegmentTranslation).values(segment_id=seg_id, language=lang, text=text)
                                .on_conflict_do_update(index_elements=["segment_id", "language"], set_={"text": text}))
                await s.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("translation for %s not saved: %s", self.room_name, e)
