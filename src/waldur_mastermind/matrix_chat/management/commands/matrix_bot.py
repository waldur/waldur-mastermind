import asyncio
import signal
import uuid

from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.matrix_chat import bot_state, matrix_client
from waldur_mastermind.matrix_chat.bot import BotError, BotSettings, MatrixBot


class Command(BaseCommand):
    help = (
        "Run the Matrix bot: Waldur's encrypted member of every Waldur room. It "
        "answers commands and posts everything Waldur sends as the bot. Run "
        "exactly one; a second process refuses to start while the first holds "
        "the bot's lease."
    )

    def handle(self, *args, **options):
        if not matrix_client.is_homeserver_configured():
            raise CommandError(
                "MATRIX_HOMESERVER_URL and MATRIX_APPSERVICE_AS_TOKEN must be set."
            )
        settings = BotSettings.from_config()
        user_id = settings.user_id
        holder = uuid.uuid4().hex
        # Taken before the crypto store opens: two processes writing one store
        # would roll each other's Olm sessions back.
        try:
            bot_state.acquire_lease(user_id, holder)
        except bot_state.LeaseHeld as error:
            raise CommandError(str(error)) from error
        try:
            asyncio.run(self._run(holder, settings))
        except BotError as error:
            raise CommandError(str(error)) from error
        finally:
            bot_state.release_lease(user_id, holder)

    async def _run(self, holder, settings):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await MatrixBot(holder, settings).run(stop)
