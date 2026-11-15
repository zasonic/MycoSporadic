"""telemetry dedupe is per boot session, not per device forever

A device reboot legitimately restarts its sequence counter. Without boot_id the
unique (device_id, seq) constraint silently rejects every reading after the first
reboot — the chamber keeps running while the backend stops seeing it.

Revision ID: 0002_telemetry_boot_id
Revises: 0001_initial
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_telemetry_boot_id"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("telemetry_raw") as batch:
        batch.add_column(sa.Column("boot_id", sa.String(length=40), nullable=False, server_default="0"))
        batch.drop_constraint("uq_telemetry_device_seq", type_="unique")
        batch.create_unique_constraint("uq_telemetry_device_boot_seq", ["device_id", "boot_id", "seq"])


def downgrade() -> None:
    with op.batch_alter_table("telemetry_raw") as batch:
        batch.drop_constraint("uq_telemetry_device_boot_seq", type_="unique")
        batch.create_unique_constraint("uq_telemetry_device_seq", ["device_id", "seq"])
        batch.drop_column("boot_id")
