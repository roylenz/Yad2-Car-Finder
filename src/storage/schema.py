"""Database tables. Alembic migrations in `storage/migrations` create and change them."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

searches = Table(
    "searches",
    metadata,
    Column("chat_id", Integer, primary_key=True),
    Column("url", Text, nullable=False),
    Column("interval_minutes", Integer, nullable=False),
)

access = Table(
    "access",
    metadata,
    Column("user_id", Integer, primary_key=True),
    Column("allowed", Integer, nullable=False),
)

admins = Table("admins", metadata, Column("user_id", Integer, primary_key=True))

access_requests = Table(
    "access_requests",
    metadata,
    Column("user_id", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False),
)

access_meta = Table("access_meta", metadata, Column("key", Text, primary_key=True))

# One row per Yad2 ad, shared by every chat's search.
ads = Table(
    "ads",
    metadata,
    Column("token", String(32), primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("price", Integer),
    Column("km", Integer),
    Column("hand", Integer),
    Column("year", Integer),
    Column("manufacturer", Text),
    Column("model", Text),
    Column("sub_model", Text),
    Column("attributes", JSON),
    Column("raw", JSON),
    Column("listed_at", DateTime),  # Yad2 creation time (UTC)
    Column("first_seen_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    Column("removed_at", DateTime),
    Column("details_fetched_at", DateTime),
)

# Price and parameter history. A row is written only when something changes.
ad_snapshots = Table(
    "ad_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token", String(32), ForeignKey("ads.token"), nullable=False),
    Column("observed_at", DateTime, nullable=False),
    Column("price", Integer),
    Column("km", Integer),
    Column("changes", JSON),  # {field: [old, new]}; null on first sighting
    Index("ix_ad_snapshots_token", "token", "observed_at"),
)

photos = Table(
    "photos",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token", String(32), ForeignKey("ads.token"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("sha256", String(64)),  # null until downloaded
    Column("path", Text),  # relative to the photo store root
    Column("size_bytes", Integer),
    Column("first_seen_at", DateTime, nullable=False),
    Column("removed_from_ad_at", DateTime),
    Column("download_attempts", Integer, nullable=False, default=0),
    UniqueConstraint("token", "source_url", name="uq_photos_token_source"),
    Index("ix_photos_sha256", "sha256"),
)

# Which ads each chat has been told about, and the price it was last told.
chat_ads = Table(
    "chat_ads",
    metadata,
    Column("chat_id", Integer, primary_key=True),
    Column("token", String(32), ForeignKey("ads.token"), primary_key=True),
    Column("last_price", Integer),
    Column("in_search", Boolean, nullable=False, default=True),
    Column("first_seen_at", DateTime, nullable=False),
)

# Output of pluggable processors (see `processing/`), one row per target and processor version.
analysis_results = Table(
    "analysis_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("target_type", String(16), nullable=False),  # "ad" or "photo"
    Column("target_id", String(64), nullable=False),  # ad token or photo id
    Column("processor", String(64), nullable=False),
    Column("version", String(32), nullable=False),
    Column("status", String(16), nullable=False),  # "ok" or "error"
    Column("result", JSON),
    Column("error", Text),
    Column("attempts", Integer, nullable=False, default=1),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint(
        "target_type", "target_id", "processor", "version", name="uq_analysis_target_version"
    ),
)
