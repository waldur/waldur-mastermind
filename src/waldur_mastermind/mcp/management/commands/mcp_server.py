import uvicorn
from django.core.management.base import BaseCommand

from waldur_mastermind.mcp import auth, server

DEFAULT_PORT = 8091


class Command(BaseCommand):
    help = (
        "Serve the remote MCP endpoint (/mcp) and /health. The endpoint answers "
        "404 while the MCP_ENABLED setting is off."
    )

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=DEFAULT_PORT)
        parser.add_argument(
            "--db-threads",
            type=int,
            default=auth.DEFAULT_ORM_THREADS,
            help="Threads that run database calls; each holds one connection.",
        )

    def handle(self, *args, **options):
        auth.configure_orm_threads(options["db_threads"])
        uvicorn.run(
            server.build_app(),
            host=options["host"],
            port=options["port"],
            lifespan="on",
        )
