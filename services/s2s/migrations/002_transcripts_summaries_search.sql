-- Saved transcripts (only while the host has "Save transcript" on), their translations, the
-- AI summary with action items, and the search index (pgvector).
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE meetings
    ADD COLUMN save_transcript boolean NOT NULL DEFAULT false,
    ADD COLUMN transcript_saving_since timestamptz,         -- when saving was last turned on
    ADD COLUMN indexed_at timestamptz,                       -- search index up to date as of this time
    ADD COLUMN transcript_updated_at timestamptz;            -- last line saved

CREATE TABLE transcript_segments (
    id               bigserial PRIMARY KEY,
    meeting_id       uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
    speaker_name     text NOT NULL,
    language         text NOT NULL,
    text             text NOT NULL,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz NOT NULL,
    utterance_key    text,                                   -- the agent's utterance id: translations attach to it
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (meeting_id, utterance_key)
);
CREATE INDEX transcript_segments_meeting_time ON transcript_segments (meeting_id, started_at);

CREATE TABLE segment_translations (
    segment_id  bigint NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
    language    text NOT NULL,
    text        text NOT NULL,
    PRIMARY KEY (segment_id, language)
);

CREATE TABLE meeting_summaries (
    meeting_id     uuid PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    summary        text NOT NULL,
    decisions      jsonb NOT NULL DEFAULT '[]'::jsonb,       -- [{text, segments:[ids]}]
    action_items   jsonb NOT NULL DEFAULT '[]'::jsonb,       -- [{task, owner, ownerUserId, due, segments:[ids]}]
    segment_count  integer NOT NULL,                         -- how many lines it was made from
    model          text,
    generated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE transcript_chunks (
    id               bigserial PRIMARY KEY,
    meeting_id       uuid NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    organization_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    chunk_index      integer NOT NULL,
    content          text NOT NULL,                          -- "[mm:ss] Speaker: text" lines
    first_segment_id bigint,
    last_segment_id  bigint,
    embedding        halfvec(2048),
    UNIQUE (meeting_id, chunk_index)
);
-- HNSW on plain vector stops at 2000 dimensions; the embedding model gives 2048.
CREATE INDEX transcript_chunks_embedding ON transcript_chunks USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX transcript_chunks_org ON transcript_chunks (organization_id);
