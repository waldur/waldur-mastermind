"""
Management command: override_templates

Applies custom template content from a YAML file to NotificationTemplate rows,
making those overrides active immediately for all subsequent email renders.

Input file format (YAML):

    Each key is a template path (the filesystem path used by Django's template
    loader, e.g. "users/invitation_created_message.html").
    The value is the full replacement content for that template.

    Example:

        users/invitation_created_message.html: |
          Hello {{ user.full_name }},
          You have been invited to {{ customer.name }}.

        users/invitation_created_subject.txt: |
          Invitation to {{ customer.name }}

Usage:

    # Apply overrides (updates existing, leaves unmentioned templates untouched):
    waldur override_templates /etc/waldur/custom_templates.yaml

    # Apply overrides and remove any DB template not present in the file:
    waldur override_templates /etc/waldur/custom_templates.yaml --clean
"""

import logging

import reversion
import yaml
from django.core.management.base import BaseCommand

from waldur_core.core.db_template_cache import add_template_to_cache
from waldur_core.core.models import NotificationTemplate

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Override notification template content from a YAML file. "
        "Use --clean to reset templates not present in the file to their "
        "filesystem default."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "templates_file",
            help="Path to a YAML file mapping template names to their content.",
        )
        parser.add_argument(
            "-c",
            "--clean",
            dest="clean",
            action="store_true",
            default=False,
            help="Reset templates not present in the file to their filesystem "
            "default (full sync mode).",
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        templates = self._load_file(options["templates_file"])
        if templates is None:
            logger.warning("Templates file is empty: %s", options["templates_file"])
            self.stdout.write(self.style.WARNING("Templates file is empty."))
            return

        if options["clean"]:
            self._clean_removed_templates(templates)

        self._apply_overrides(templates)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_file(self, filepath):
        """Parse *filepath* as YAML and return the resulting dict (or None)."""
        logger.info("Loading templates file: %s", filepath)
        with open(filepath) as fh:
            return yaml.safe_load(fh)

    # ------------------------------------------------------------------
    # Clean mode
    # ------------------------------------------------------------------

    def _clean_removed_templates(self, templates):
        """
        Clear the content override of every NotificationTemplate not present in
        *templates*.

        This brings the DB into full sync with the file — any template that was
        previously overridden but is no longer in the file reverts to the
        filesystem default on the next render. The NotificationTemplate row itself
        is kept (it may still be part of the notification registry), only its
        content override is cleared.
        """
        to_reset = NotificationTemplate.objects.exclude(
            path__in=templates.keys()
        ).exclude(content="")
        count = to_reset.count()
        if not count:
            logger.debug("Clean mode: no templates to reset.")
            return

        paths = list(to_reset.values_list("path", flat=True))
        logger.info("Clean mode: resetting %d template(s): %s", count, paths)
        self.stdout.write(
            self.style.WARNING(
                f"Clean mode: resetting {count} template(s) not in file."
            )
        )
        for template in to_reset:
            template.content = ""
            with reversion.create_revision():
                template.save(update_fields=["content"])
                reversion.set_comment(
                    "Reset via override_templates --clean management command"
                )
            add_template_to_cache(template)

    # ------------------------------------------------------------------
    # Override application
    # ------------------------------------------------------------------

    def _apply_overrides(self, templates):
        """Write *templates* content into the DB and refresh the template cache."""
        for path, content in templates.items():
            self._override_template(path, content)

    def _override_template(self, path, content):
        """
        Create or update the NotificationTemplate for *path* and warm the cache.

        The cache is refreshed via add_template_to_cache so the new content is
        served on the very next render without a process restart.  This also clears
        any "notfound" sentinel that may have been planted by the loader on a
        previous miss, ensuring the DB entry is not silently bypassed.
        """
        template, created = NotificationTemplate.objects.get_or_create(
            path=path,
            defaults={"name": path, "content": content},
        )
        if not created and template.content != content:
            template.content = content
            with reversion.create_revision():
                template.save(update_fields=["content"])
                reversion.set_comment(
                    "Overridden via override_templates management command"
                )
            logger.info("Updated template: '%s'", path)
            self.stdout.write(f"  Updated: {path}")
        elif created:
            logger.info("Created template: '%s'", path)
            self.stdout.write(f"  Created: {path}")
        else:
            logger.debug("Template unchanged: '%s'", path)
            self.stdout.write(f"  Unchanged: {path}")

        # Always refresh the cache so the current content is immediately active,
        # regardless of whether the row was just created, updated, or unchanged.
        add_template_to_cache(template)
