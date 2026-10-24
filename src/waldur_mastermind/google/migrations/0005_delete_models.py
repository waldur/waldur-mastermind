import logging

from django.db import migrations

logger = logging.getLogger(__name__)

GOOGLE_MODELS = ("googlecalendar", "googlecredentials")


def _owner(customer):
    if customer is None:
        return "no organization"
    return f"{customer.name} ({customer.uuid.hex})"


def report_google_data(apps, schema_editor):
    """Name what stays behind in Google once these records are gone.

    A calendar shared publicly keeps its public ACL in Google, and without the
    backend_id dropped here Waldur can no longer unshare it. A provider that
    authorised Waldur keeps that grant in its Google account until revoked.
    """
    db = schema_editor.connection.alias
    GoogleCalendar = apps.get_model("google", "GoogleCalendar")
    GoogleCredentials = apps.get_model("google", "GoogleCredentials")

    # A calendar without backend_id was never created in Google.
    calendars = list(
        GoogleCalendar.objects.using(db)
        .exclude(backend_id__isnull=True)
        .exclude(backend_id="")
        .select_related("offering__customer")
        .order_by("pk")
    )
    for calendar in calendars:
        logger.warning(
            "Google Calendar of offering %s (%s), organization %s: "
            "backend_id=%s, public=%s",
            calendar.offering.name,
            calendar.offering.uuid.hex,
            _owner(calendar.offering.customer),
            calendar.backend_id,
            calendar.public,
        )
    if calendars:
        logger.warning(
            "The Google Calendar integration has been removed. Unshare or delete "
            "the calendars listed above in Google Calendar: Waldur no longer "
            "manages them, and public ones stay public."
        )

    credentials = list(
        GoogleCredentials.objects.using(db)
        .select_related("service_provider__customer")
        .order_by("pk")
    )
    for credential in credentials:
        logger.warning(
            "Google credentials of service provider %s, organization %s",
            credential.service_provider.uuid.hex,
            _owner(credential.service_provider.customer),
        )
    if credentials:
        logger.warning(
            "The stored Google tokens of the service providers listed above are "
            "deleted. They should also revoke Waldur's access in their Google "
            "account (Security > Third-party access)."
        )


def delete_versions(apps, schema_editor):
    """Drop reversion history of the deleted models.

    OfferingAdmin is a VersionAdmin, so its GoogleCalendar inline put a
    google.googlecalendar Version into every offering revision saved in admin.
    Reverting such a revision once the model is gone fails with a 500.
    """
    db = schema_editor.connection.alias
    ContentType = apps.get_model("contenttypes", "ContentType")
    Version = apps.get_model("reversion", "Version")
    content_types = ContentType.objects.using(db).filter(
        app_label="google", model__in=GOOGLE_MODELS
    )
    Version.objects.using(db).filter(content_type__in=content_types).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("google", "0001_squashed_0004"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
    ]

    operations = [
        migrations.RunPython(report_google_data, migrations.RunPython.noop),
        migrations.RunPython(delete_versions, migrations.RunPython.noop),
        migrations.DeleteModel(
            name="GoogleCalendar",
        ),
        migrations.DeleteModel(
            name="GoogleCredentials",
        ),
    ]
