import uvicorn
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.mcp import auth, server

DEFAULT_PORT = 8091


class Command(BaseCommand):
    help = (
        "Serve the remote MCP endpoint (/mcp), its OAuth resource metadata and "
        "/health. All but /health answer 404 while the MCP_ENABLED setting is off."
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
        parser.add_argument(
            "--statement-timeout",
            type=int,
            default=auth.DEFAULT_STATEMENT_TIMEOUT,
            help="Seconds a database statement may run before it is cancelled; "
            "0 turns the limit off.",
        )

    def handle(self, *args, **options):
        # Postgres would refuse it on every query, not at startup.
        if not 0 <= options["statement_timeout"] <= auth.MAX_STATEMENT_TIMEOUT:
            raise CommandError(
                "--statement-timeout must be between 0 and "
                f"{auth.MAX_STATEMENT_TIMEOUT} seconds."
            )
        auth.configure_orm_threads(options["db_threads"])
        auth.configure_statement_timeout(options["statement_timeout"])
        uvicorn.run(
            server.build_app(),
            host=options["host"],
            port=options["port"],
            lifespan="on",
        )
