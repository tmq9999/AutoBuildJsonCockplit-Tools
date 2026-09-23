"""Link monetary reservations to durable request lifecycle for crash recovery."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0008_budget_request'
down_revision='0007_admin_versions'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('upstream_reservations',sa.Column('request_id',pg.UUID(as_uuid=True),sa.ForeignKey('requests.id')))
    op.create_index('upstream_reservation_request','upstream_reservations',['request_id'])


def downgrade():
    raise RuntimeError('Restore a verified backup instead')
