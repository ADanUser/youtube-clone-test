"""Establish the initial migration baseline.

Revision ID: 20261109_0001
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

revision: str = "20261109_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reserve the baseline; domain tables start in TODO step 6."""


def downgrade() -> None:
    """Return to the pre-migration state."""
