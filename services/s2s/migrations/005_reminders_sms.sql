-- Reminders (for yourself), in-app notifications, text messages (SMS) and phone verification.

-- Your mobile number (E.164, e.g. +919812345678). Changing it clears the verification and turns
-- SMS reminders off. phone_verified_via is the SMS provider that verified it ("twilio" or "outbox"):
-- a number verified through the dev outbox never gets real texts.
ALTER TABLE users
    ADD COLUMN phone_number        text,
    ADD COLUMN phone_verified_at   timestamptz,
    ADD COLUMN phone_verified_via  text,
    ADD COLUMN sms_reminders       boolean NOT NULL DEFAULT false;   -- text reminders by default

CREATE TABLE reminders (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text             text NOT NULL,
    due_at           timestamptz NOT NULL,
    sms              boolean NOT NULL DEFAULT false,
    -- PENDING -> SENDING (claimed by a worker) -> SENT or FAILED; CANCELED by the user.
    status           text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELED')),
    source           text NOT NULL DEFAULT 'user',      -- 'user' | 'assistant' | 'action_item'
    meeting_id       uuid REFERENCES meetings(id) ON DELETE SET NULL,
    action_item_id   text UNIQUE,                       -- one reminder per action item
    sent_at          timestamptz,                       -- claim time while SENDING, then delivery time
    sms_status       text,                              -- 'sent' | 'outbox' | 'failed: …' | 'skipped: …'
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reminders_due ON reminders (status, due_at);
CREATE INDEX reminders_user ON reminders (user_id, status, due_at);

CREATE TABLE notifications (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        text NOT NULL,                          -- e.g. 'reminder'
    title       text NOT NULL,
    body        text,
    link        text,
    data        jsonb,
    read_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notifications_user ON notifications (user_id, read_at, created_at DESC);

CREATE TABLE sms_messages (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid REFERENCES users(id) ON DELETE SET NULL,
    to_number    text NOT NULL,
    body         text NOT NULL,                         -- verification codes are masked unless this is the dev outbox
    purpose      text NOT NULL,                         -- 'verification' | 'reminder' | 'summary'
    provider     text NOT NULL,                         -- 'twilio' | 'outbox' | 'none'
    provider_id  text,
    status       text NOT NULL CHECK (status IN ('OUTBOX', 'SENT', 'FAILED')),
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sms_messages_user ON sms_messages (user_id, created_at DESC);
CREATE INDEX sms_messages_to ON sms_messages (to_number, purpose, created_at);
CREATE INDEX sms_messages_created ON sms_messages (created_at);

-- One pending code per user: only its sha256 is stored; 5 wrong attempts and it is spent.
CREATE TABLE phone_verifications (
    user_id     uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    phone       text NOT NULL,
    code_hash   text NOT NULL,
    attempts    integer NOT NULL DEFAULT 0,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
