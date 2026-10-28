"""Reminder tools (meetmind's create_reminder, list_reminders, cancel_reminder)."""
from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field

from ...reminders import service
from .base import Tool, ToolContext, ToolOutput


class CreateReminderArgs(BaseModel):
    text: str = Field(min_length=1, max_length=300, description="What to remind them about, in their words")
    remind_at: AwareDatetime = Field(description="When to remind as ISO 8601 with offset — get it from resolve_datetime")
    sms: bool | None = None


class NoArgs(BaseModel):
    pass


class CancelReminderArgs(BaseModel):
    reminder_id: str = Field(min_length=1)


async def _create_preflight(t: ToolContext, a: CreateReminderArgs) -> str:
    r = await service.check_reminder(t.s, t.user_id, a.text, a.remind_at, a.sms, t.now)
    return f"Remind you on {r['when']}: “{r['text']}”" + (f", also by SMS to {r['phone']}" if r["sms"] else "")


async def _create(t: ToolContext, a: CreateReminderArgs) -> ToolOutput:
    r = await service.create_reminder(t.s, t.user_id, t.org_id, a.text, a.remind_at, a.sms, "assistant", t.now)
    await t.s.commit()
    return ToolOutput(data={"id": r["id"], "text": r["text"], "when": r["when"], "sms": r["sms"]},
                      summary=f"Reminder · {r['when']}" + (" · SMS" if r["sms"] else ""), link=service.LINK)


async def _list(t: ToolContext, a: NoArgs) -> ToolOutput:
    rows = await service.list_reminders(t.s, t.user_id)
    return ToolOutput(data={"reminders": [{"id": r["id"], "text": r["text"], "when": r["when"], "sms": r["sms"]} for r in rows]},
                      summary=f"{len(rows)} upcoming reminder(s)", link=service.LINK)


async def _cancel_preflight(t: ToolContext, a: CancelReminderArgs) -> str:
    r = await service.get_pending(t.s, t.user_id, a.reminder_id)
    return f"Cancel the reminder “{r.text}” ({service.fmt_when(r.due_at, t.timezone, year=False)})"


async def _cancel(t: ToolContext, a: CancelReminderArgs) -> ToolOutput:
    data = await service.cancel_reminder(t.s, t.user_id, a.reminder_id)
    await t.s.commit()
    return ToolOutput(data=data, summary="Reminder cancelled")


TOOLS = [
    # Confirmed by the user: reminder text can come from what others said, and it may be texted to their phone.
    Tool(name="create_reminder", kind="sensitive",
         description="Set a personal reminder for the user (only for themselves). Get the time from resolve_datetime. sms=true texts it "
                     "to their verified mobile number too; leave sms unset to use their default setting.",
         args=CreateReminderArgs, run=_create, preflight=_create_preflight, label=lambda a: "Setting a reminder"),
    Tool(name="list_reminders", kind="read", description="List the user's upcoming reminders.",
         args=NoArgs, run=_list, label=lambda a: "Checking your reminders"),
    Tool(name="cancel_reminder", kind="sensitive", description="Cancel one of the user's upcoming reminders (id from list_reminders).",
         args=CancelReminderArgs, run=_cancel, preflight=_cancel_preflight, label=lambda a: "Cancelling a reminder"),
]
