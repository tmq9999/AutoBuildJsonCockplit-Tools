from pathlib import Path

from alembic import command
from alembic.config import Config


async def upgrade(database):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    async with database.engine.begin() as connection:
        def run(sync_connection):
            config.attributes["connection"] = sync_connection
            command.upgrade(config, "head")
        await connection.run_sync(run)
