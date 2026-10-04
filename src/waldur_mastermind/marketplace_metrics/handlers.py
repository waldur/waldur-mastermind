from constance import config
from django.db import connection
from rest_framework import serializers

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates

from . import enums, models


def projects_with_metrics(project_ids):
    """Projects with a running resource whose offering reports a metric."""
    return set(
        marketplace_models.Resource.objects.filter(project_id__in=project_ids)
        .exclude(state=ResourceStates.TERMINATED)
        .filter(
            offering__metrics__state__in=(
                enums.OfferingMetricStates.ACTIVE,
                enums.OfferingMetricStates.PAUSED,
            )
        )
        .values_list("project_id", flat=True)
        .distinct()
    )


def get_has_metrics(serializer, project) -> bool:
    bulk_data = serializer.context.get("bulk_data", {})
    if "project_has_metrics" in bulk_data:
        return project.id in bulk_data["project_has_metrics"]
    return project.id in projects_with_metrics([project.id])


def add_has_metrics(sender, fields, **kwargs):
    """The project Metrics tab is shown only where this is true."""
    fields["has_metrics"] = serializers.SerializerMethodField()
    setattr(sender, "get_has_metrics", get_has_metrics)


def create_default_retention_policy(sender, **kwargs):
    """The policy definitions without one fall back to.

    Created after every migrate rather than by a data migration, so a
    database built straight from the models gets it too. Skipped when the
    table is absent, as after migrating this app back to zero.
    """
    table = models.RetentionPolicy._meta.db_table
    if table not in connection.introspection.table_names():
        return
    name = config.METRICS_DEFAULT_RETENTION_POLICY or "standard"
    if not models.RetentionPolicy.objects.filter(name=name).exists():
        models.RetentionPolicy.objects.create(name=name)
