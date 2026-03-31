"""
Management command: load_notifications

Populates the database with Notification and NotificationTemplate records
from Waldur's registered NOTIFICATIONS registry, and seeds dbtemplates.Template
with the built-in filesystem content so the dbtemplates loader serves the correct
template on first use.

Template content overrides are handled separately by the override_templates command.

All notifications are **disabled by default**. To enable specific notifications,
list them explicitly in the input file with a value of ``true``.

If the input file does not exist, all registered notifications are synced with
their model defaults (disabled) and no enabled status is overridden.

Input file format (JSON or YAML):

    Each top-level key is a notification key (e.g. "users.invitation_created").
    The value is a bool that sets the notification's enabled/disabled status.
    Only keys present in the file are overridden; absent keys keep their
    current database value (disabled on first creation).

    Example JSON:

        {
            "users.invitation_created": true,
            "users.invitation_approved": true,
            "marketplace.notification_usages": true,
            "invoices.notification": false
        }

    Example YAML:

        users.invitation_created: true
        users.invitation_approved: true
        marketplace.notification_usages: true
        invoices.notification: false

    In waldur-helm, set ``waldur.notifications`` in values.yaml:

        waldur:
          notifications:
            users.invitation_created: true
            users.invitation_approved: true
"""

import json
import logging
import os

import yaml
from dbtemplates.models import Template as DBTemplate
from dbtemplates.utils.template import get_template_source
from django.core.management.base import BaseCommand
from django.db import IntegrityError

from waldur_core.core.models import Notification, NotificationTemplate
from waldur_core.structure.notifications import NOTIFICATIONS

logger = logging.getLogger(__name__)


def _is_registered(notification_key):
    """Return True if *notification_key* exists in the NOTIFICATIONS registry."""
    for section_key, section in NOTIFICATIONS.items():
        for notification in section:
            if notification_key == f"{section_key}.{notification['path']}":
                return True
    return False


class Command(BaseCommand):
    help = (
        "Sync notifications and their templates from a JSON/YAML config file to the DB."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "notifications_file",
            help="Path to a JSON or YAML file mapping notification keys to their "
            "enabled status (bool).",
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        filepath = options["notifications_file"]
        if not os.path.exists(filepath):
            self.stdout.write(
                self.style.WARNING(
                    f"Notifications file not found: {filepath}, "
                    "syncing registered notifications with defaults only."
                )
            )
            notifications = {}
        else:
            notifications = self._load_file(filepath)
        self._warn_unknown_keys(notifications)

        for notification_data in self._iter_registered_notifications():
            try:
                self._sync_notification(notification_data, notifications)
            except Exception as exc:
                logger.exception(
                    "Failed to process notification '%s'", notification_data["path"]
                )
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed to process notification '{notification_data['path']}': "
                        f"{exc}, skipping"
                    )
                )

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_file(self, filepath):
        """Parse *filepath* as JSON or YAML depending on its extension."""
        logger.info("Loading notifications file: %s", filepath)
        with open(filepath) as fh:
            if filepath.endswith((".yaml", ".yml")):
                return yaml.safe_load(fh)
            return json.load(fh)

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _warn_unknown_keys(self, notifications):
        """Emit a warning for every key in *notifications* that is not registered."""
        for key in notifications:
            if not _is_registered(key):
                logger.warning("Unknown notification key in file: %s", key)
                self.stdout.write(
                    self.style.WARNING(f"Unknown notification key in file: {key}")
                )

    def _iter_registered_notifications(self):
        """Yield a descriptor dict for every notification in NOTIFICATIONS."""
        for section_key, section in NOTIFICATIONS.items():
            for notification in section:
                path = f"{section_key}.{notification['path']}"
                yield {
                    "path": path,
                    "templates": {
                        f"{section_key}/{tmpl['path']}": tmpl["name"]
                        for tmpl in notification["templates"]
                    },
                    "description": notification.get("description"),
                }

    # ------------------------------------------------------------------
    # Per-notification sync
    # ------------------------------------------------------------------

    def _sync_notification(self, notification_data, notifications):
        """Create or update the Notification row and all its templates."""
        notification, created = Notification.objects.get_or_create(
            key=notification_data["path"],
        )

        for template_path, template_name in notification_data["templates"].items():
            self._sync_template(
                notification=notification,
                template_path=template_path,
                template_name=template_name,
            )

        notification.description = notification_data.get("description")
        notification.save()

        self._apply_enabled_status(
            notification, notifications.get(notification_data["path"])
        )

        if created:
            logger.info(
                "Created notification '%s' (enabled=%s)",
                notification.key,
                notification.enabled,
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Created notification '{notification.key}' "
                    f"(enabled={notification.enabled})"
                )
            )

    # ------------------------------------------------------------------
    # Template sync
    # ------------------------------------------------------------------

    def _sync_template(self, notification, template_path, template_name):
        """
        Ensure NotificationTemplate and DBTemplate rows exist for *template_path*.

        DBTemplate is seeded from the built-in filesystem template on first creation
        only — existing entries (including user overrides via override_templates) are
        never touched.
        """
        try:
            notification_template, _ = NotificationTemplate.objects.get_or_create(
                path=template_path,
                defaults={"name": template_name},
            )
            notification.templates.add(notification_template)
        except NotificationTemplate.MultipleObjectsReturned:
            logger.error(
                "Multiple NotificationTemplate rows for path '%s' "
                "(notification '%s') — using the first one",
                template_path,
                notification.key,
            )
            self.stdout.write(
                self.style.ERROR(
                    f"Multiple NotificationTemplate rows for path '{template_path}' "
                    f"(notification '{notification.key}') — using the first one"
                )
            )
            notification_template = NotificationTemplate.objects.filter(
                path=template_path
            ).first()
            if notification_template:
                notification.templates.add(notification_template)
        except (IntegrityError, Exception) as exc:
            logger.exception(
                "Error processing template '%s' for notification '%s'",
                template_path,
                notification.key,
            )
            self.stdout.write(
                self.style.ERROR(
                    f"Error processing template '{template_path}': {exc}, skipping"
                )
            )
            return

        self._seed_db_template(template_path)

    def _seed_db_template(self, template_path):
        """
        Create a DBTemplate row seeded from the filesystem template if none exists yet.

        Uses get_or_create so that any content already stored in the database
        (e.g. a previous override applied by override_templates) is preserved.
        """
        source = get_template_source(template_path)
        if source:
            logger.debug("Seeding DB template from filesystem: '%s'", template_path)
            DBTemplate.objects.get_or_create(
                name=template_path,
                defaults={"content": source},
            )
        else:
            logger.warning(
                "No filesystem template found for '%s', skipping DB seed",
                template_path,
            )

    # ------------------------------------------------------------------
    # Enabled status
    # ------------------------------------------------------------------

    def _apply_enabled_status(self, notification, file_value):
        """
        Update notification.enabled from the file value when it is a plain bool.
        Keys absent from the file leave the current enabled status unchanged.
        """
        if not isinstance(file_value, bool):
            return
        if notification.enabled != file_value:
            logger.info(
                "Notification '%s' enabled status changed: %s -> %s",
                notification.key,
                notification.enabled,
                file_value,
            )
            notification.enabled = file_value
            notification.save()
            self.stdout.write(
                self.style.WARNING(
                    f"Notification '{notification.key}' enabled set to {file_value}"
                )
            )
