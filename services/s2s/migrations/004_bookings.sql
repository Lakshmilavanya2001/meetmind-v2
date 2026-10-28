-- Bookings made through the assistant: food orders, cabs, restaurant tables and movie tickets.
-- The sandbox provider places no real order and charges nothing.
CREATE TABLE bookings (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('FOOD', 'CAB', 'TABLE', 'MOVIE')),
    provider         text NOT NULL,                  -- "sandbox"
    reference        text NOT NULL,
    status           text NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'CANCELED')),
    title            text NOT NULL,                  -- e.g. "Order from Dosa Corner"
    details          jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_paise      integer,
    scheduled_for    timestamptz,                    -- delivery eta, pickup, table or show time
    meeting_id       uuid REFERENCES meetings(id) ON DELETE SET NULL,
    canceled_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bookings_user ON bookings (user_id, created_at DESC);
-- Seats taken per table slot and per show (only confirmed bookings count).
CREATE INDEX bookings_table_slots ON bookings ((details->>'restaurantId'), scheduled_for)
    WHERE kind = 'TABLE' AND status = 'CONFIRMED';
CREATE INDEX bookings_movie_shows ON bookings ((details->>'showId'))
    WHERE kind = 'MOVIE' AND status = 'CONFIRMED';
