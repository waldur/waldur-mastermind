"""Re-encrypt Fernet-encrypted columns under the current primary key."""

import json

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from waldur_core.core import encryption, fields


def _concrete_fields(field_classes):
    """(model, field) for every concrete column whose field is one of these classes.

    Found from the field classes rather than listed by hand: a new encrypted column is
    rotated the day it is added, instead of being stranded the day an old key is
    retired. Each column is reported once, on the model that owns its table.
    """
    found = []
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for field in model._meta.local_concrete_fields:
            if isinstance(field, field_classes):
                found.append((model, field))
    return sorted(found, key=lambda pair: (pair[0]._meta.label, pair[1].name))


def encrypted_scalar_fields():
    """Columns holding one Fernet token: transparently encrypted or written raw."""
    return _concrete_fields((fields.EncryptedTextField, fields.CiphertextField))


def encrypted_json_fields():
    """JSON columns that hold Fernet tokens under some keys, plaintext elsewhere."""
    return _concrete_fields(fields.SelectiveEncryptionMixin)


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

        for model, field in encrypted_scalar_fields():
            self._process_scalar(model, field)
        for model, field in encrypted_json_fields():
            self._process_json(model, field)

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

    def _iter_rows(self, model, column, condition):
        """Yield (pk, raw value) for the rows matching ``condition``.

        Reads the raw at-rest value, bypassing any decrypting from_db_value, so the
        stored ciphertext can be rotated directly. Collects the (cheap) keys first,
        then reads values one batch at a time — never holding the whole table in
        memory, and keeping reads separate from the updates that reuse the same
        connection. ``condition`` names the column as ``{col}``.
        """
        quote = connection.ops.quote_name
        table = quote(model._meta.db_table)
        pk = quote(model._meta.pk.column)
        col = quote(column)
        where = condition.format(col=col)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {pk} FROM {table} WHERE {where}"  # noqa: S608 (model metadata)
            )
            keys = [row[0] for row in cursor.fetchall()]
        for start in range(0, len(keys), BATCH_SIZE):
            batch = keys[start : start + BATCH_SIZE]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {pk}, {col} FROM {table} WHERE {pk} = ANY(%s)",  # noqa: S608
                    [batch],
                )
                yield from cursor.fetchall()

    def _write(self, model, pk, field, value):
        if self.dry_run:
            return
        # .update() bypasses pre_save, so the rotated ciphertext is stored verbatim.
        # The base manager, because a default manager may hide rows: one it skipped
        # would be counted as rotated and become unreadable once the old key goes.
        with transaction.atomic():
            updated = model._base_manager.filter(pk=pk).update(**{field.attname: value})
        if updated != 1:
            raise CommandError(
                f"{model._meta.label} {pk}: {field.name} was not rewritten; "
                "keep the previous key in FIELD_ENCRYPTION_KEY_FALLBACKS"
            )

    def _process_scalar(self, model, field):
        name = model._meta.label
        rows = self._iter_rows(model, field.column, "{col} IS NOT NULL AND {col} <> ''")
        for pk, value in rows:
            if not encryption.is_encrypted(value):
                # Plaintext left by a deployment that predates encryption: encrypting
                # it here would be a silent data change, so leave it and say so.
                self.stdout.write(
                    self.style.WARNING(f"{name} {pk}: {field.name} is not encrypted")
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
        is_sensitive = field._is_sensitive_key
        # ::text rather than ::jsonb — Offering.secret_options is a jsonb column but
        # ServiceSettings.options is text holding serialised JSON, and both render an
        # empty object as exactly '{}'.
        rows = self._iter_rows(
            model, field.column, "{col} IS NOT NULL AND {col}::text NOT IN ('', '{{}}')"
        )
        for pk, raw in rows:
            try:
                # A jsonb column comes back parsed; a text-backed one comes back as the
                # serialised string, which is not guaranteed to be valid JSON.
                data = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                self.stdout.write(
                    self.style.WARNING(f"{name} {pk}: {field.name} is not valid JSON")
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
                                f"{name} {pk}: {field.name}[{key}] is not encrypted"
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
