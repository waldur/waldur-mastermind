"""Re-encrypt Fernet-encrypted columns under the current primary key."""

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from waldur_core.core import encryption

# Encrypted columns, as "app_label.ModelName" plus the field holding the token.
# Referenced by name rather than imported: waldur_core must not depend on the
# apps above it, and Django resolves these lazily once the registry is ready.
ENCRYPTED_FIELDS = [
    ("marketplace.ResourceApiKey", "key_ciphertext"),
]

BATCH_SIZE = 200


class Command(BaseCommand):
    help = (
        "Re-encrypt stored secrets under the current FIELD_ENCRYPTION_KEY. Run this "
        "after promoting a new key (with the previous one in "
        "FIELD_ENCRYPTION_KEY_FALLBACKS) so the old key can then be retired; rows are "
        "otherwise only re-encrypted when they happen to be rewritten. Use --dry-run "
        "to audit which rows the configured keys can still decrypt."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be re-encrypted without writing anything",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        totals = {"rotated": 0, "undecryptable": 0}

        for label, field in ENCRYPTED_FIELDS:
            model = apps.get_model(label)
            self._process(model, field, dry_run, totals)

        verb = "would re-encrypt" if dry_run else "re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {totals['rotated']} row(s)"))
        if totals["undecryptable"]:
            # The operative case: a key that wrote these rows is no longer configured,
            # so their values are unrecoverable. Reveal would fail with a 409 the next
            # time somebody asked, which is a bad way to find out.
            self.stdout.write(
                self.style.ERROR(
                    f"{totals['undecryptable']} row(s) cannot be decrypted with any "
                    "configured key. Add the key that wrote them to "
                    "FIELD_ENCRYPTION_KEY_FALLBACKS, or replace those secrets."
                )
            )

    def _process(self, model, field, dry_run, totals):
        rows = model.objects.exclude(**{field: ""}).only("pk", field)
        name = model._meta.label

        for row in rows.iterator(chunk_size=BATCH_SIZE):
            ciphertext = getattr(row, field)
            if not encryption.is_encrypted(ciphertext):
                # Plaintext left by a deployment that predates encryption: encrypting
                # it here would be a silent data change, so leave it and say so.
                self.stdout.write(
                    self.style.WARNING(f"{name} {row.pk}: {field} is not encrypted")
                )
                continue
            try:
                rotated = encryption.rotate_value(ciphertext)
            except InvalidToken:
                totals["undecryptable"] += 1
                self.stdout.write(
                    self.style.ERROR(f"{name} {row.pk}: undecryptable, left untouched")
                )
                continue

            totals["rotated"] += 1
            if dry_run:
                continue
            with transaction.atomic():
                setattr(row, field, rotated)
                row.save(update_fields=[field])
