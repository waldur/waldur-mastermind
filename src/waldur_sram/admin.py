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
    list_display = ("display_name", "urn", "kind", "external_id", "modified")
    list_filter = ("kind",)
    search_fields = ("display_name", "urn", "external_id")
    raw_id_fields = ("members",)
    readonly_fields = ("uuid", "payload", "created", "modified")
