"""add_device_secret_and_leave_balances

Revision ID: 2faa2b6cb609
Revises: c44a8ae6bf4d
Create Date: 2026-05-25 16:08:41.713507

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2faa2b6cb609'
down_revision: Union[str, Sequence[str], None] = 'c44a8ae6bf4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add column device_secret to device_bindings table
    op.add_column('device_bindings', sa.Column('device_secret', sa.String(length=100), nullable=True))
    
    # Create leave_balances table
    op.create_table('leave_balances',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.String(length=50), nullable=False),
    sa.Column('annual_total', sa.Integer(), nullable=True, server_default='12'),
    sa.Column('annual_used', sa.Integer(), nullable=True, server_default='0'),
    sa.Column('sick_total', sa.Integer(), nullable=True, server_default='12'),
    sa.Column('sick_used', sa.Integer(), nullable=True, server_default='0'),
    sa.Column('year', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.employee_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id', 'year', name='uq_employee_leave_year')
    )
    op.create_index(op.f('ix_leave_balances_id'), 'leave_balances', ['id'], unique=False)
    op.create_index(op.f('ix_leave_balances_employee_id'), 'leave_balances', ['employee_id'], unique=False)

    # Create overtime_requests table
    op.create_table('overtime_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.String(length=50), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('hours_requested', sa.Float(), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True, server_default='pending'),
    sa.Column('approved_by', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.employee_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_overtime_requests_id'), 'overtime_requests', ['id'], unique=False)
    op.create_index(op.f('ix_overtime_requests_employee_id'), 'overtime_requests', ['employee_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_overtime_requests_employee_id'), table_name='overtime_requests')
    op.drop_index(op.f('ix_overtime_requests_id'), table_name='overtime_requests')
    op.drop_table('overtime_requests')
    op.drop_index(op.f('ix_leave_balances_employee_id'), table_name='leave_balances')
    op.drop_index(op.f('ix_leave_balances_id'), table_name='leave_balances')
    op.drop_table('leave_balances')
    op.drop_column('device_bindings', 'device_secret')
