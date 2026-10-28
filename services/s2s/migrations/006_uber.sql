-- Uber (Riders API): each user's own connection (OAuth tokens, encrypted at rest with ENCRYPTION_KEY)
-- and the rides booked through it. Test mode (Uber's sandbox) unless UBER_ENV=production.
CREATE TABLE uber_connections (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status            text NOT NULL CHECK (status IN ('ACTIVE', 'ERROR')),   -- ERROR: the login was refused, reconnect
    encrypted_tokens  text NOT NULL,                  -- app.crypto: {"accessToken", "refreshToken"}
    scopes            text[] NOT NULL DEFAULT '{}',
    expires_at        timestamptz,                    -- when the access token expires
    env               text NOT NULL CHECK (env IN ('sandbox', 'production')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id)
);

CREATE TABLE uber_rides (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id        text NOT NULL,                  -- Uber's ride id
    product_id        text NOT NULL,
    product           text NOT NULL,                  -- its name, e.g. UberGo
    pickup            text NOT NULL,
    drop_off          text NOT NULL,
    fare              text,                           -- as Uber showed it, e.g. ₹265
    fare_paise        integer,                        -- when the fare was in INR
    ride_status       text NOT NULL DEFAULT 'processing',   -- Uber's: processing, accepted, arriving, in_progress, completed, ...
    driver            text,
    vehicle           text,
    eta_min           integer,
    env               text NOT NULL CHECK (env IN ('sandbox', 'production')),
    status            text NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'CANCELED')),
    meeting_id        uuid REFERENCES meetings(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    canceled_at       timestamptz
);
CREATE INDEX uber_rides_user ON uber_rides (user_id, created_at DESC);
