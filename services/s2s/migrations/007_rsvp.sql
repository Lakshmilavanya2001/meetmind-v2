-- Invitation replies: each participant accepts or declines (the host has accepted). A declined meeting
-- doesn't count as busy in availability and conflict checks.
ALTER TABLE meeting_participants
    ADD COLUMN rsvp text NOT NULL DEFAULT 'PENDING' CHECK (rsvp IN ('PENDING', 'ACCEPTED', 'DECLINED')),
    ADD COLUMN responded_at timestamptz;
UPDATE meeting_participants SET rsvp = 'ACCEPTED' WHERE role = 'HOST';
