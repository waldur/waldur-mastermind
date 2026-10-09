import asyncio
import logging
import signal
import threading
import uuid

from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.matrix_chat import bot_state, matrix_client
from waldur_mastermind.matrix_chat.bot import (
    BotError,
    BotSettings,
    ConfigChanged,
    MatrixBot,
)

logger = logging.getLogger(__name__)

# How often a bot that is not configured yet looks again.
CONFIG_POLL_SECONDS = 30


class Command(BaseCommand):
    help = (
        "Run the Matrix bot: Waldur's encrypted member of every Waldur room. It "
        "answers commands and posts everything Waldur sends as the bot. Run "
        "exactly one; a second process refuses to start while the first holds "
        "the bot's lease. Until the homeserver and appservice are configured it "
        "waits, and when they change it starts over with the new settings."
    )

    def handle(self, *args, **options):
        stopped = threading.Event()
        while not stopped.is_set():
            settings = self._wait_for_configuration(stopped)
            if settings is None:
                return
            if not self._run_once(settings, stopped):
                return

    def _watch_signals(self, stopped):
        # The event loop installs its own handlers while the bot runs and
        # removes them when it closes, so these are set again each time.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())

    def _wait_for_configuration(self, stopped):
        """The bot's settings once Waldur can reach the homeserver as the
        appservice; None if the process is stopped first.

        Waiting instead of exiting lets a deployment start the bot before its
        Matrix settings are written, e.g. by a job that runs after install.
        """
        self._watch_signals(stopped)
        logged = False
        while not stopped.is_set():
            if matrix_client.is_homeserver_configured():
                return BotSettings.from_config()
            if not logged:
                logger.info(
                    "The Matrix bot waits for MATRIX_HOMESERVER_URL and "
                    "MATRIX_APPSERVICE_AS_TOKEN to be set."
                )
                logged = True
            stopped.wait(CONFIG_POLL_SECONDS)
        return None

    def _run_once(self, settings, stopped):
        """Run the bot; True if it should start over with new settings."""
        holder = uuid.uuid4().hex
        # Taken before the crypto store opens: two processes writing one store
        # would roll each other's Olm sessions back.
        try:
            bot_state.acquire_lease(settings.user_id, holder)
        except bot_state.LeaseHeld as error:
            raise CommandError(str(error)) from error
        try:
            asyncio.run(self._run(holder, settings, stopped))
        except ConfigChanged:
            logger.info("The Matrix settings changed; the bot starts over.")
            return not stopped.is_set()
        except BotError as error:
            raise CommandError(str(error)) from error
        finally:
            # Closing the event loop puts the default handlers back; a SIGTERM
            # now must still let the lease be released below.
            self._watch_signals(stopped)
            bot_state.release_lease(settings.user_id, holder)
        return False

    async def _run(self, holder, settings, stopped):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_stop():
            stopped.set()
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, request_stop)
        await MatrixBot(holder, settings).run(stop)
