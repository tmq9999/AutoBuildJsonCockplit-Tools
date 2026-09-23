"""Identity schema; migration history, not create_all, owns database installation."""
from sqlalchemy import Column, MetaData, Table, Text, Boolean, Integer, LargeBinary, ForeignKey, DateTime, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

metadata = MetaData()
customers = Table("customers", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
api_keys = Table("api_keys", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("customer_id", UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False),
    Column("prefix", Text, nullable=False),
    Column("secret_digest", LargeBinary, nullable=False, unique=True),
    Column("name", Text, nullable=False, server_default=""),
    Column("policy", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
audit_events = Table("audit_events", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("record_id", UUID(as_uuid=True), nullable=False),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
