import logging
import sys

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from requests.auth import HTTPBasicAuth


class Command(BaseCommand):
    """
    Django management command to migrate RabbitMQ from classic queues with direct exchanges
    to quorum queues with topic exchanges.

    IMPORTANT: This command must be run BEFORE deploying the code changes that
    introduce quorum queues in celery_settings.py

    The migration process:
    1. Stops all Celery workers (manual step - not handled by this command)
    2. Deletes old classic queues (tasks, heavy, background)
    3. Deletes old direct exchanges (tasks, heavy, background)
    """

    help = "Migrate RabbitMQ queues from classic to quorum type"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_logging()

    def setup_logging(self):
        """Configure logging for the command."""
        self.logger = logging.getLogger(__name__)
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--vhost",
            default="/",
            help="RabbitMQ virtual host to migrate (default: /)",
        )
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Only check if migration is needed (exit code 0=no migration needed, 1=migration needed)",
        )
        parser.add_argument(
            "--auto-migrate",
            action="store_true",
            help="Automatically proceed with migration without interactive prompts",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force migration even when queues have pending messages (DANGEROUS)",
        )

    def handle(self, *args, **options):
        try:
            migration = RabbitMQQueueMigration(
                dry_run=options["dry_run"],
                vhost=options["vhost"],
                auto_migrate=options["auto_migrate"],
                force=options["force"],
                stdout=self.stdout,
                logger=self.logger,
            )

            if options["check_only"]:
                needs_migration = migration.check_migration_needed()
                if needs_migration:
                    self.stdout.write("Migration needed")
                    sys.exit(1)
                else:
                    self.stdout.write("No migration needed")
                    sys.exit(0)
            else:
                success = migration.migrate()
                if not success:
                    raise CommandError("Migration failed")

        except KeyboardInterrupt:
            self.logger.info("Migration interrupted by user")
            raise CommandError("Migration interrupted")
        except Exception as exc:
            self.logger.exception("Unexpected error during migration: %s", exc)
            raise CommandError(f"Migration failed: {exc}")


class RabbitMQQueueMigration:
    """Handles migration of RabbitMQ queues from classic to quorum."""

    QUEUE_NAMES = ["tasks", "heavy", "background"]

    def __init__(
        self,
        dry_run: bool = False,
        vhost: str = "/",
        auto_migrate: bool = False,
        force: bool = False,
        stdout=None,
        logger=None,
    ):
        """Initialize the migration handler.

        Args:
            dry_run: If True, only show what would be done without making changes
            vhost: RabbitMQ virtual host to operate on
            auto_migrate: If True, skip interactive prompts
            force: If True, allow migration even with pending messages
            stdout: Django command stdout for output
            logger: Logger instance
        """
        self.dry_run = dry_run
        self.vhost = vhost
        self.auto_migrate = auto_migrate
        self.force = force
        self.stdout = stdout
        self.logger = logger
        self.rabbitmq_available = True

        # Validate RabbitMQ settings
        if not hasattr(settings, "RABBITMQ"):
            self.logger.warning("RABBITMQ settings not found in Django settings")
            self.rabbitmq_available = False
            return

        required_keys = ["HOST", "MANAGEMENT_PORT", "USER", "PASSWORD"]
        missing_keys = [key for key in required_keys if key not in settings.RABBITMQ]
        if missing_keys:
            self.logger.warning(f"Missing RABBITMQ settings: {missing_keys}")
            self.rabbitmq_available = False
            return

        self.rmq_management_url = (
            f"http://{settings.RABBITMQ['HOST']}:"
            f"{settings.RABBITMQ['MANAGEMENT_PORT']}/api"
        )
        self.auth = HTTPBasicAuth(
            settings.RABBITMQ["USER"], settings.RABBITMQ["PASSWORD"]
        )

    def _encode_vhost(self, vhost: str) -> str:
        """Encode vhost name for URL."""
        return requests.utils.quote(vhost, safe="")

    def list_queues(self) -> list[dict]:
        """List all queues in the vhost.

        Returns:
            List of queue dictionaries with name, type, and other properties
        """
        if not self.rabbitmq_available:
            return []

        vhost_encoded = self._encode_vhost(self.vhost)
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}"

        try:
            response = requests.get(url, auth=self.auth, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            if (
                isinstance(exc, requests.exceptions.HTTPError)
                and exc.response.status_code == 401
            ):
                self.logger.debug(
                    "RabbitMQ authentication failed - service may not be available or configured"
                )
            else:
                self.logger.debug("Failed to list queues: %s", exc)
            return []

    def list_exchanges(self) -> list[dict]:
        """List all exchanges in the vhost.

        Returns:
            List of exchange dictionaries with name, type, and other properties
        """
        if not self.rabbitmq_available:
            return []

        vhost_encoded = self._encode_vhost(self.vhost)
        url = f"{self.rmq_management_url}/exchanges/{vhost_encoded}"

        try:
            response = requests.get(url, auth=self.auth, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            if (
                isinstance(exc, requests.exceptions.HTTPError)
                and exc.response.status_code == 401
            ):
                self.logger.debug(
                    "RabbitMQ authentication failed - service may not be available or configured"
                )
            else:
                self.logger.debug("Failed to list exchanges: %s", exc)
            return []

    def delete_queue(self, queue_name: str) -> bool:
        """Delete a queue.

        Args:
            queue_name: Name of the queue to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would delete queue: %s", queue_name)
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        queue_encoded = requests.utils.quote(queue_name, safe="")
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}/{queue_encoded}"

        try:
            response = requests.delete(url, auth=self.auth, timeout=10)
            if response.status_code == 204:
                self.logger.info("Successfully deleted queue: %s", queue_name)
                return True
            elif response.status_code == 404:
                self.logger.warning("Queue not found: %s", queue_name)
                return True
            else:
                self.logger.error(
                    "Failed to delete queue %s: %s - %s",
                    queue_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to delete queue %s: %s", queue_name, exc)
            return False

    def delete_exchange(self, exchange_name: str) -> bool:
        """Delete an exchange.

        Args:
            exchange_name: Name of the exchange to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would delete exchange: %s", exchange_name)
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        exchange_encoded = requests.utils.quote(exchange_name, safe="")
        url = f"{self.rmq_management_url}/exchanges/{vhost_encoded}/{exchange_encoded}"

        try:
            response = requests.delete(url, auth=self.auth, timeout=10)
            if response.status_code == 204:
                self.logger.info("Successfully deleted exchange: %s", exchange_name)
                return True
            elif response.status_code == 404:
                self.logger.warning("Exchange not found: %s", exchange_name)
                return True
            else:
                self.logger.error(
                    "Failed to delete exchange %s: %s - %s",
                    exchange_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to delete exchange %s: %s", exchange_name, exc)
            return False

    def analyze_current_state(self) -> dict:
        """Analyze the current state of queues and exchanges.

        Returns:
            Dictionary with analysis results
        """
        self.logger.debug("Analyzing current RabbitMQ state for vhost: %s", self.vhost)

        if not self.rabbitmq_available:
            self.logger.debug("RabbitMQ not available, assuming no migration needed")
            return {
                "queues": {},
                "exchanges": {},
                "has_messages": False,
                "ready_for_migration": True,
                "rabbitmq_unavailable": True,
            }

        queues = self.list_queues()
        exchanges = self.list_exchanges()

        celery_queues = [q for q in queues if q["name"] in self.QUEUE_NAMES]
        celery_exchanges = [e for e in exchanges if e["name"] in self.QUEUE_NAMES]

        analysis = {
            "queues": {},
            "exchanges": {},
            "has_messages": False,
            "ready_for_migration": True,
            "rabbitmq_unavailable": False,
        }

        for queue in celery_queues:
            queue_name = queue["name"]
            message_count = queue.get("messages", 0)
            queue_type = queue.get("type", "classic")
            queue_arguments = queue.get("arguments", {})

            analysis["queues"][queue_name] = {
                "exists": True,
                "type": queue_type,
                "messages": message_count,
                "durable": queue.get("durable", False),
                "is_quorum": queue_arguments.get("x-queue-type") == "quorum",
            }

            if message_count > 0:
                analysis["has_messages"] = True
                self.logger.warning(
                    "Queue %s has %d pending messages", queue_name, message_count
                )

        for exchange in celery_exchanges:
            exchange_name = exchange["name"]
            exchange_type = exchange.get("type", "direct")

            analysis["exchanges"][exchange_name] = {
                "exists": True,
                "type": exchange_type,
                "durable": exchange.get("durable", False),
                "is_topic": exchange_type == "topic",
            }

        return analysis

    def check_migration_needed(self) -> bool:
        """Check if migration is needed.

        Returns:
            True if migration is needed, False otherwise
        """
        analysis = self.analyze_current_state()

        # If RabbitMQ is unavailable, no migration needed
        if analysis.get("rabbitmq_unavailable", False):
            self.logger.debug("RabbitMQ unavailable, no migration needed")
            return False

        # Check if any queues exist but are not quorum type
        for queue_name in self.QUEUE_NAMES:
            queue_info = analysis["queues"].get(queue_name)
            if queue_info and queue_info["exists"] and not queue_info["is_quorum"]:
                self.logger.debug(
                    "Queue %s is not quorum type, migration needed", queue_name
                )
                return True

        # Check if any exchanges exist but are not topic type
        for exchange_name in self.QUEUE_NAMES:
            exchange_info = analysis["exchanges"].get(exchange_name)
            if (
                exchange_info
                and exchange_info["exists"]
                and not exchange_info["is_topic"]
            ):
                self.logger.debug(
                    "Exchange %s is not topic type, migration needed", exchange_name
                )
                return True

        # If we have any classic queues or direct exchanges, migration is needed
        if analysis["queues"] or analysis["exchanges"]:
            # Check if all are already correct
            all_queues_correct = all(
                not info["exists"] or info["is_quorum"]
                for info in analysis["queues"].values()
            )
            all_exchanges_correct = all(
                not info["exists"] or info["is_topic"]
                for info in analysis["exchanges"].values()
            )

            if all_queues_correct and all_exchanges_correct:
                self.logger.debug(
                    "All queues and exchanges are already correctly configured"
                )
                return False
            else:
                return True

        # No relevant queues or exchanges found - fresh deployment, no migration needed
        self.logger.debug("No relevant queues or exchanges found, no migration needed")
        return False

    def migrate(self) -> bool:
        """Execute the migration.

        Returns:
            True if migration was successful, False otherwise
        """
        self.logger.info("=" * 70)
        if self.dry_run:
            self.logger.info("DRY RUN MODE - No changes will be made")
        self.logger.info("Starting RabbitMQ queue migration for vhost: %s", self.vhost)
        self.logger.info("=" * 70)

        # Check if migration is needed
        if not self.check_migration_needed():
            self.logger.info(
                "No migration needed, queues are already properly configured"
            )
            return True

        # Analyze current state
        analysis = self.analyze_current_state()

        # If RabbitMQ is unavailable, return success (no migration needed)
        if analysis.get("rabbitmq_unavailable", False):
            self.logger.info("RabbitMQ service unavailable, skipping migration")
            return True

        self.logger.info("\nCurrent state:")
        self.logger.info("-" * 70)
        self.logger.info("Queues found:")
        for queue_name, info in analysis["queues"].items():
            self.logger.info(
                "  - %s: type=%s, messages=%d, durable=%s, quorum=%s",
                queue_name,
                info["type"],
                info["messages"],
                info["durable"],
                info["is_quorum"],
            )

        self.logger.info("\nExchanges found:")
        for exchange_name, info in analysis["exchanges"].items():
            self.logger.info(
                "  - %s: type=%s, durable=%s, topic=%s",
                exchange_name,
                info["type"],
                info["durable"],
                info["is_topic"],
            )

        # Handle pending messages - enforce empty queue policy
        if analysis["has_messages"]:
            total_messages = sum(
                info["messages"] for info in analysis["queues"].values()
            )
            self.logger.error("\n" + "!" * 70)
            self.logger.error(
                "MIGRATION BLOCKED: Queues contain %d pending messages!", total_messages
            )
            self.logger.error(
                "For safety, migration only proceeds when queues are empty."
            )
            self.logger.error("!" * 70)

            # Show detailed message counts
            for queue_name, info in analysis["queues"].items():
                if info["messages"] > 0:
                    self.logger.error(
                        "  - %s: %d pending messages", queue_name, info["messages"]
                    )

            self.logger.error("\nOptions to proceed:")
            self.logger.error(
                "1. RECOMMENDED: Stop Celery beat and API (task schedulers), let workers drain queues"
            )
            self.logger.error(
                "2. ALTERNATIVE: Schedule maintenance window when queues are naturally empty"
            )
            self.logger.error(
                "3. DANGEROUS: Use --force flag to delete messages and proceed"
            )

            if not self.force:
                if self.dry_run:
                    self.logger.error(
                        "\n[DRY RUN] Migration would be blocked due to pending messages"
                    )
                    return False
                else:
                    self.logger.error("\nMigration aborted due to pending messages")
                    return False
            else:
                self.logger.warning("\n" + "⚠️" * 70)
                self.logger.warning("FORCE MODE: Proceeding with message deletion!")
                self.logger.warning("⚠️" * 70)

                if not self.dry_run and not self.auto_migrate:
                    self.logger.warning(
                        "\nThis will DELETE %d messages permanently!", total_messages
                    )
                    response = input("Type 'DELETE MESSAGES' to confirm: ")
                    if response != "DELETE MESSAGES":
                        self.logger.info(
                            "Migration aborted - confirmation not provided"
                        )
                        return False

        # Delete queues
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 1: Deleting old queues")
        self.logger.info("=" * 70)

        all_success = True
        for queue_name in self.QUEUE_NAMES:
            if queue_name in analysis["queues"]:
                success = self.delete_queue(queue_name)
                all_success = all_success and success
            else:
                self.logger.info("Queue %s does not exist, skipping", queue_name)

        # Delete exchanges
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 2: Deleting old exchanges")
        self.logger.info("=" * 70)

        for exchange_name in self.QUEUE_NAMES:
            if exchange_name in analysis["exchanges"]:
                success = self.delete_exchange(exchange_name)
                all_success = all_success and success
            else:
                self.logger.info("Exchange %s does not exist, skipping", exchange_name)

        # Summary
        self.logger.info("\n" + "=" * 70)
        if self.dry_run:
            self.logger.info("DRY RUN COMPLETED")
            self.logger.info("=" * 70)
            self.logger.info("\nTo execute the migration, run without --dry-run flag")
        elif all_success:
            self.logger.info("MIGRATION COMPLETED SUCCESSFULLY")
            self.logger.info("=" * 70)
            self.logger.info("\nNext steps:")
            self.logger.info("1. Deploy the new code with quorum queue configuration")
            self.logger.info(
                "2. Start Celery workers (they will auto-create the new queues)"
            )
            self.logger.info("3. Verify that the new queues are created correctly:")
            self.logger.info(
                "   - Check RabbitMQ management UI or run: "
                "rabbitmqctl list_queues name type arguments"
            )
        else:
            self.logger.error("MIGRATION FAILED")
            self.logger.error("=" * 70)
            self.logger.error("Some operations failed. Please check the logs above.")
            return False

        return all_success
