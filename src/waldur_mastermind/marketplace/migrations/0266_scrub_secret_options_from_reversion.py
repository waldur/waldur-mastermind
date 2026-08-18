"""Strip plaintext secret_options from existing reversion history for Offering.

secret_options is now excluded from reversion tracking, and its values are encrypted
at rest in the live column — but historical ``Version.serialized_data`` still holds
them in plaintext. Remove the key from every Offering version so no cleartext secret
survives in the reversion table. Idempotent: versions without the key are left as-is.

``django_reversion`` is by far the largest table this feature touches, so the scan is
kept as narrow and as cheap as the data allows:

* the ``serialized_data__contains`` filter makes Postgres discard versions that do not
  mention the key, so only candidates are shipped to Python and a re-run after a
  partial pass matches almost nothing;
* rows are walked in id-ordered keyset batches and only the two needed columns are
  loaded, so history of any size is processed in bounded memory;
* each batch is written with one ``bulk_update`` instead of a ``save()`` per version,
  which is what made this O(number of versions) round trips before.
"""

import json

from django.db import migrations

# Deliberately smaller than the other backfills: each row carries a whole serialised
# offering, and bulk_update inlines every value into one CASE statement, so a large
# batch here means a multi-megabyte query rather than a cheap one.
BATCH_SIZE = 200


def _scrub_batch(versions):
    """Return the versions whose serialized_data actually changed."""
    changed = []
    for version in versions:
        try:
            objects = json.loads(version.serialized_data)
        except (ValueError, TypeError):
            continue
        dirty = False
        for obj in objects:
            fields = obj.get("fields") if isinstance(obj, dict) else None
            if isinstance(fields, dict) and "secret_options" in fields:
                del fields["secret_options"]
                dirty = True
        if dirty:
            version.serialized_data = json.dumps(objects)
            changed.append(version)
    return changed


def scrub_secret_options(apps, schema_editor):
    Version = apps.get_model("reversion", "Version")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        content_type = ContentType.objects.get(
            app_label="marketplace", model="offering"
        )
    except ContentType.DoesNotExist:
        return

    # The substring test is a pre-filter, not the parse: only versions that mention
    # the key are deserialised, and the JSON walk below decides what really changes.
    candidates = (
        Version.objects.filter(content_type=content_type)
        .filter(serialized_data__contains='"secret_options"')
        .only("id", "serialized_data")
        .order_by("id")
    )

    last_id = 0
    while True:
        batch = list(candidates.filter(id__gt=last_id)[:BATCH_SIZE])
        if not batch:
            return
        last_id = batch[-1].id
        changed = _scrub_batch(batch)
        if changed:
            Version.objects.bulk_update(
                changed, ["serialized_data"], batch_size=BATCH_SIZE
            )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0265_encrypt_existing_secret_options"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
    ]

    operations = [
        migrations.RunPython(
            scrub_secret_options, migrations.RunPython.noop, elidable=True
        ),
    ]
