"""the assistant's own database: memory_items (notes, learned things, diary, the owner's private journal), and profile_traits

Revision ID: 0009
Revises: 0008

`text` holds the Fernet ciphertext of the entry when `enc` is true (the app encrypts everything it writes; rows copied from the
older JSON storage start unencrypted and are encrypted by the app on its next run). `shared` says whether the assistant (and so
the AI provider) may read the entry: journal entries are private by default, so they are never sent anywhere. There is no text
index on purpose: it would index the ciphertext, and search runs in the app after decrypting.

Idempotent (a database created from the current models already has the tables). Notes, learned items and diary entries that
were kept as JSON in policy_rules are copied into memory_items once, when the table is empty. The runtime role may DELETE
here (the owner deletes what the assistant remembers), unlike on the ledger tables.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS memory_items (
            id BIGSERIAL PRIMARY KEY,
            kind VARCHAR(12) NOT NULL,
            text TEXT NOT NULL,
            day DATE,
            source VARCHAR(8) NOT NULL DEFAULT 'owner',
            enc BOOLEAN NOT NULL DEFAULT false,
            shared BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_memory_items_kind_day ON memory_items (kind, day)")
    op.execute(
        """CREATE TABLE IF NOT EXISTS profile_traits (
            key VARCHAR(48) PRIMARY KEY,
            summary TEXT NOT NULL,
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence INTEGER NOT NULL DEFAULT 0,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apa_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON memory_items, profile_traits TO apa_app;
                GRANT USAGE, SELECT ON SEQUENCE memory_items_id_seq TO apa_app;
            END IF;
        END $$"""
    )
    # copy what was kept as JSON before: notes, learned items, diary (only into an empty table)
    op.execute(
        """INSERT INTO memory_items (kind, text, source)
           SELECT 'note', f, 'owner' FROM policy_rules, jsonb_array_elements_text(value_json->'facts') AS f
           WHERE rule_key = 'assistant_memory' AND NOT EXISTS (SELECT 1 FROM memory_items)"""
    )
    op.execute(
        """INSERT INTO memory_items (kind, text, day, source)
           SELECT 'diary', e->>'summary', (e->>'date')::date, 'auto'
           FROM policy_rules, jsonb_array_elements(value_json->'episodes') AS e
           WHERE rule_key = 'assistant_diary' AND e->>'summary' IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM memory_items WHERE kind = 'diary')"""
    )
    op.execute(
        """INSERT INTO memory_items (kind, text, day, source)
           SELECT CASE WHEN i->>'kind' IN ('person','preference','goal','routine','plan','health') THEN i->>'kind' ELSE 'other' END,
                  i->>'text', NULLIF(i->>'date', '')::date, 'auto'
           FROM policy_rules, jsonb_array_elements(value_json->'items') AS i
           WHERE rule_key = 'assistant_learned' AND i->>'text' IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM memory_items WHERE kind NOT IN ('note','diary'))"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS profile_traits")
    op.execute("DROP TABLE IF EXISTS memory_items")
