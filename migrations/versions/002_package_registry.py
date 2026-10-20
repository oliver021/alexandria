from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_package_registry"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ROLES = ("admin", "publisher")
SKILL_STATUSES = ("draft", "published", "unpublished")
VERSION_STATUSES = ("draft", "published", "deprecated")


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    postgresql = _is_postgresql()

    op.drop_index("idx_skill_version_status", table_name="skill_versions")
    op.drop_index("idx_skill_version_skill", table_name="skill_versions")
    op.drop_table("skill_versions")

    if postgresql:
        op.alter_column(
            "users",
            "role",
            existing_type=sa.Enum(*USER_ROLES, name="userrole"),
            type_=sa.String(9),
            existing_nullable=False,
            postgresql_using="role::text",
        )
        op.alter_column(
            "skills",
            "status",
            existing_type=sa.Enum(*SKILL_STATUSES, name="skillstatus"),
            type_=sa.String(11),
            existing_nullable=False,
            postgresql_using="status::text",
        )
        op.execute("DROP TYPE userrole")
        op.execute("DROP TYPE skillstatus")
        op.execute("DROP TYPE versionstatus")

    op.execute("UPDATE users SET role = lower(role)")
    op.execute("UPDATE skills SET status = lower(status)")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    with op.batch_alter_table("skills") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(9), nullable=False, server_default="skill"))
        batch_op.add_column(sa.Column("category", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("latest_version", sa.String(50), nullable=True))
        batch_op.add_column(sa.Column("search_text", sa.Text, nullable=True))
        batch_op.drop_index("idx_skill_name", if_exists=True)
        batch_op.create_index("ix_skills_name", ["name"], unique=True, if_not_exists=True)
        batch_op.create_index("ix_skills_kind", ["kind"])
        batch_op.create_index("ix_skills_category", ["category"])

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("blob_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("uncompressed_bytes", sa.BigInteger, nullable=False),
        sa.Column("file_count", sa.Integer, nullable=False),
        sa.Column("entrypoint", sa.String(16), nullable=False),
        sa.Column("root_prefix", sa.String(255), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("tags", sa.Text, nullable=False),
        sa.Column("metadata", sa.Text, nullable=False),
        sa.Column("manifest", sa.Text, nullable=False),
        sa.Column("search_text", sa.Text, nullable=False),
        sa.Column("uploaded_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_id_version"),
    )
    op.create_index("ix_skill_versions_blob_sha256", "skill_versions", ["blob_sha256"])

    op.create_table(
        "skill_tags",
        sa.Column(
            "skill_id",
            sa.String(36),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("skill_id", "tag"),
    )
    op.create_index("ix_skill_tags_tag", "skill_tags", ["tag"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(5), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    postgresql = _is_postgresql()

    op.drop_table("api_tokens")
    op.drop_table("skill_tags")
    op.drop_table("skill_versions")

    with op.batch_alter_table("skills") as batch_op:
        batch_op.drop_index("ix_skills_category")
        batch_op.drop_index("ix_skills_kind")
        batch_op.drop_index("ix_skills_name")
        batch_op.drop_column("search_text")
        batch_op.drop_column("latest_version")
        batch_op.drop_column("category")
        batch_op.drop_column("kind")
        batch_op.create_index("idx_skill_name", ["name"])

    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_email", "users", ["email"])

    if postgresql:
        op.execute("CREATE TYPE userrole AS ENUM ('admin', 'publisher')")
        op.execute("CREATE TYPE skillstatus AS ENUM ('draft', 'published', 'unpublished')")
        op.alter_column(
            "users",
            "role",
            existing_type=sa.String(9),
            type_=sa.Enum(*USER_ROLES, name="userrole"),
            existing_nullable=False,
            postgresql_using="role::userrole",
        )
        op.alter_column(
            "skills",
            "status",
            existing_type=sa.String(11),
            type_=sa.Enum(*SKILL_STATUSES, name="skillstatus"),
            existing_nullable=False,
            postgresql_using="status::skillstatus",
        )

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("capabilities", sa.Text, nullable=True),
        sa.Column("endpoints", sa.Text, nullable=True),
        sa.Column("metadata", sa.Text, nullable=True),
        sa.Column("tags", sa.Text, nullable=True),
        sa.Column("status", sa.Enum(*VERSION_STATUSES, name="versionstatus"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("idx_skill_version_skill", "skill_versions", ["skill_id"])
    op.create_index("idx_skill_version_status", "skill_versions", ["status"])
