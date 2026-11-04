"""add coordinator integration runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coord_integrations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("task_order", sqlite.JSON(), nullable=True),
        sa.Column("steps", sqlite.JSON(), nullable=True),
        sa.Column("tests", sqlite.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coord_integrations_repo", "coord_integrations", ["repo_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_coord_integrations_repo", table_name="coord_integrations")
    op.drop_table("coord_integrations")
