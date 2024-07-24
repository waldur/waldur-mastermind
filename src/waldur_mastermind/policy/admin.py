from django.contrib import admin

from . import models


class EstimatedCostPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "scope",
        "limit_cost",
    )


admin.site.register(models.ProjectEstimatedCostPolicy, EstimatedCostPolicyAdmin)
admin.site.register(models.CustomerEstimatedCostPolicy, EstimatedCostPolicyAdmin)
