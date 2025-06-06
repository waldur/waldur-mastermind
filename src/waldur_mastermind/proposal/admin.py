from django.contrib import admin

from . import models


class RequestedOfferingInline(admin.TabularInline):
    model = models.RequestedOffering
    extra = 1


class RoundInline(admin.TabularInline):
    model = models.Round
    extra = 1


class CallResourceTemplateInline(admin.TabularInline):
    model = models.CallResourceTemplate
    extra = 0
    readonly_fields = ("created_by",)


class RequestedResourceInline(admin.TabularInline):
    model = models.RequestedResource
    extra = 1


class CallAdmin(admin.ModelAdmin):
    inlines = [RequestedOfferingInline, RoundInline, CallResourceTemplateInline]
    list_display = ("name", "fixed_duration_in_days", "state")
    fieldsets = (
        (None, {"fields": ("name", "description", "manager", "state")}),
        (
            "Configuration",
            {
                "fields": (
                    "fixed_duration_in_days",
                    "default_project_role",
                    "external_url",
                    "backend_id",
                )
            },
        ),
    )


class RoundAdmin(admin.ModelAdmin):
    list_display = ("call", "start_time", "cutoff_time")


class ProposalAdmin(admin.ModelAdmin):
    inlines = [RequestedResourceInline]
    list_display = ("__str__", "get_state_display")


class ReviewAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "proposal")


admin.site.register(models.CallManagingOrganisation)
admin.site.register(models.Call, CallAdmin)
admin.site.register(models.CallResourceTemplate)
admin.site.register(models.Round, RoundAdmin)
admin.site.register(models.Proposal, ProposalAdmin)
admin.site.register(models.Review, ReviewAdmin)
