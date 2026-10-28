-- The assistant: conversations (text chat and "Hey Mind" in calls), their messages, actions that
-- wait for the user's confirmation, and the Planner's plans.
CREATE TABLE agent_conversations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    meeting_id       uuid REFERENCES meetings(id) ON DELETE SET NULL,    -- attached to a call ("this meeting")
    title            text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_conversations_user ON agent_conversations (user_id, updated_at DESC);

CREATE TABLE agent_messages (
    id               bigserial PRIMARY KEY,
    conversation_id  uuid NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
    role             text NOT NULL CHECK (role IN ('USER', 'ASSISTANT', 'TOOL', 'SYSTEM')),
    content          text NOT NULL DEFAULT '',
    tool_calls       jsonb,                          -- the model's tool calls (ASSISTANT)
    tool_call_id     text,                           -- which call this answers (TOOL)
    tool_name        text,
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_messages_conversation ON agent_messages (conversation_id, id);

CREATE TABLE agent_actions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid REFERENCES agent_conversations(id) ON DELETE CASCADE,
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tool_name        text NOT NULL,
    arguments        jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary          text,                           -- what the user is asked to confirm
    status           text NOT NULL CHECK (status IN ('PENDING_CONFIRMATION', 'SUCCEEDED', 'FAILED', 'REJECTED', 'EXPIRED')),
    result           jsonb,
    error            text,
    agent            text,
    duration_ms      integer,
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz
);
CREATE INDEX agent_actions_user ON agent_actions (user_id, created_at DESC);

CREATE TABLE agent_plans (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    goal             text NOT NULL,
    steps            jsonb NOT NULL,                 -- [{index, tool, args, summary, why, depends_on, status, result_summary, error, link}]
    status           text NOT NULL CHECK (status IN ('PROPOSED', 'RUNNING', 'DONE', 'PARTIAL', 'FAILED', 'REJECTED', 'EXPIRED')),
    parent_id        uuid REFERENCES agent_plans(id) ON DELETE SET NULL,
    revision         integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz
);
