"""add_phase_5_models

Revision ID: c44a8ae6bf4d
Revises: 79db5f5796e0
Create Date: 2026-05-25 13:06:37.743089

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c44a8ae6bf4d'
down_revision: Union[str, Sequence[str], None] = '79db5f5796e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create webhooks table (Phase 5F)
    op.create_table('webhooks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('events', sa.String(length=500), nullable=True),
    sa.Column('secret', sa.String(length=200), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_webhooks_id'), 'webhooks', ['id'], unique=False)
    
    # Create webhook_deliveries table (Phase 5F)
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('webhook_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.String(length=100), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.ForeignKeyConstraint(['webhook_id'], ['webhooks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_webhook_deliveries_id'), 'webhook_deliveries', ['id'], unique=False)
    
    # Create schedule_assignments table (Phase 5D)
    op.create_table('schedule_assignments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.String(length=50), nullable=False),
    sa.Column('shift_schedule_id', sa.Integer(), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('created_by', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.employee_id'], ),
    sa.ForeignKeyConstraint(['shift_schedule_id'], ['shift_schedules.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_schedule_assignment_lookup', 'schedule_assignments', ['employee_id', 'effective_date'], unique=False)
    op.create_index(op.f('ix_schedule_assignments_employee_id'), 'schedule_assignments', ['employee_id'], unique=False)
    op.create_index(op.f('ix_schedule_assignments_id'), 'schedule_assignments', ['id'], unique=False)
    
    # Add columns to existing tables
    op.add_column('branches', sa.Column('timezone_name', sa.String(length=50), nullable=True))
    op.add_column('punch_logs', sa.Column('is_auto_generated', sa.Boolean(), server_default=sa.text('false'), nullable=True))
    
    op.add_column('shift_schedules', sa.Column('overtime_multiplier_1', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('overtime_multiplier_2', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('overtime_threshold_2_hours', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('weekend_overtime_multiplier', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('holiday_overtime_multiplier', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('monthly_overtime_cap_hours', sa.Float(), nullable=True))
    op.add_column('shift_schedules', sa.Column('auto_clockout_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=True))
    op.add_column('shift_schedules', sa.Column('auto_clockout_buffer_minutes', sa.Integer(), server_default=sa.text('60'), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('shift_schedules', 'auto_clockout_buffer_minutes')
    op.drop_column('shift_schedules', 'auto_clockout_enabled')
    op.drop_column('shift_schedules', 'monthly_overtime_cap_hours')
    op.drop_column('shift_schedules', 'holiday_overtime_multiplier')
    op.drop_column('shift_schedules', 'weekend_overtime_multiplier')
    op.drop_column('shift_schedules', 'overtime_threshold_2_hours')
    op.drop_column('shift_schedules', 'overtime_multiplier_2')
    op.drop_column('shift_schedules', 'overtime_multiplier_1')
    
    op.drop_column('punch_logs', 'is_auto_generated')
    op.drop_column('branches', 'timezone_name')
    
    op.drop_index(op.f('ix_schedule_assignments_id'), table_name='schedule_assignments')
    op.drop_index(op.f('ix_schedule_assignments_employee_id'), table_name='schedule_assignments')
    op.drop_index('idx_schedule_assignment_lookup', table_name='schedule_assignments')
    op.drop_table('schedule_assignments')
    
    op.drop_index(op.f('ix_webhook_deliveries_id'), table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    
    op.drop_index(op.f('ix_webhooks_id'), table_name='webhooks')
    op.drop_table('webhooks')
