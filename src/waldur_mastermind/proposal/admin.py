from django.contrib import admin

from . import models
from .enums import ProposalStates


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

    # What a submitted proposal asks for stays as it was submitted.
    def _submitted(self, obj):
        return obj is not None and obj.state != ProposalStates.DRAFT

    def has_add_permission(self, request, obj=None):
        return not self._submitted(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not self._submitted(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._submitted(obj) and super().has_delete_permission(request, obj)


class CallAdmin(admin.ModelAdmin):
    inlines = [RequestedOfferingInline, RoundInline, CallResourceTemplateInline]
    list_display = (
        "name",
        "state",
        "fixed_duration_in_days",
        "reviewer_identity_visible_to_submitters",
        "reviews_visible_to_submitters",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "description",
                    "manager",
                    "state",
                )
            },
        ),
        (
            "Configuration",
            {
                "fields": (
                    "fixed_duration_in_days",
                    "allowed_duration_months",
                    "external_url",
                    "backend_id",
                )
            },
        ),
        (
            "Visibility settings",
            {
                "fields": (
                    "reviewer_identity_visible_to_submitters",
                    "reviews_visible_to_submitters",
                ),
                "description": "Control what proposal submitters can see about reviews",
            },
        ),
    )


class ProposalProjectRoleMappingAdmin(admin.ModelAdmin):
    list_display = ("call", "proposal_role", "project_role")
    search_fields = ("call__name", "proposal_role__name", "project_role__name")


class RoundAdmin(admin.ModelAdmin):
    list_display = ("call", "start_time", "cutoff_time")


class ProposalAdmin(admin.ModelAdmin):
    """A submitted proposal is the applicant's and the workflow's: read-only
    here, where a change would leave no trace on its feed."""

    inlines = [RequestedResourceInline]
    list_display = ("__str__", "get_state_display")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.state != ProposalStates.DRAFT:
            return [field.name for field in self.model._meta.concrete_fields]
        return super().get_readonly_fields(request, obj)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # The column's max_length is the API's rule for new input, not a bound
        # on what is stored: a summary written over it before the limit
        # existed must stay editable here, and ORM writes are not checked.
        if db_field.name == "project_summary":
            kwargs["max_length"] = None
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class ReviewAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "proposal")


class ReviewerProfileAdmin(admin.ModelAdmin):
    """Read-mostly: standing is set through the staff reviewer registry
    (/api/reviewer-registry/), which records who set it and why and logs an
    event; it is shown here, not edited."""

    list_display = (
        "user",
        "standing",
        "is_published",
        "available_for_reviews",
        "standing_changed_at",
    )
    list_filter = ("standing", "is_published", "available_for_reviews")
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
    )
    raw_id_fields = ("user",)
    readonly_fields = (
        "standing",
        "standing_reason",
        "standing_changed_by",
        "standing_changed_at",
        "is_published",
        "published_at",
    )


class CallReviewerPoolAdmin(admin.ModelAdmin):
    list_display = ("call", "reviewer", "invited_email", "invitation_status")
    list_filter = ("invitation_status",)
    raw_id_fields = ("call", "reviewer", "invited_user", "invited_by", "overridden_by")


class ConflictOfInterestAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "proposal", "coi_type", "severity", "status")
    list_filter = ("status", "severity")
    raw_id_fields = ("reviewer", "proposal", "call")


class COIDisclosureFormAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "call", "certified", "is_current", "valid_until")
    list_filter = ("certified", "is_current")
    raw_id_fields = ("reviewer", "call")


admin.site.register(models.ReviewerProfile, ReviewerProfileAdmin)
admin.site.register(models.CallReviewerPool, CallReviewerPoolAdmin)
admin.site.register(models.ConflictOfInterest, ConflictOfInterestAdmin)
admin.site.register(models.COIDisclosureForm, COIDisclosureFormAdmin)
admin.site.register(models.CallManagingOrganisation)
admin.site.register(models.Call, CallAdmin)
admin.site.register(models.ProposalProjectRoleMapping, ProposalProjectRoleMappingAdmin)
admin.site.register(models.CallResourceTemplate)
admin.site.register(models.Round, RoundAdmin)
admin.site.register(models.Proposal, ProposalAdmin)
admin.site.register(models.Review, ReviewAdmin)
