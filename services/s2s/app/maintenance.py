"""Housekeeping, run by the watchdog every few minutes:

- expired sign-in sessions are deleted;
- confirmations and plans nobody decided within 15 minutes are marked EXPIRED;
- read notifications older than 90 days are deleted;
- saved transcripts (lines, translations, search index, summary) of meetings that ended more than
  TRANSCRIPT_RETENTION_DAYS ago are deleted, when that is set (default: kept).

Also: deleting one meeting's saved transcript (the host's "Delete transcript").
"""
from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("s2s.maintenance")
NOTIFICATION_DAYS = 90


def retention_days() -> int | None:
    v = os.environ.get("TRANSCRIPT_RETENTION_DAYS", "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


async def delete_transcript(s: AsyncSession, meeting_id: uuid.UUID) -> int:
    """Every saved line of a meeting, with its translations, search chunks and summary. Commits."""
    p = {"m": meeting_id}
    await s.execute(text("DELETE FROM transcript_chunks WHERE meeting_id = :m"), p)
    await s.execute(text("DELETE FROM meeting_summaries WHERE meeting_id = :m"), p)
    n = (await s.execute(text("DELETE FROM transcript_segments WHERE meeting_id = :m"), p)).rowcount    # translations cascade
    await s.execute(text("UPDATE meetings SET transcript_updated_at = NULL, indexed_at = NULL WHERE id = :m"), p)
    await s.commit()
    return n


async def run(s: AsyncSession) -> dict:
    done = {}
    done["sessions"] = (await s.execute(text("DELETE FROM sessions WHERE expires_at < now()"))).rowcount
    done["confirmations"] = (await s.execute(text(
        "UPDATE agent_actions SET status = 'EXPIRED', error = 'Confirmation expired' "
        "WHERE status = 'PENDING_CONFIRMATION' AND created_at < now() - interval '15 minutes'"))).rowcount
    done["plans"] = (await s.execute(text(
        "UPDATE agent_plans SET status = 'EXPIRED' WHERE status = 'PROPOSED' AND created_at < now() - interval '15 minutes'"))).rowcount
    done["notifications"] = (await s.execute(text(
        f"DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < now() - interval '{NOTIFICATION_DAYS} days'"))).rowcount
    await s.commit()
    days = retention_days()
    if days:
        old = (await s.execute(text(
            "SELECT m.id FROM meetings m WHERE m.status IN ('ENDED', 'CANCELLED') AND m.ended_at < now() - make_interval(days => :d) "
            "AND (m.transcript_updated_at IS NOT NULL OR EXISTS (SELECT 1 FROM transcript_segments t WHERE t.meeting_id = m.id)) LIMIT 50"),
            {"d": days})).scalars().all()
        for mid in old:
            await delete_transcript(s, mid)
        done["transcripts"] = len(old)
    done = {k: v for k, v in done.items() if v}
    if done:
        log.info("housekeeping: %s", done)
    return done
