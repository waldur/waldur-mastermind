"""Re-encrypt Fernet-encrypted columns under the current primary key."""

import json

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from waldur_core.core import encryption

# Whole-column ciphertext: raw ciphertext columns and transparently-encrypted
# scalar fields alike (both store a single Fernet token in the column).
# Referenced by name rather than imported: waldur_core must not depend on the
# apps above it, and Django resolves these lazily once the registry is ready.
ENCRYPTED_SCALAR_FIELDS = [
    ("marketplace.ResourceApiKey", "key_ciphertext"),
    ("structure.ServiceSettings", "password"),
    ("structure.ServiceSettings", "token"),
]

# JSON columns that hold Fernet tokens under some keys while the rest stays
# plaintext (e.g. Offering.secret_options). Both jsonb- and text-backed JSON columns
# appear here; the scan below casts to text so it does not care which.
ENCRYPTED_JSON_FIELDS = [
    ("marketplace.Offering", "secret_options"),
    ("structure.ServiceSettings", "options"),
]

# Rows are read in batches so a full-table rotation never materialises every
# ciphertext row at once.
BATCH_SIZE = 500


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
        self.dry_run = options["dry_run"]
        self.totals = {"rotated": 0, "undecryptable": 0}

        for label, field in ENCRYPTED_SCALAR_FIELDS:
            self._process_scalar(apps.get_model(label), field)
        for label, field in ENCRYPTED_JSON_FIELDS:
            self._process_json(apps.get_model(label), field)

        verb = "would re-encrypt" if self.dry_run else "re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {self.totals['rotated']} row(s)"))
        if self.totals["undecryptable"]:
            # The operative case: a key that wrote these rows is no longer configured,
            # so their values are unrecoverable. Reveal would fail with a 409 the next
            # time somebody asked, which is a bad way to find out.
            self.stdout.write(
                self.style.ERROR(
                    f"{self.totals['undecryptable']} row(s) cannot be decrypted with "
                    "any configured key. Add the key that wrote them to "
                    "FIELD_ENCRYPTION_KEY_FALLBACKS, or replace those secrets."
                )
            )

    def _iter_rows(self, table, field, where):
        # Read the raw at-rest value, bypassing any decrypting from_db_value, so the
        # stored ciphertext can be rotated directly. Collect the (cheap) ids first, then
        # read values one batch at a time — never holding the whole table in memory, and
        # keeping reads separate from the updates that reuse the same connection.
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {table} WHERE {where}"  # noqa: S608 (trusted names)
            )
            ids = [row[0] for row in cursor.fetchall()]
        for start in range(0, len(ids), BATCH_SIZE):
            batch = ids[start : start + BATCH_SIZE]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT id, {field} FROM {table} WHERE id = ANY(%s)",  # noqa: S608
                    [batch],
                )
                yield from cursor.fetchall()

    def _write(self, model, pk, field, value):
        if self.dry_run:
            return
        # .update() bypasses pre_save, so the rotated ciphertext is stored verbatim.
        with transaction.atomic():
            model.objects.filter(pk=pk).update(**{field: value})

    def _process_scalar(self, model, field):
        name = model._meta.label
        rows = self._iter_rows(
            model._meta.db_table, field, f"{field} IS NOT NULL AND {field} <> ''"
        )
        for pk, value in rows:
            if not encryption.is_encrypted(value):
                # Plaintext left by a deployment that predates encryption: encrypting
                # it here would be a silent data change, so leave it and say so.
                self.stdout.write(
                    self.style.WARNING(f"{name} {pk}: {field} is not encrypted")
                )
                continue
            try:
                rotated = encryption.rotate_value(value)
            except InvalidToken:
                self.totals["undecryptable"] += 1
                self.stdout.write(
                    self.style.ERROR(f"{name} {pk}: undecryptable, left untouched")
                )
                continue
            self.totals["rotated"] += 1
            self._write(model, pk, field, rotated)

    def _process_json(self, model, field):
        name = model._meta.label
        # Which keys hold credentials is the field's own business, and it already
        # answers exactly that question for pre_save/from_db_value. Asking the field
        # keeps this command free of any dependency on the apps above waldur_core, and
        # means a classification change is picked up here with no second list to edit.
        is_sensitive = model._meta.get_field(field)._is_sensitive_key
        # ::text rather than ::jsonb — Offering.secret_options is a jsonb column but
        # ServiceSettings.options is text holding serialised JSON, and both render an
        # empty object as exactly '{}'.
        rows = self._iter_rows(
            model._meta.db_table,
            field,
            f"{field} IS NOT NULL AND {field}::text NOT IN ('', '{{}}')",
        )
        for pk, raw in rows:
            try:
                # A jsonb column comes back parsed; a text-backed one comes back as the
                # serialised string, which is not guaranteed to be valid JSON.
                data = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                self.stdout.write(
                    self.style.WARNING(f"{name} {pk}: {field} is not valid JSON")
                )
                continue
            if not isinstance(data, dict):
                continue
            result = {}
            changed = undecryptable = False
            for key, value in data.items():
                if not encryption.is_encrypted(value):
                    if is_sensitive(key) and isinstance(value, str) and value:
                        # A credential sitting in plaintext under a sensitive key.
                        # Most values in these columns are legitimately plaintext, so
                        # unlike the scalar case this cannot be inferred from the value
                        # alone — the field's own classifier decides. Encrypting it here
                        # would be a silent data change, so report it and move on.
                        self.stdout.write(
                            self.style.WARNING(
                                f"{name} {pk}: {field}[{key}] is not encrypted"
                            )
                        )
                    result[key] = value
                    continue
                try:
                    result[key] = encryption.rotate_value(value)
                    changed = True
                except InvalidToken:
                    result[key] = value
                    undecryptable = True
            if undecryptable:
                self.totals["undecryptable"] += 1
                self.stdout.write(
                    self.style.ERROR(f"{name} {pk}: undecryptable value(s), left as-is")
                )
            if changed:
                self.totals["rotated"] += 1
                self._write(model, pk, field, result)
