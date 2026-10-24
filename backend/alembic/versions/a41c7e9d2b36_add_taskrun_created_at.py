"""add task_runs.created_at

Revision ID: a41c7e9d2b36
Revises: 5952156ac6c7
Create Date: 2026-10-24 12:00:00.000000

Adds TaskRun.created_at so queue wait (started_at - created_at) can be
measured for the Week 8 bottleneck predictor.

The column is added nullable WITHOUT a default first, then the default is
set. Adding it with the default in one step would make Postgres stamp every
existing row with the migration time, which would be a made-up value. Existing
rows stay NULL; only rows inserted after this migration get a timestamp.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a41c7e9d2b36'
down_revision: Union[str, Sequence[str], None] = '5952156ac6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('task_runs', sa.Column('created_at', sa.DateTime(), nullable=True))
    op.alter_column(
        'task_runs',
        'created_at',
        server_default=sa.text("timezone('utc', clock_timestamp())"),
    )


def downgrade() -> None:
    op.drop_column('task_runs', 'created_at')
