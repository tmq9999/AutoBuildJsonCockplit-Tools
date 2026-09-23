"""OAuth identity, token-generation fences and refresh lifecycle."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0006_oauth"
down_revision = "0005_continuations"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("issuer", sa.Text()), sa.Column("account_id", sa.Text()), sa.Column("email", sa.Text()),
        sa.Column("profile_id", pg.UUID(as_uuid=True)),
        sa.Column("token_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_owner", pg.UUID(as_uuid=True)),
    ):
        op.add_column("credentials", column)
    op.create_unique_constraint("credentials_oauth_identity", "credentials", ["issuer", "account_id"])


def downgrade():
    raise RuntimeError("Credential rollback requires explicit backup/restore")
