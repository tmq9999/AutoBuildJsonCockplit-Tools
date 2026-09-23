"""Shared provider/credential admission rate and active-slot accounting."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision='0009_provider_admission'
down_revision='0008_budget_request'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('credentials',sa.Column('rpm_limit',sa.Integer(),nullable=False,server_default='600'))
    op.add_column('credentials',sa.Column('concurrency_limit',sa.Integer(),nullable=False,server_default='16'))
    op.create_table('provider_admissions',sa.Column('id',pg.UUID(as_uuid=True),primary_key=True),
        sa.Column('provider_id',pg.UUID(as_uuid=True),sa.ForeignKey('providers.id'),nullable=False),
        sa.Column('credential_id',pg.UUID(as_uuid=True),sa.ForeignKey('credentials.id'),nullable=False),
        sa.Column('admitted_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.text('now()')),
        sa.Column('deadline',sa.DateTime(timezone=True),nullable=False),
        sa.Column('active',sa.Boolean(),nullable=False,server_default=sa.text('true')))
    op.create_index('provider_admissions_window','provider_admissions',['provider_id','admitted_at'])


def downgrade():
    raise RuntimeError('Explicit backup/restore required')
