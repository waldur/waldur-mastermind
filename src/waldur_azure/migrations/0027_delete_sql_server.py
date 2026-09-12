from django.db import migrations
from django.utils import timezone

# Azure retired Database for PostgreSQL Single Server, which the Azure.SQLServer
# offering provisioned, so nothing behind these rows exists any more. They are
# deleted rather than archived. The migration is irreversible: reversing it
# restores the migration record, not the data.
#
# Rows are deleted through historical models, so no signal handlers run. The
# steps below do by hand what those handlers, or GenericForeignKey cascades
# (which the database does not enforce), would otherwise have done.

SQL_SERVER_OFFERING_TYPE = "Azure.SQLServer"
SQL_MODELS = ("sqlserver", "sqldatabase")

# (app_label, model, content type field, object id field) for generic pointers
# that may be empty: these rows outlive the SQL server, so only the pointer goes.
NULLABLE_POINTERS = (
    ("marketplace", "Resource", "content_type", "object_id"),
    ("permissions", "UserRole", "content_type", "object_id"),
    ("quotas", "QuotaLimit", "content_type", "object_id"),
    ("quotas", "QuotaUsage", "content_type", "object_id"),
    ("billing", "PriceEstimate", "content_type", "object_id"),
    ("support", "Issue", "resource_content_type", "resource_object_id"),
    ("structure", "ServiceSettings", "content_type", "object_id"),
)

# Generic pointers that cannot be empty: the rows only describe the SQL server.
REQUIRED_POINTERS = (
    ("logging", "Feed"),
    ("analytics", "DailyQuotaHistory"),
    ("permissions", "RoleAvailability"),
    ("user_actions", "UserAction"),
    ("reversion", "Version"),
)


def _sql_content_types(apps):
    ContentType = apps.get_model("contenttypes", "ContentType")
    return ContentType.objects.filter(app_label="waldur_azure", model__in=SQL_MODELS)


def _revoke_roles(apps, model, object_ids):
    """Revoke active roles scoped to marketplace rows about to be deleted.

    Deleting an offering through the live model revokes its roles in a
    pre_delete handler; without that, the roles stay active with a scope that
    no longer resolves and cannot be revoked through the API. Resources get the
    same treatment for the same reason.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    UserRole = apps.get_model("permissions", "UserRole")
    content_type = ContentType.objects.filter(
        app_label="marketplace", model=model
    ).first()
    if content_type is None or not object_ids:
        return
    UserRole.objects.filter(
        content_type=content_type, object_id__in=object_ids, is_active=True
    ).update(
        is_active=False,
        expiration_time=timezone.now(),
        revoke_reason="Azure SQL Server offering removed",
    )


def delete_sql_server_data(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    Resource = apps.get_model("marketplace", "Resource")

    offerings = Offering.objects.filter(type=SQL_SERVER_OFFERING_TYPE)
    offering_ids = list(offerings.values_list("id", flat=True))
    resource_ids = list(
        Resource.objects.filter(offering_id__in=offering_ids).values_list(
            "id", flat=True
        )
    )
    _revoke_roles(apps, "offering", offering_ids)
    _revoke_roles(apps, "resource", resource_ids)

    # Cascades to plans, resources, orders and usages. Invoice items keep their
    # history: their resource foreign key is SET_NULL.
    offerings.delete()

    sql_cts = _sql_content_types(apps)
    for app_label, model_name, ct_field, id_field in NULLABLE_POINTERS:
        model = apps.get_model(app_label, model_name)
        model.objects.filter(**{f"{ct_field}__in": sql_cts}).update(
            **{ct_field: None, id_field: None}
        )
    for app_label, model_name in REQUIRED_POINTERS:
        model = apps.get_model(app_label, model_name)
        model.objects.filter(content_type__in=sql_cts).delete()


def delete_sql_content_types(apps, schema_editor):
    """Remove the content types once nothing points at them.

    delete_sql_server_data needs them to find the pointers, so they go last.
    This cascades to the matching auth.Permission rows, as
    `remove_stale_contenttypes` would.
    """
    _sql_content_types(apps).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_azure", "0001_squashed_0026"),
        ("marketplace", "0286_service_provider_account"),
        ("invoices", "0031_credit_offerings_help_text"),
        ("structure", "0085_remove_project_display_credit_reports"),
        ("permissions", "0028_userrole_source"),
        ("quotas", "0005_drop_zero_usage"),
        ("billing", "0002_drop_limit_and_threshold"),
        ("support", "0001_squashed_0027"),
        ("logging", "0029_eventconsumer_auth_attribution"),
        ("analytics", "0001_squashed_0003"),
        ("user_actions", "0001_squashed_0007"),
        ("reversion", "0002_add_index_on_version_for_content_type_and_db"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(delete_sql_server_data, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="sqlserver",
            name="project",
        ),
        migrations.RemoveField(
            model_name="sqlserver",
            name="resource_group",
        ),
        migrations.RemoveField(
            model_name="sqlserver",
            name="service_settings",
        ),
        migrations.DeleteModel(
            name="SQLDatabase",
        ),
        migrations.DeleteModel(
            name="SQLServer",
        ),
        migrations.RunPython(delete_sql_content_types, migrations.RunPython.noop),
    ]
