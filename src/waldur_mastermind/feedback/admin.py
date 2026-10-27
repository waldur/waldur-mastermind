from django.contrib import admin

from . import models


@admin.register(models.FeedbackSurvey)
class FeedbackSurveyAdmin(admin.ModelAdmin):
    list_display = ("name", "checklist", "starts_at", "ends_at", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    readonly_fields = ("uuid", "created", "modified")


@admin.register(models.FeedbackRequest)
class FeedbackRequestAdmin(admin.ModelAdmin):
    list_display = ("survey", "user", "state", "prompt_count", "answered_at")
    list_filter = ("state", "survey")
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("uuid", "created", "modified")
