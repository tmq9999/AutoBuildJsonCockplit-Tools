from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations using the configured service database")
context.configure(connection=connection)
with context.begin_transaction():
    context.run_migrations()
