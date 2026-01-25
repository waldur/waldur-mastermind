from django.contrib import admin

from . import models


class EstimatedCostPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "scope",
        "limit_cost",
    )


admin.site.register(models.ProjectEstimatedCostPolicy, EstimatedCostPolicyAdmin)
admin.site.register(models.CustomerEstimatedCostPolicy, EstimatedCostPolicyAdmin)


class SlurmCommandHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "command_type",
        "resource",
        "executed_at",
        "execution_mode",
        "success",
    )
    list_filter = ("command_type", "execution_mode", "success", "billing_period")
    search_fields = ("resource__name", "shell_command", "description")
    readonly_fields = (
        "uuid",
        "policy",
        "resource",
        "billing_period",
        "command_type",
        "description",
        "shell_command",
        "parameters",
        "executed_at",
        "execution_mode",
        "success",
        "error_message",
    )
    ordering = ("-executed_at",)


admin.site.register(models.SlurmCommandHistory, SlurmCommandHistoryAdmin)
