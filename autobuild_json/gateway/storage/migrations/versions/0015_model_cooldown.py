"""Account/model-specific rejection cooldown without disabling other models."""
from alembic import op

revision = "0015_model_cooldown"
down_revision = "0014_codex_quota_refresh"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE model_bindings ADD COLUMN cooldown_until timestamptz")


def downgrade():
    raise RuntimeError("Model cooldown rollback requires an explicit backup/restore procedure")
