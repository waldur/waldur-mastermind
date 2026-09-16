from django.contrib import admin

from . import models


@admin.register(models.SramUser)
class SramUserAdmin(admin.ModelAdmin):
    list_display = ("external_id", "user", "modified")
    search_fields = ("external_id", "user__username", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("payload", "created", "modified")


@admin.register(models.SramGroup)
class SramGroupAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "urn",
        "kind",
        "customer",
        "external_id",
        "modified",
    )
    list_filter = ("kind",)
    search_fields = ("display_name", "urn", "external_id")
    raw_id_fields = ("members", "customer")
    readonly_fields = ("uuid", "payload", "created", "modified")


@admin.register(models.SramProjectRule)
class SramProjectRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "is_active",
        "source_kind",
        "project_field",
        "project_match",
        "project_pattern",
        "project_role",
    )
    list_filter = ("is_active", "source_kind")
    search_fields = ("name", "project_pattern")
    readonly_fields = ("uuid", "created", "modified")
