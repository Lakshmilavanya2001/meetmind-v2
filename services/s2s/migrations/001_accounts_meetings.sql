-- Accounts, organisations and meetings.
CREATE TABLE organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    slug        text NOT NULL UNIQUE,
    join_code   text NOT NULL UNIQUE,              -- people sign up into the org with it
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email               text NOT NULL,
    display_name        text NOT NULL,
    password_hash       text NOT NULL,             -- argon2id
    timezone            text NOT NULL DEFAULT 'Asia/Kolkata',
    preferred_language  text NOT NULL DEFAULT 'en',
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_lower ON users (lower(email));

CREATE TABLE memberships (
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role             text NOT NULL CHECK (role IN ('OWNER', 'ADMIN', 'MEMBER')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash       text NOT NULL UNIQUE,          -- sha256 of the cookie value; the value itself is never stored
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE meetings (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    host_id          uuid NOT NULL REFERENCES users(id),
    title            text NOT NULL,
    join_code        text NOT NULL UNIQUE,          -- abc-defg-hij, in links
    room_name        text NOT NULL UNIQUE,          -- the LiveKit room
    status           text NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'LIVE', 'ENDED', 'CANCELLED')),
    scheduled_start  timestamptz,
    scheduled_end    timestamptz,
    started_at       timestamptz,
    ended_at         timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (scheduled_end IS NULL OR scheduled_start IS NULL OR scheduled_end > scheduled_start)
);
CREATE INDEX meetings_org_start ON meetings (organization_id, scheduled_start);

CREATE TABLE meeting_participants (
    meeting_id       uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role             text NOT NULL DEFAULT 'ATTENDEE' CHECK (role IN ('HOST', 'COHOST', 'ATTENDEE')),
    invited_at       timestamptz,
    first_joined_at  timestamptz,
    last_joined_at   timestamptz,
    removed_at       timestamptz,
    speak            text,                          -- call preferences remembered for this meeting
    hear             text,
    voice            text,
    noise            text,
    PRIMARY KEY (meeting_id, user_id)
);
CREATE INDEX meeting_participants_user ON meeting_participants (user_id);

CREATE TABLE audit_log (
    id               bigserial PRIMARY KEY,
    organization_id  uuid REFERENCES organizations(id) ON DELETE CASCADE,
    actor_id         uuid REFERENCES users(id) ON DELETE SET NULL,
    action           text NOT NULL,
    target_type      text,
    target_id        text,
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_org_time ON audit_log (organization_id, created_at);
