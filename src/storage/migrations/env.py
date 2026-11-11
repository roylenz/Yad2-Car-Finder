"""Alembic environment. Run through `storage.db.migrate()`, which passes the engine in."""

from alembic import context
from sqlalchemy import create_engine

from storage.schema import metadata

config = context.config
engine = config.attributes.get("engine") or create_engine(config.get_main_option("sqlalchemy.url"))

with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
