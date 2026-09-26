"""Persistent all-account quota refresh jobs and opt-in schedule."""
from alembic import op

revision = "0014_codex_quota_refresh"
down_revision = "0013_codex_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE codex_quota_refresh_settings (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        version integer NOT NULL DEFAULT 1 CHECK(version >= 1),
        interval_minutes integer NOT NULL DEFAULT 0 CHECK(interval_minutes BETWEEN 0 AND 999),
        next_run_at timestamptz
    )""")
    op.execute("""CREATE TABLE codex_quota_refresh_runs (
        id uuid PRIMARY KEY,
        trigger text NOT NULL CHECK(trigger IN ('manual','automatic')),
        state text NOT NULL CHECK(state IN ('queued','running','completed','stopped','interrupted','failed')),
        total integer NOT NULL DEFAULT 0 CHECK(total >= 0),
        succeeded integer NOT NULL DEFAULT 0 CHECK(succeeded >= 0),
        failed integer NOT NULL DEFAULT 0 CHECK(failed >= 0),
        skipped integer NOT NULL DEFAULT 0 CHECK(skipped >= 0),
        stop_requested boolean NOT NULL DEFAULT false,
        errors jsonb NOT NULL DEFAULT '[]'::jsonb,
        result_code text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        started_at timestamptz,
        finished_at timestamptz,
        CHECK(succeeded + failed + skipped <= total)
    )""")
    op.execute("CREATE UNIQUE INDEX codex_quota_refresh_one_active ON codex_quota_refresh_runs "
               "((true)) WHERE state IN ('queued','running')")
    op.execute("CREATE INDEX codex_quota_refresh_recent ON codex_quota_refresh_runs(created_at DESC)")


def downgrade():
    raise RuntimeError("Quota refresh rollback requires an explicit backup/restore procedure")
