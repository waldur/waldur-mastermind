from django.contrib import admin

from . import models


class OnboardingVerificationAdmin(admin.ModelAdmin):
    list_display = [
        "uuid",
        "user",
        "country",
        "legal_person_identifier",
        "legal_name",
        "status",
        "validation_method",
        "created",
    ]
    list_filter = ["status", "country", "validation_method", "created"]
    search_fields = [
        "legal_person_identifier",
        "legal_name",
        "user__username",
        "user__email",
    ]
    readonly_fields = [
        "uuid",
        "validation_method",
        "verified_user_roles",
        "verified_company_data",
        "raw_response",
        "validated_at",
        "created",
        "modified",
    ]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": [
                    "uuid",
                    "user",
                    "country",
                    "legal_person_identifier",
                    "legal_name",
                ]
            },
        ),
        (
            "Validation Results",
            {
                "fields": [
                    "status",
                    "validation_method",
                    "verified_user_roles",
                    "validated_at",
                ]
            },
        ),
        (
            "Company Data",
            {"fields": ["verified_company_data"], "classes": ["collapse"]},
        ),
        (
            "Error Information",
            {"fields": ["error_traceback", "error_message"], "classes": ["collapse"]},
        ),
        ("Raw Response", {"fields": ["raw_response"], "classes": ["collapse"]}),
        (
            "Customer Creation",
            {"fields": ["user_submitted_customer_metadata", "customer"]},
        ),
        (
            "Timeline",
            {"fields": ["expires_at", "created", "modified"], "classes": ["collapse"]},
        ),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user", "customer")


class OnboardingJustificationAdmin(admin.ModelAdmin):
    list_display = [
        "uuid",
        "verification",
        "user",
        "validation_decision",
        "validated_by",
        "validated_at",
        "created",
    ]
    list_filter = ["validation_decision", "created", "validated_at"]
    search_fields = [
        "verification__legal_person_identifier",
        "user__username",
        "user_justification",
    ]
    readonly_fields = ["uuid", "created", "modified"]

    fieldsets = [
        (
            "Basic Information",
            {"fields": ["uuid", "verification", "user", "user_justification"]},
        ),
        (
            "Review Process",
            {
                "fields": [
                    "validation_decision",
                    "validated_by",
                    "validated_at",
                    "staff_notes",
                ]
            },
        ),
        ("Timeline", {"fields": ["created", "modified"], "classes": ["collapse"]}),
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("verification", "user", "validated_by")
        )


class OnboardingJustificationDocumentationAdmin(admin.ModelAdmin):
    list_display = [
        "uuid",
        "justification",
        "file",
        "created",
    ]
    list_filter = ["created"]
    search_fields = [
        "justification__verification__company_legal_person_identifier",
        "justification__user__username",
    ]
    readonly_fields = ["uuid", "created"]

    fieldsets = [
        ("Basic Information", {"fields": ["uuid", "justification", "file"]}),
        ("Timeline", {"fields": ["created"]}),
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("justification__user", "justification__verification")
        )


admin.site.register(models.OnboardingVerification, OnboardingVerificationAdmin)
admin.site.register(models.OnboardingJustification, OnboardingJustificationAdmin)
admin.site.register(
    models.OnboardingJustificationDocumentation,
    OnboardingJustificationDocumentationAdmin,
)
