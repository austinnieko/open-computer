"""add multi-agent coordination tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coord_tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("repo_root", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("agent_session_id", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("base_ref", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.BigInteger(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("pr_state", sa.Text(), nullable=True),
        sa.Column("files", sqlite.JSON(), nullable=True),
        sa.Column("validation", sqlite.JSON(), nullable=True),
        sa.Column("blockers", sqlite.JSON(), nullable=True),
        sa.Column("meta", sqlite.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coord_tasks_repo_status", "coord_tasks", ["repo_key", "status"])
    op.create_index("ix_coord_tasks_chat", "coord_tasks", ["chat_id"])

    op.create_table(
        "coord_task_deps",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("depends_on_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "depends_on_id"),
    )

    op.create_table(
        "coord_claims",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_key", "resource", name="uq_coord_claim_resource"),
    )
    op.create_index("ix_coord_claims_task", "coord_claims", ["task_id"])

    op.create_table(
        "coord_conflicts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("task_a", sa.Text(), nullable=False),
        sa.Column("task_b", sa.Text(), nullable=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("evidence", sqlite.JSON(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_coord_conflict_fingerprint"),
    )
    op.create_index(
        "ix_coord_conflicts_repo_status", "coord_conflicts", ["repo_key", "status"]
    )

    op.create_table(
        "coord_messages",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=False),
        sa.Column("from_task", sa.Text(), nullable=True),
        sa.Column("to_task", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", sqlite.JSON(), nullable=True),
        sa.Column("delivered_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coord_messages_to", "coord_messages", ["to_task", "delivered_at"])


def downgrade() -> None:
    op.drop_index("ix_coord_messages_to", table_name="coord_messages")
    op.drop_table("coord_messages")
    op.drop_index("ix_coord_conflicts_repo_status", table_name="coord_conflicts")
    op.drop_table("coord_conflicts")
    op.drop_index("ix_coord_claims_task", table_name="coord_claims")
    op.drop_table("coord_claims")
    op.drop_table("coord_task_deps")
    op.drop_index("ix_coord_tasks_chat", table_name="coord_tasks")
    op.drop_index("ix_coord_tasks_repo_status", table_name="coord_tasks")
    op.drop_table("coord_tasks")
