"""The Planner's plans (ported from meetmind's plans.ts and decidePlan).

propose_plan's steps are each checked with the same preflight as a single confirmation (permissions,
real ids, free slots), so the user approves exact sentences. Approval runs the steps in order; a step
whose prerequisite didn't succeed is skipped. If something failed, the Planner is asked once more for
replacement steps only (at most two revisions), which again need approval.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from .tools.base import REGISTRY, Tool, ToolContext, ToolError, ToolOutput

log = logging.getLogger("s2s.assistant")
PLAN_TTL = timedelta(minutes=15)
MAX_REVISIONS = 2
PLAN_ACTION_TOOLS = ["create_meeting", "invite_participants", "reserve_table", "order_food", "book_cab", "create_reminder", "book_movie_tickets"]


def _owner(tool: str) -> str:
    from .agents import SPECIALISTS
    return next((s.id for s in SPECIALISTS if s.id not in ("general", "planner") and tool in s.tools), "general")


class PlanStepIn(BaseModel):
    tool: str = Field(description="One of: " + ", ".join(PLAN_ACTION_TOOLS))
    args: dict = Field(description="Arguments for that tool, exactly as the tool itself expects them")
    why: str = Field(min_length=3, max_length=200, description="One short line: why this step serves the goal")
    depends_on: list[int] = Field(default_factory=list, max_length=5, description="1-based numbers of earlier steps that must succeed first")


class ProposePlanArgs(BaseModel):
    goal: str = Field(min_length=3, max_length=300, description="The user's goal in one line")
    steps: list[PlanStepIn] = Field(min_length=1, max_length=6)


def _validation_message(e: Exception) -> str:
    if isinstance(e, ToolError):
        return e.message
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    return "invalid arguments"


async def create_plan(t: ToolContext, a: ProposePlanArgs, parent_id=None, revision: int = 0) -> dict:
    steps = []
    for i, st in enumerate(a.steps):
        n = i + 1
        tool = REGISTRY.get(st.tool)
        if tool is None or st.tool not in PLAN_ACTION_TOOLS or tool.preflight is None:
            raise ToolError("VALIDATION", f"Step {n}: {st.tool} can't be part of a plan.")
        deps = sorted(set(st.depends_on))
        if any(d >= n or d < 1 for d in deps):
            raise ToolError("VALIDATION", f"Step {n}: depends_on may only name earlier steps.")
        try:
            args = tool.args.model_validate(st.args)
            summary = await tool.preflight(t, args)
        except Exception as e:  # noqa: BLE001
            await t.s.rollback()
            if not isinstance(e, (ToolError, ValidationError)):
                log.exception("plan preflight crashed")
            raise ToolError("VALIDATION", f"Step {n} ({st.tool}) isn't valid: {_validation_message(e)}. Fix this step and call propose_plan again.") from None
        await t.s.rollback()
        steps.append({"index": n, "tool": st.tool, "args": args.model_dump(mode="json"), "summary": summary, "why": st.why,
                      "dependsOn": deps, "agent": _owner(st.tool), "status": "pending"})
    row = (await t.s.execute(text("INSERT INTO agent_plans (conversation_id, organization_id, user_id, goal, steps, status, parent_id, revision) "
                                  "VALUES (:c, :o, :u, :g, CAST(:s AS jsonb), 'PROPOSED', :p, :r) RETURNING *"),
                             {"c": t.conversation_id, "o": t.org_id, "u": t.user_id, "g": a.goal, "s": json.dumps(steps),
                              "p": parent_id, "r": revision})).mappings().first()
    await t.s.commit()
    return dict(row)


def plan_view(p: dict) -> dict:
    status = p["status"]
    expired = status == "PROPOSED" and datetime.now(timezone.utc) - p["created_at"] > PLAN_TTL
    keep = ("index", "summary", "why", "agent", "status", "resultSummary", "error", "link", "dependsOn")
    return {"id": str(p["id"]), "goal": p["goal"], "revision": p["revision"],
            "status": "expired" if expired else {"DONE": "completed"}.get(status, status.lower()),
            "steps": [{k: st.get(k) for k in keep} for st in p["steps"]]}


def plan_action_signatures() -> str:
    def describe(p: dict, defs: dict) -> str:
        if "$ref" in p:
            p = defs[p["$ref"].split("/")[-1]]
        if "anyOf" in p:
            opts = [describe(x, defs) for x in p["anyOf"] if x.get("type") != "null"]
            return "|".join(opts)
        if "enum" in p:
            return "|".join(json.dumps(v) for v in p["enum"])
        if p.get("type") == "array":
            return describe(p.get("items", {}), defs) + "[]"
        if p.get("type") == "object" and p.get("properties"):
            req = set(p.get("required", []))
            return "{" + ", ".join(f"{k}{'' if k in req else '?'}: {describe(v, defs)}" for k, v in p["properties"].items()) + "}"
        return p.get("format") or p.get("type", "any")
    out = []
    for name in PLAN_ACTION_TOOLS:
        if name in REGISTRY:
            js = REGISTRY[name].args.model_json_schema()
            out.append(f"- {name} {describe(js, js.get('$defs', {}))}")
    return "\n".join(out)


async def execute_plan(t: ToolContext, plan: dict) -> list[dict]:
    steps = plan["steps"]
    for st in steps:
        blocked = next((d for d in st["dependsOn"] if steps[d - 1]["status"] != "done"), None)
        if blocked:
            st["status"], st["error"] = "skipped", f"Skipped because step {blocked} didn't succeed."
            continue
        tool = REGISTRY[st["tool"]]
        t0 = time.time()
        try:
            out: ToolOutput = await tool.run(t, tool.args.model_validate(st["args"]))
            st["status"], st["resultSummary"], st["link"] = "done", out.summary, out.link
            status, result, error = "SUCCEEDED", json.loads(json.dumps(out.data, default=str)), None
        except Exception as e:  # noqa: BLE001
            await t.s.rollback()
            if not isinstance(e, (ToolError, ValidationError)):
                log.exception("plan step crashed")
            st["status"] = "failed"
            st["error"] = _validation_message(e) if isinstance(e, (ToolError, ValidationError)) else "Internal error; nothing was changed by this step."
            status, result, error = "FAILED", None, st["error"]
        await t.s.execute(text("INSERT INTO agent_actions (conversation_id, organization_id, user_id, tool_name, arguments, summary, status, result, error, agent, duration_ms, decided_at) "
                               "VALUES (:c, :o, :u, :n, CAST(:a AS jsonb), :sm, :st, CAST(:r AS jsonb), :e, 'planner', :d, now())"),
                          {"c": plan["conversation_id"], "o": plan["organization_id"], "u": plan["user_id"], "n": st["tool"],
                           "a": json.dumps(st["args"]), "sm": st["summary"], "st": status, "r": json.dumps(result) if result is not None else None,
                           "e": error, "d": round((time.time() - t0) * 1000)})
        await t.s.commit()
    return steps


async def decide_plan(s, http, a, plan_id: str, decision: str) -> dict:
    """Approve or reject a plan. Only its user decides; once; within 15 minutes."""
    from . import agents, engine
    row = (await s.execute(text("SELECT * FROM agent_plans WHERE id = :id AND user_id = :u AND organization_id = :o"),
                           {"id": plan_id, "u": a.user_id, "o": a.org_id})).mappings().first()
    if row is None:
        raise ToolError("NOT_FOUND", "That plan was not found.")
    plan = dict(row)
    if plan["status"] != "PROPOSED":
        raise ToolError("CONFLICT", "This plan was already handled.")
    if datetime.now(timezone.utc) - plan["created_at"] > PLAN_TTL:
        await s.execute(text("UPDATE agent_plans SET status = 'EXPIRED' WHERE id = :id AND status = 'PROPOSED'"), {"id": plan_id})
        await s.commit()
        raise ToolError("CONFLICT", "This plan expired. Ask the assistant again.")
    claimed = await s.execute(text("UPDATE agent_plans SET status = :st, decided_at = now() WHERE id = :id AND status = 'PROPOSED'"),
                              {"id": plan_id, "st": "RUNNING" if decision == "approve" else "REJECTED"})
    await s.commit()
    if claimed.rowcount == 0:
        raise ToolError("CONFLICT", "This plan was already handled.")
    conv = dict((await s.execute(text("SELECT c.*, m.join_code, m.title AS meeting_title FROM agent_conversations c "
                                      "LEFT JOIN meetings m ON m.id = c.meeting_id WHERE c.id = :c"),
                                 {"c": plan["conversation_id"]})).mappings().first())

    async def say(content: str) -> int:
        return await engine._add(s, conv["id"], "ASSISTANT", content, metadata={"agent": "planner", "planId": str(plan_id)})

    async def reload() -> dict:
        return dict((await s.execute(text("SELECT * FROM agent_plans WHERE id = :id"), {"id": plan_id})).mappings().first())

    if decision != "approve":
        mid = await say("Okay — I didn't do any of it. Nothing was changed.")
        return {"ok": True, "plan": plan_view(await reload()), "message": {"id": mid, "content": "Okay — I didn't do any of it. Nothing was changed."}, "revision": None}

    now = datetime.now(timezone.utc)
    rows = await engine._rows(s, conv["id"], 200)
    t = ToolContext(s=s, user_id=a.user_id, org_id=a.org_id, user_name=a.name, user_email=a.email, timezone=a.timezone, role=a.role,
                    conversation_id=conv["id"], meeting_code=conv["join_code"], now=now, history=engine._history(rows), http=http, agent="planner")
    steps = await execute_plan(t, plan)
    done = [x for x in steps if x["status"] == "done"]
    not_done = [x for x in steps if x["status"] != "done"]
    status = "DONE" if not not_done else "FAILED" if not done else "PARTIAL"
    await s.execute(text("UPDATE agent_plans SET steps = CAST(:s AS jsonb), status = :st WHERE id = :id"),
                    {"s": json.dumps(steps), "st": status, "id": plan_id})
    await s.commit()
    mark = {"done": "✓", "skipped": "–"}
    lines = [f"{mark.get(x['status'], '✗')} {x['index']}. " + (x.get("resultSummary") or x["summary"] if x["status"] == "done" else f"{x['summary']} — {x.get('error')}")
             for x in steps]
    head = f"✓ Plan carried out: all {len(steps)} steps done." if not not_done else f"Plan partly carried out: {len(done)} of {len(steps)} steps done."
    content = head + "\n\n" + "\n".join(lines)
    mid = await say(content)

    revision = None
    if not_done and plan["revision"] < MAX_REVISIONS:
        spec = agents.BY_ID["planner"]
        describe = lambda x: f"step {x['index']} ({x['tool']}): {x['summary']}" + (f" — {x['error']}" if x.get("error") else "")  # noqa: E731
        task = "\n".join(p for p in [
            f'Part of the approved plan for "{plan["goal"]}" didn\'t work.',
            f"Already done (don't repeat): {'; '.join(map(describe, done))}." if done else "",
            f"Not done: {'; '.join(map(describe, not_done))}.",
            "Research alternatives that keep the same intent (same people, a close time, a similar place) and call propose_plan with ONLY "
            "the replacement steps. If there is no reasonable alternative, explain briefly instead.",
        ] if p)
        await engine._add(s, conv["id"], "SYSTEM", task, metadata={"handoff": {"agent": "planner", "name": spec.name, "task": task, "index": 0, "total": 1, "fallback": False}})
        org_name = (await s.execute(text("SELECT name FROM organizations WHERE id = :o"), {"o": a.org_id})).scalar()
        a.org_name = a.org_name or org_name
        meeting = {"title": conv["meeting_title"], "code": conv["join_code"]} if conv.get("join_code") else None
        system = engine.system_prompt(a, now, meeting, spec, task, [], "text", None)
        result: dict = {}
        async for _ in engine._specialist(s, http, a, conv, spec, system, f"(Coordinator) Planner, your task now: {task}", now, "text", None, result,
                                          revision_of=(plan["id"], plan["revision"])):
            pass
        if result.get("plan_id"):
            prow = dict((await s.execute(text("SELECT * FROM agent_plans WHERE id = :id"), {"id": result["plan_id"]})).mappings().first())
            revision = plan_view(prow)
            await say("Here's a revised plan for what didn't work. Nothing happens until you approve it.")
    await s.execute(text("UPDATE agent_conversations SET updated_at = now() WHERE id = :c"), {"c": conv["id"]})
    await s.commit()
    return {"ok": not not_done, "plan": plan_view(await reload()), "message": {"id": mid, "content": content}, "revision": revision}


async def _never(t, a):
    raise ToolError("INTERNAL", "propose_plan is handled by the engine.")


TOOLS = [Tool(
    "propose_plan", "plan",
    "Propose the complete plan for the user's goal, AFTER researching with your read-only tools. Each step is one action with complete, "
    "concrete arguments taken from tool results (real user ids, restaurant ids, slot start_time values, ISO times). The app checks every "
    "step and shows the whole plan for one approval; nothing runs until the user approves. If a step is rejected as invalid, fix it and "
    "call propose_plan again.",
    ProposePlanArgs, _never, label=lambda a: f"Planning: {a.goal}")]
