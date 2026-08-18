from django.db import migrations


def _slurm_content_types(apps):
    ContentType = apps.get_model("contenttypes", "ContentType")
    return ContentType.objects.filter(app_label="waldur_slurm")


def nullify_slurm_scopes(apps, schema_editor):
    slurm_cts = _slurm_content_types(apps)

    # --- models with nullable content_type: set scope to NULL ---

    Resource = apps.get_model("marketplace", "Resource")
    Resource.objects.filter(content_type__in=slurm_cts).update(
        content_type=None, object_id=None
    )

    QuotaLimit = apps.get_model("quotas", "QuotaLimit")
    QuotaLimit.objects.filter(content_type__in=slurm_cts).update(
        content_type=None, object_id=None
    )

    QuotaUsage = apps.get_model("quotas", "QuotaUsage")
    QuotaUsage.objects.filter(content_type__in=slurm_cts).update(
        content_type=None, object_id=None
    )

    UserRole = apps.get_model("permissions", "UserRole")
    UserRole.objects.filter(content_type__in=slurm_cts).update(
        content_type=None, object_id=None
    )

    Issue = apps.get_model("support", "Issue")
    Issue.objects.filter(resource_content_type__in=slurm_cts).update(
        resource_content_type=None, resource_object_id=None
    )

    PriceEstimate = apps.get_model("billing", "PriceEstimate")
    PriceEstimate.objects.filter(content_type__in=slurm_cts).update(
        content_type=None, object_id=None
    )

    # --- models with non-nullable content_type: delete the rows ---

    Feed = apps.get_model("logging", "Feed")
    Feed.objects.filter(content_type__in=slurm_cts).delete()

    DailyQuotaHistory = apps.get_model("analytics", "DailyQuotaHistory")
    DailyQuotaHistory.objects.filter(content_type__in=slurm_cts).delete()

    RoleAvailability = apps.get_model("permissions", "RoleAvailability")
    RoleAvailability.objects.filter(content_type__in=slurm_cts).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0263_remove_category_default_tenant_category"),
        ("quotas", "0005_drop_zero_usage"),
        ("logging", "0024_alter_webhook_destination_url"),
        ("analytics", "0001_squashed_0003"),
        ("billing", "0002_drop_limit_and_threshold"),
        ("support", "0021_allow_nullable_request_type_backend_id"),
        ("permissions", "0021_alter_role_name_roleavailability"),
    ]

    operations = [
        migrations.RunPython(nullify_slurm_scopes, migrations.RunPython.noop),
    ]
