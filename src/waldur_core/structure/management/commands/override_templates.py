"""
Management command: override_templates

Applies custom template content from a YAML file to the dbtemplates.Template table,
making those overrides active immediately for all subsequent email renders.

Input file format (YAML):

    Each key is a dbtemplates template name (the filesystem path used by Django's
    template loader, e.g. "users/invitation_created_message.html").
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

import yaml
from dbtemplates.models import Template
from dbtemplates.utils.cache import add_template_to_cache
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Override dbtemplates content from a YAML file. "
        "Use --clean to remove DB templates not present in the file."
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
            help="Remove DB templates whose names are not present in the file "
            "(full sync mode).",
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
        Delete every DBTemplate whose name is not present in *templates*.

        This brings the DB into full sync with the file — any template that was
        previously overridden but is no longer in the file reverts to the
        filesystem default on the next render.
        """
        to_delete = Template.objects.exclude(name__in=templates.keys())
        count = to_delete.count()
        if count:
            names = list(to_delete.values_list("name", flat=True))
            logger.info("Clean mode: deleting %d DB template(s): %s", count, names)
            self.stdout.write(
                self.style.WARNING(
                    f"Clean mode: removing {count} DB template(s) not in file."
                )
            )
            to_delete.delete()
        else:
            logger.debug("Clean mode: no templates to remove.")

    # ------------------------------------------------------------------
    # Override application
    # ------------------------------------------------------------------

    def _apply_overrides(self, templates):
        """Write *templates* content into the DB and refresh the dbtemplates cache."""
        for path, content in templates.items():
            self._override_template(path, content)

    def _override_template(self, path, content):
        """
        Create or update the DBTemplate for *path* and warm the dbtemplates cache.

        The cache is refreshed via add_template_to_cache so the new content is
        served on the very next render without a process restart.  This also clears
        any "notfound" sentinel that may have been planted by the loader on a
        previous miss, ensuring the DB entry is not silently bypassed.
        """
        db_template, created = Template.objects.get_or_create(
            name=path,
            defaults={"content": content},
        )
        if not created and db_template.content != content:
            db_template.content = content
            db_template.save()
            logger.info("Updated DB template: '%s'", path)
            self.stdout.write(f"  Updated: {path}")
        elif created:
            logger.info("Created DB template: '%s'", path)
            self.stdout.write(f"  Created: {path}")
        else:
            logger.debug("DB template unchanged: '%s'", path)
            self.stdout.write(f"  Unchanged: {path}")

        # Always refresh the cache so the current content is immediately active,
        # regardless of whether the DB row was just created, updated, or unchanged.
        add_template_to_cache(db_template)
