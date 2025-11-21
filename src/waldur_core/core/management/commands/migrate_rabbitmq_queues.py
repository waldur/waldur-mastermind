import logging
import sys

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from requests.auth import HTTPBasicAuth


class Command(BaseCommand):
    """
    Django management command to migrate RabbitMQ to new queue names with quorum queues
    and topic exchanges.

    IMPORTANT: To prevent duplicate message processing during migration:
    - Stop all Celery workers before running this command, OR
    - Ensure workers only connect to OLD queues during migration
    - Deploy new queue configuration AFTER migration completes

    REQUIREMENTS:
    - rabbitmq-shovel plugin (will be auto-enabled/disabled if available)
    - Use --force to proceed without shovels (DELETES messages)

    The migration process:
    1. Checks and enables rabbitmq-shovel plugin if needed
    2. Creates new topic exchanges (tasks-durable, heavy-durable, background-durable)
    3. Creates new quorum queues with the same names and binds them to exchanges
    4. Creates RabbitMQ shovels to MOVE (not copy) messages from old to new queues
    5. Waits for data transfer to complete via shovels (10min timeout)
    6. Cleans up temporary shovels
    7. Restores original shovel plugin state
    8. Deletes old classic queues (tasks, heavy, background)
    9. Deletes old direct exchanges (tasks, heavy, background)
    10. Deploy code changes to use the new queue names
    11. Start/restart Celery workers with new configuration
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

    OLD_QUEUE_NAMES = ["tasks", "heavy", "background"]
    NEW_QUEUE_NAMES = ["tasks-durable", "heavy-durable", "background-durable"]

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

    def create_exchange(self, exchange_name: str) -> bool:
        """Create a new topic exchange.

        Args:
            exchange_name: Name of the exchange to create

        Returns:
            True if creation was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would create topic exchange: %s", exchange_name)
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        exchange_encoded = requests.utils.quote(exchange_name, safe="")
        url = f"{self.rmq_management_url}/exchanges/{vhost_encoded}/{exchange_encoded}"

        exchange_config = {
            "type": "topic",
            "durable": True,
            "auto_delete": False,
            "internal": False,
            "arguments": {},
        }

        try:
            response = requests.put(
                url, json=exchange_config, auth=self.auth, timeout=10
            )
            if response.status_code in (201, 204):
                self.logger.info("Successfully created exchange: %s", exchange_name)
                return True
            else:
                self.logger.error(
                    "Failed to create exchange %s: %s - %s",
                    exchange_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to create exchange %s: %s", exchange_name, exc)
            return False

    def create_queue(self, queue_name: str, routing_key: str) -> bool:
        """Create a new quorum queue.

        Args:
            queue_name: Name of the queue to create
            routing_key: Routing key for the queue

        Returns:
            True if creation was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would create quorum queue: %s", queue_name)
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        queue_encoded = requests.utils.quote(queue_name, safe="")
        url = f"{self.rmq_management_url}/queues/{vhost_encoded}/{queue_encoded}"

        queue_config = {
            "durable": True,
            "auto_delete": False,
            "arguments": {"x-queue-type": "quorum"},
        }

        try:
            response = requests.put(url, json=queue_config, auth=self.auth, timeout=10)
            if response.status_code in (201, 204):
                self.logger.info("Successfully created queue: %s", queue_name)

                # Now bind the queue to its exchange
                binding_url = f"{self.rmq_management_url}/bindings/{vhost_encoded}/e/{requests.utils.quote(queue_name, safe='')}/q/{queue_encoded}"
                binding_config = {"routing_key": routing_key, "arguments": {}}

                binding_response = requests.post(
                    binding_url, json=binding_config, auth=self.auth, timeout=10
                )
                if binding_response.status_code in (201, 204):
                    self.logger.info(
                        "Successfully bound queue %s to exchange %s with routing key %s",
                        queue_name,
                        queue_name,
                        routing_key,
                    )
                    return True
                else:
                    self.logger.error(
                        "Failed to bind queue %s: %s - %s",
                        queue_name,
                        binding_response.status_code,
                        binding_response.text,
                    )
                    return False
            else:
                self.logger.error(
                    "Failed to create queue %s: %s - %s",
                    queue_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to create queue %s: %s", queue_name, exc)
            return False

    def create_shovel(
        self, shovel_name: str, source_queue: str, destination_queue: str
    ) -> bool:
        """Create a shovel to move messages from source queue to destination queue.

        Args:
            shovel_name: Name of the shovel
            source_queue: Source queue name
            destination_queue: Destination queue name

        Returns:
            True if creation was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info(
                "[DRY RUN] Would create shovel: %s (%s -> %s)",
                shovel_name,
                source_queue,
                destination_queue,
            )
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        shovel_encoded = requests.utils.quote(shovel_name, safe="")
        url = f"{self.rmq_management_url}/parameters/shovel/{vhost_encoded}/{shovel_encoded}"

        shovel_config = {
            "value": {
                "src-uri": "amqp://",
                "src-queue": source_queue,
                "dest-uri": "amqp://",
                "dest-queue": destination_queue,
                "ack-mode": "on-confirm",
                "delete-after": "queue-length",
            }
        }

        try:
            response = requests.put(url, json=shovel_config, auth=self.auth, timeout=10)
            if response.status_code in (201, 204):
                self.logger.info(
                    "Successfully created shovel: %s (%s -> %s)",
                    shovel_name,
                    source_queue,
                    destination_queue,
                )
                return True
            else:
                self.logger.error(
                    "Failed to create shovel %s: %s - %s",
                    shovel_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to create shovel %s: %s", shovel_name, exc)
            return False

    def delete_shovel(self, shovel_name: str) -> bool:
        """Delete a shovel.

        Args:
            shovel_name: Name of the shovel to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would delete shovel: %s", shovel_name)
            return True

        vhost_encoded = self._encode_vhost(self.vhost)
        shovel_encoded = requests.utils.quote(shovel_name, safe="")
        url = f"{self.rmq_management_url}/parameters/shovel/{vhost_encoded}/{shovel_encoded}"

        try:
            response = requests.delete(url, auth=self.auth, timeout=10)
            if response.status_code == 204:
                self.logger.info("Successfully deleted shovel: %s", shovel_name)
                return True
            elif response.status_code == 404:
                self.logger.warning("Shovel not found: %s", shovel_name)
                return True
            else:
                self.logger.error(
                    "Failed to delete shovel %s: %s - %s",
                    shovel_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to delete shovel %s: %s", shovel_name, exc)
            return False

    def wait_for_queue_empty(self, queue_name: str, timeout: int = 300) -> bool:
        """Wait for a queue to become empty.

        Args:
            queue_name: Name of the queue to monitor
            timeout: Maximum time to wait in seconds

        Returns:
            True if queue becomes empty, False if timeout
        """
        if self.dry_run:
            self.logger.info(
                "[DRY RUN] Would wait for queue %s to become empty", queue_name
            )
            return True

        import time

        start_time = time.time()

        while time.time() - start_time < timeout:
            queues = self.list_queues()
            queue_info = next((q for q in queues if q["name"] == queue_name), None)

            if not queue_info:
                self.logger.info(
                    "Queue %s not found (may have been deleted)", queue_name
                )
                return True

            message_count = queue_info.get("messages", 0)
            if message_count == 0:
                self.logger.info("Queue %s is now empty", queue_name)
                return True

            self.logger.info(
                "Queue %s still has %d messages, waiting...", queue_name, message_count
            )
            time.sleep(5)

        self.logger.warning("Timeout waiting for queue %s to become empty", queue_name)
        return False

    def list_enabled_plugins(self) -> list[str]:
        """List currently enabled RabbitMQ plugins.

        Returns:
            List of enabled plugin names
        """
        if not self.rabbitmq_available:
            return []

        url = f"{self.rmq_management_url}/plugins"

        try:
            response = requests.get(url, auth=self.auth, timeout=10)
            response.raise_for_status()
            plugins = response.json()
            # Return only enabled plugins
            return [
                plugin["name"] for plugin in plugins if plugin.get("enabled", False)
            ]
        except requests.RequestException as exc:
            self.logger.debug("Failed to list plugins: %s", exc)
            return []

    def enable_plugin(self, plugin_name: str) -> bool:
        """Enable a RabbitMQ plugin.

        Args:
            plugin_name: Name of the plugin to enable

        Returns:
            True if enabling was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would enable plugin: %s", plugin_name)
            return True

        url = f"{self.rmq_management_url}/plugins/{requests.utils.quote(plugin_name, safe='')}"

        try:
            response = requests.put(
                url, json={"enabled": True}, auth=self.auth, timeout=30
            )
            if response.status_code in (200, 201, 204):
                self.logger.info("Successfully enabled plugin: %s", plugin_name)
                return True
            else:
                self.logger.error(
                    "Failed to enable plugin %s: %s - %s",
                    plugin_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to enable plugin %s: %s", plugin_name, exc)
            return False

    def disable_plugin(self, plugin_name: str) -> bool:
        """Disable a RabbitMQ plugin.

        Args:
            plugin_name: Name of the plugin to disable

        Returns:
            True if disabling was successful, False otherwise
        """
        if self.dry_run:
            self.logger.info("[DRY RUN] Would disable plugin: %s", plugin_name)
            return True

        url = f"{self.rmq_management_url}/plugins/{requests.utils.quote(plugin_name, safe='')}"

        try:
            response = requests.put(
                url, json={"enabled": False}, auth=self.auth, timeout=30
            )
            if response.status_code in (200, 201, 204):
                self.logger.info("Successfully disabled plugin: %s", plugin_name)
                return True
            else:
                self.logger.error(
                    "Failed to disable plugin %s: %s - %s",
                    plugin_name,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as exc:
            self.logger.error("Failed to disable plugin %s: %s", plugin_name, exc)
            return False

    def check_shovel_plugin(self) -> tuple[bool, bool]:
        """Check if shovel plugin is available and enabled.

        Returns:
            Tuple of (available, enabled)
        """
        enabled_plugins = self.list_enabled_plugins()

        # Check if shovel plugin is enabled
        shovel_enabled = "rabbitmq_shovel" in enabled_plugins

        if shovel_enabled:
            self.logger.debug("Shovel plugin is already enabled")
            return True, True

        # If not enabled, try to enable it to check if it's available
        if self.dry_run:
            self.logger.info("[DRY RUN] Would check shovel plugin availability")
            return True, False

        self.logger.info("Shovel plugin not enabled, checking availability...")

        # Try to enable the plugin to see if it's available
        success = self.enable_plugin("rabbitmq_shovel")
        if success:
            # Plugin was available and is now enabled
            return True, False  # Was available but not previously enabled
        else:
            self.logger.error(
                "Shovel plugin is not available on this RabbitMQ installation"
            )
            return False, False

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

        celery_queues = [q for q in queues if q["name"] in self.OLD_QUEUE_NAMES]
        celery_exchanges = [e for e in exchanges if e["name"] in self.OLD_QUEUE_NAMES]

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

        # Check if any old queues exist - they need to be migrated
        for queue_name in self.OLD_QUEUE_NAMES:
            queue_info = analysis["queues"].get(queue_name)
            if queue_info and queue_info["exists"]:
                self.logger.debug("Old queue %s exists, migration needed", queue_name)
                return True

        # Check if any old exchanges exist - they need to be migrated
        for exchange_name in self.OLD_QUEUE_NAMES:
            exchange_info = analysis["exchanges"].get(exchange_name)
            if exchange_info and exchange_info["exists"]:
                self.logger.debug(
                    "Old exchange %s exists, migration needed", exchange_name
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

        # Check shovel plugin availability
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Pre-migration: Checking shovel plugin availability")
        self.logger.info("=" * 70)

        shovel_available, shovel_was_enabled = self.check_shovel_plugin()
        use_shovels = True

        if not shovel_available:
            if not self.force:
                self.logger.error(
                    "Shovel plugin is not available. Cannot perform safe migration."
                )
                self.logger.error(
                    "Please install rabbitmq-shovel plugin or use --force for unsafe migration."
                )
                self.logger.error("--force will DELETE any pending messages!")
                return False
            else:
                self.logger.warning("Shovel plugin not available - using FORCE mode")
                self.logger.warning(
                    "This will DELETE pending messages instead of moving them!"
                )
                use_shovels = False

        if shovel_available and not shovel_was_enabled:
            self.logger.info("Shovel plugin was enabled for this migration")

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

        # Handle pending messages - they will be moved via shovels
        if analysis["has_messages"]:
            total_messages = sum(
                info["messages"] for info in analysis["queues"].values()
            )
            self.logger.warning("\n" + "⚠️" * 70)
            self.logger.warning(
                "PENDING MESSAGES DETECTED: Queues contain %d pending messages",
                total_messages,
            )
            self.logger.warning(
                "Messages will be safely MOVED (not copied) to new queues using RabbitMQ shovels."
            )
            self.logger.warning("\n⚠️  CRITICAL: Prevent duplicate processing:")
            self.logger.warning(
                "   - Workers on OLD queues: ✅ OK (will process messages normally)"
            )
            self.logger.warning("   - Shovels: ✅ OK (will move unprocessed messages)")
            self.logger.warning(
                "   - Workers on BOTH old+new: ❌ DANGER (duplicate processing!)"
            )
            self.logger.warning("\n⚠️  SAFE DEPLOYMENT:")
            self.logger.warning(
                "   1. Run migration (creates new queues + moves messages)"
            )
            self.logger.warning("   2. Deploy new code with -durable queue names")
            self.logger.warning(
                "   3. Restart workers (switches from old to new queues)"
            )
            self.logger.warning("⚠️" * 70)

            # Show detailed message counts
            for queue_name, info in analysis["queues"].items():
                if info["messages"] > 0:
                    self.logger.warning(
                        "  - %s: %d pending messages (will be moved)",
                        queue_name,
                        info["messages"],
                    )

            if not self.auto_migrate:
                response = input("\nContinue with message migration? (yes/no): ")
                if response.lower() not in ["yes", "y"]:
                    self.logger.info("Migration aborted by user")
                    return False
        else:
            self.logger.info("\nNo pending messages found in queues.")

        # Create new exchanges first
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 1: Creating new topic exchanges")
        self.logger.info("=" * 70)

        all_success = True
        for exchange_name in self.NEW_QUEUE_NAMES:
            success = self.create_exchange(exchange_name)
            all_success = all_success and success

        # Create new queues and bind them
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 2: Creating new quorum queues")
        self.logger.info("=" * 70)

        queue_routing_mapping = {
            "tasks-durable": "tasks-durable",
            "heavy-durable": "heavy-durable",
            "background-durable": "background-durable",
        }

        for queue_name in self.NEW_QUEUE_NAMES:
            routing_key = queue_routing_mapping[queue_name]
            success = self.create_queue(queue_name, routing_key)
            all_success = all_success and success

        # Create shovels to move data from old to new queues (if shovels available)
        created_shovels = []
        if use_shovels and analysis["has_messages"]:
            self.logger.info("\n" + "=" * 70)
            self.logger.info(
                "Step 3: Creating shovels to move data from old to new queues"
            )
            self.logger.info("=" * 70)

            shovel_mapping = {
                "tasks": "tasks-durable",
                "heavy": "heavy-durable",
                "background": "background-durable",
            }

            for old_queue, new_queue in shovel_mapping.items():
                if (
                    old_queue in analysis["queues"]
                    and analysis["queues"][old_queue]["messages"] > 0
                ):
                    shovel_name = f"migrate-{old_queue}-to-{new_queue}"
                    success = self.create_shovel(shovel_name, old_queue, new_queue)
                    if success:
                        created_shovels.append((shovel_name, old_queue))
                    all_success = all_success and success
                else:
                    self.logger.info(
                        "Queue %s has no messages to migrate, skipping shovel",
                        old_queue,
                    )

            # Wait for shovels to complete data transfer
            if created_shovels and not self.dry_run:
                self.logger.info("\n" + "=" * 70)
                self.logger.info("Step 4: Waiting for data transfer to complete")
                self.logger.info("=" * 70)

                for shovel_name, old_queue in created_shovels:
                    self.logger.info(
                        "Waiting for shovel %s to complete...", shovel_name
                    )
                    success = self.wait_for_queue_empty(
                        old_queue, timeout=600
                    )  # 10 minute timeout
                    if not success:
                        self.logger.warning(
                            "Timeout waiting for queue %s to empty via shovel",
                            old_queue,
                        )
                        # Continue anyway, but log the issue
                    all_success = all_success and success

            # Clean up shovels
            if created_shovels:
                self.logger.info("\n" + "=" * 70)
                self.logger.info("Step 5: Cleaning up shovels")
                self.logger.info("=" * 70)

                for shovel_name, _ in created_shovels:
                    success = self.delete_shovel(shovel_name)
                    # Don't fail the migration if shovel cleanup fails
                    if not success:
                        self.logger.warning(
                            "Failed to clean up shovel %s, but continuing", shovel_name
                        )
        elif analysis["has_messages"] and not use_shovels:
            self.logger.warning("\n" + "⚠️" * 70)
            self.logger.warning(
                "FORCE MODE: Messages will be DELETED (no shovels available)"
            )
            self.logger.warning("⚠️" * 70)

        # Delete old queues
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 6: Deleting old queues")
        self.logger.info("=" * 70)

        for queue_name in self.OLD_QUEUE_NAMES:
            if queue_name in analysis["queues"]:
                success = self.delete_queue(queue_name)
                all_success = all_success and success
            else:
                self.logger.info("Queue %s does not exist, skipping", queue_name)

        # Delete old exchanges
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Step 7: Deleting old exchanges")
        self.logger.info("=" * 70)

        for exchange_name in self.OLD_QUEUE_NAMES:
            if exchange_name in analysis["exchanges"]:
                success = self.delete_exchange(exchange_name)
                all_success = all_success and success
            else:
                self.logger.info("Exchange %s does not exist, skipping", exchange_name)

        # Restore shovel plugin state if we enabled it
        if not shovel_was_enabled and shovel_available:
            self.logger.info("\n" + "=" * 70)
            self.logger.info("Post-migration: Restoring shovel plugin state")
            self.logger.info("=" * 70)

            success = self.disable_plugin("rabbitmq_shovel")
            if success:
                self.logger.info("Shovel plugin disabled (restored to original state)")
            else:
                self.logger.warning(
                    "Failed to disable shovel plugin - it remains enabled"
                )
                # Don't fail the migration for this

        # Summary
        self.logger.info("\n" + "=" * 70)
        if self.dry_run:
            self.logger.info("DRY RUN COMPLETED")
            self.logger.info("=" * 70)
            self.logger.info("\nTo execute the migration, run without --dry-run flag")
        elif all_success:
            self.logger.info("MIGRATION COMPLETED SUCCESSFULLY")
            self.logger.info("=" * 70)
            self.logger.info("\nMigration Summary:")
            self.logger.info("- Created new durable queues with topic exchanges")
            self.logger.info("- Moved existing messages via RabbitMQ shovels")
            self.logger.info("- Cleaned up old queues and exchanges")
            self.logger.info("\nNext steps:")
            self.logger.info("1. Deploy the new code with -durable queue configuration")
            self.logger.info("2. Restart Celery workers to connect to new queues")
            self.logger.info("3. Verify that the new queues are working correctly:")
            self.logger.info("   - Check RabbitMQ management UI")
            self.logger.info("   - Monitor task processing")
            self.logger.info("   - Verify no messages are lost")
        else:
            self.logger.error("MIGRATION FAILED")
            self.logger.error("=" * 70)
            self.logger.error("Some operations failed. Please check the logs above.")
            return False

        return all_success
