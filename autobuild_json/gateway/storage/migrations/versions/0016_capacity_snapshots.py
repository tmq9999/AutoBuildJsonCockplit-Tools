"""Redacted serving-process capacity telemetry for the separate admin process."""
from alembic import op

revision = "0016_capacity_snapshots"
down_revision = "0015_model_cooldown"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE gateway_capacity_snapshots (
            id smallint PRIMARY KEY CHECK (id = 1),
            snapshot jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
    """)


def downgrade():
    raise RuntimeError("Capacity snapshot rollback requires an explicit backup/restore procedure")
