"""
Management command: load_notifications

Populates the database with Notification and NotificationTemplate records
from Waldur's registered NOTIFICATIONS registry. Templates are created with
blank content, so DatabaseTemplateLoader falls through to the filesystem
template until an operator overrides it.

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

Orphaned rows
-------------

A ``Notification`` row whose key is no longer in the ``NOTIFICATIONS`` registry
(removed, renamed, or folded into another notification) is never cleaned up by
this command's normal sync — it only ever creates or updates. Every run reports
such rows, along with each of their templates classified as:

- ``shared``: still declared by another *registered* notification, so it is
  kept regardless of ``--prune``.
- ``customized``: has operator-overridden content (``NotificationTemplate.content``
  is non-blank), so it is kept and must be handled manually — pruning never
  discards a customisation.
- ``safe to remove``: not declared by any registered notification and has no
  override; deleted only when ``--prune`` is passed.

Deletion is opt-in via ``--prune`` and never runs automatically. In particular,
``initdb`` runs this command on every unattended boot *without* ``--prune``, by
design — deleting on boot is too strong a default for a command that has, until
now, only ever added rows. Run ``waldur load_notifications <file> --prune``
manually (or from a controlled maintenance job) to actually remove orphaned
rows and their safe-to-remove templates.

Renames are not detected. A rename looks identical to a removal plus an
addition, and there is currently no reliable way to tell them apart; carrying
state (like ``enabled``) across a rename is handled per-case by a hand-written
data migration, as in ``core/migrations/0042_call_and_proposal_invitation_notifications.py``.
"""

import json
import logging
import os

import yaml
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

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


def _registered_keys():
    """Return the set of every notification key currently in the registry."""
    return {
        f"{section_key}.{notification['path']}"
        for section_key, section in NOTIFICATIONS.items()
        for notification in section
    }


def _registered_template_paths():
    """Return the set of every template path declared by a registered notification."""
    return {
        f"{section_key}/{tmpl['path']}"
        for section_key, section in NOTIFICATIONS.items()
        for notification in section
        for tmpl in notification["templates"]
    }


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
        parser.add_argument(
            "--prune",
            action="store_true",
            default=False,
            help="Delete Notification rows whose key is no longer in the "
            "NOTIFICATIONS registry, along with any of their templates that "
            "are not shared with a registered notification and have no "
            "operator-customised content. Without this flag, orphaned rows "
            "are only reported. Never enabled by default (e.g. by initdb) — "
            "an unattended boot should not delete data.",
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

        self._handle_orphans(prune=options["prune"])

    # ------------------------------------------------------------------
    # Orphaned rows: report always, delete only with --prune
    # ------------------------------------------------------------------

    def _handle_orphans(self, prune):
        """
        Report every Notification row whose key is no longer registered, and
        (with --prune) delete it along with its safe-to-remove templates.

        A template is safe to remove only if no *registered* notification still
        declares its path, and it carries no operator-customised content —
        either condition alone is enough to keep it.
        """
        registered_keys = _registered_keys()
        registered_template_paths = _registered_template_paths()

        orphans = Notification.objects.exclude(key__in=registered_keys)
        if not orphans.exists():
            return

        templates_to_delete = set()

        for notification in orphans:
            self.stdout.write(
                self.style.WARNING(
                    f"Orphaned notification '{notification.key}' "
                    f"(enabled={notification.enabled}) has no matching key in "
                    "the NOTIFICATIONS registry."
                )
            )
            logger.warning(
                "Orphaned notification '%s' (enabled=%s): key is no longer registered.",
                notification.key,
                notification.enabled,
            )

            for template in notification.templates.all():
                if template.path in registered_template_paths:
                    status = "shared with a registered notification, keeping"
                elif template.content:
                    status = "has customised content, keeping"
                else:
                    status = "safe to remove" if prune else "would be removed"
                    templates_to_delete.add(template.pk)
                self.stdout.write(f"  template '{template.path}': {status}")

        if not prune:
            self.stdout.write(
                self.style.WARNING(
                    "Re-run with --prune to delete the orphaned notification(s) "
                    "listed above."
                )
            )
            return

        with transaction.atomic():
            template_count = len(templates_to_delete)
            NotificationTemplate.objects.filter(pk__in=templates_to_delete).delete()
            notification_count = orphans.count()
            orphans.delete()

        self.stdout.write(
            self.style.WARNING(
                f"Pruned {notification_count} orphaned notification(s) and "
                f"{template_count} orphaned template(s)."
            )
        )
        logger.info(
            "Pruned %d orphaned notification(s) and %d orphaned template(s).",
            notification_count,
            template_count,
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
        Ensure a NotificationTemplate row exists for *template_path*, with blank
        content on first creation — existing entries (including user overrides via
        override_templates) are never touched.

        Blank content means "no override, use the filesystem template" everywhere
        else in the code. Seeding it with the filesystem source instead would
        freeze that content at whatever it was when the row was first created:
        get_or_create's defaults only apply once, so a later release that changes
        the shipped template would never reach users (the loader keeps serving the
        frozen DB copy), and the template would incorrectly start reporting as
        overridden despite nobody having touched it.
        """
        try:
            notification_template, _ = NotificationTemplate.objects.get_or_create(
                path=template_path,
                defaults={"name": template_name},
            )
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
