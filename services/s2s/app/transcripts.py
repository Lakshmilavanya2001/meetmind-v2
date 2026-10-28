"""Saved transcripts, summaries and questions, over HTTP.

- The host or a co-host turns "Save transcript" on or off for a meeting; while it's on, everyone in
  the call sees a notice, and the call agent saves each line (and its translations).
- Only the meeting's participants (invited or joined, not removed) can read its transcript and
  summary, or ask about it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import maintenance, search, summaries
from .accounts import Ctx, audit, current, get_db
from .meetings import MODERATORS, load
from .models import Meeting, MeetingSummary, SegmentTranslation, TranscriptSegment
from .security import rate_limit

router = APIRouter()


def _http() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=60))


async def _participant(s: AsyncSession, ctx: Ctx, code: str) -> Meeting:
    m, me = await load(s, ctx, code)
    if me is None:
        raise HTTPException(403, "Only the meeting's participants can see its transcript.")
    return m


def summary_view(sm: MeetingSummary | None) -> dict | None:
    if sm is None:
        return None
    return {"summary": sm.summary, "decisions": sm.decisions, "actionItems": sm.action_items,
            "segmentCount": sm.segment_count, "generatedAt": sm.generated_at.isoformat()}


class SaveIn(BaseModel):
    enabled: bool


@router.post("/api/meetings/{code}/transcript")
async def set_saving(code: str, body: SaveIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    if not me or me.role not in MODERATORS:
        raise HTTPException(403, "Only the host or a co-host can turn saving the transcript on or off.")
    if m.status in ("ENDED", "CANCELLED"):
        raise HTTPException(409, "This meeting is over.")
    if m.save_transcript != body.enabled:
        m.save_transcript = body.enabled
        if body.enabled:
            m.transcript_saving_since = datetime.now(timezone.utc)
        await audit(s, "transcript.enable" if body.enabled else "transcript.disable", ctx.org.id, ctx.user.id, "Meeting", m.id)
        await s.commit()
    return {"saveTranscript": m.save_transcript}


@router.delete("/api/meetings/{code}/transcript")
async def delete_saved(code: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    """The host or a co-host deletes everything saved from the meeting (lines, translations, summary, search)."""
    m, me = await load(s, ctx, code)
    if not me or me.role not in MODERATORS:
        raise HTTPException(403, "Only the host or a co-host can delete the transcript.")
    n = await maintenance.delete_transcript(s, m.id)
    await audit(s, "transcript.delete", ctx.org.id, ctx.user.id, "Meeting", m.id, lines=n)
    await s.commit()
    return {"deleted": n}


@router.get("/api/meetings/{code}/transcript")
async def transcript(code: str, lang: str = "", ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    m, me = await load(s, ctx, code)
    if me is None:
        raise HTTPException(403, "Only the meeting's participants can see its transcript.")
    segs = await summaries.lines_for(s, m.id)
    tr = {}
    if lang and segs:
        tr = {r.segment_id: r.text for r in (await s.execute(select(SegmentTranslation).where(
            SegmentTranslation.segment_id.in_([sg.id for sg in segs]), SegmentTranslation.language == lang))).scalars()}
    start = m.started_at or (segs[0].started_at if segs else None)
    return {"meeting": {"title": m.title, "joinCode": m.join_code, "status": m.status, "saveTranscript": m.save_transcript,
                        "startedAt": m.started_at.isoformat() if m.started_at else None, "canDelete": me.role in MODERATORS},
            "segments": [{"id": sg.id, "speaker": sg.speaker_name, "language": sg.language, "text": sg.text,
                          "translation": tr.get(sg.id), "at": search._clock(start, sg.started_at) if start else None}
                         for sg in segs],
            "summary": summary_view(await s.get(MeetingSummary, m.id))}


@router.post("/api/meetings/{code}/summary")
async def summarise_now(code: str, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rate_limit(f"summary:{ctx.user.id}", 6, 600)
    m = await _participant(s, ctx, code)
    if not (await s.execute(select(func.count()).select_from(TranscriptSegment).where(TranscriptSegment.meeting_id == m.id))).scalar():
        raise HTTPException(409, "There's no saved transcript to summarise yet.")
    async with _http() as http:
        sm = await summaries.summarise(s, http, m)
    if sm is None:
        raise HTTPException(503, "The summary model isn't available.")
    return {"summary": summary_view(sm)}


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    meetingCode: str | None = None


@router.post("/api/ask")
async def ask(body: AskIn, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rate_limit(f"ask:{ctx.user.id}", 30, 600)
    meeting_id = (await _participant(s, ctx, body.meetingCode)).id if body.meetingCode else None
    async with _http() as http:
        return await search.ask(s, http, ctx.user.id, ctx.org.id, body.question.strip(), meeting_id)


async def background(s: AsyncSession, http: aiohttp.ClientSession) -> None:
    """Run by the watchdog: summarise meetings that ended with new lines, and keep the index current."""
    rows = (await s.execute(
        select(Meeting).outerjoin(MeetingSummary, MeetingSummary.meeting_id == Meeting.id)
        .where(Meeting.status == "ENDED", Meeting.transcript_updated_at.is_not(None),
               (MeetingSummary.meeting_id.is_(None)) | (MeetingSummary.generated_at < Meeting.transcript_updated_at))
        .limit(3))).scalars().all()
    for m in rows:
        try:
            await summaries.summarise(s, http, m)
        except Exception as e:  # noqa: BLE001
            await s.rollback()
            summaries.log.warning("summary of meeting %s failed: %s", m.id, e)
    await search.index_pending(s, http)
