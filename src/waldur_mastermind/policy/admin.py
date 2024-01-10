from django.contrib import admin

from . import models


class ProjectEstimatedCostPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "limit_cost",
    )


admin.site.register(models.ProjectEstimatedCostPolicy, ProjectEstimatedCostPolicyAdmin)
