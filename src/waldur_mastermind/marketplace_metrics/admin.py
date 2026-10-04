from django.contrib import admin

from . import models


@admin.register(models.RetentionPolicy)
class RetentionPolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "raw_days", "hourly_days", "daily_days")


@admin.register(models.MetricDefinition)
class MetricDefinitionAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "kind", "unit", "owner_customer", "state")
    list_filter = ("kind", "state")
    search_fields = ("key", "name")
    raw_id_fields = ("owner_customer",)


@admin.register(models.OfferingMetric)
class OfferingMetricAdmin(admin.ModelAdmin):
    list_display = ("offering", "definition", "project_aggregation", "state")
    list_filter = ("state",)
    raw_id_fields = ("offering", "definition")
