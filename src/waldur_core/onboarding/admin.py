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
            {"fields": ["customer"]},
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


class OnboardingCountryChecklistConfigurationAdmin(admin.ModelAdmin):
    list_display = [
        "country",
        "checklist",
        "is_active",
        "created",
    ]
    list_filter = ["is_active", "country", "created"]
    search_fields = ["country", "checklist__name"]
    readonly_fields = ["uuid", "created", "modified"]

    fieldsets = [
        (
            "Configuration",
            {"fields": ["uuid", "country", "checklist", "is_active"]},
        ),
        ("Timeline", {"fields": ["created", "modified"], "classes": ["collapse"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("checklist")


admin.site.register(
    models.OnboardingCountryChecklistConfiguration,
    OnboardingCountryChecklistConfigurationAdmin,
)


class OnboardingQuestionMetadataAdmin(admin.ModelAdmin):
    list_display = [
        "question_description",
        "maps_to_customer_field",
        "intent_field",
        "created",
    ]
    list_filter = ["created"]
    search_fields = ["question__description", "maps_to_customer_field", "intent_field"]
    readonly_fields = ["uuid", "created", "modified"]
    autocomplete_fields = ["question"]

    fieldsets = [
        (
            "Question",
            {"fields": ["uuid", "question"]},
        ),
        (
            "Mapping Configuration",
            {
                "fields": [
                    "maps_to_customer_field",
                    "intent_field",
                ],
                "description": "Configure how this question's answer should be mapped. "
                "Use maps_to_customer_field for Customer model fields, "
                "intent_field for intent/purpose data that stays with verification.",
            },
        ),
        ("Timeline", {"fields": ["created", "modified"], "classes": ["collapse"]}),
    ]

    def question_description(self, obj):
        return obj.question.description[:80]

    question_description.short_description = "Question"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("question__checklist")


admin.site.register(models.OnboardingQuestionMetadata, OnboardingQuestionMetadataAdmin)
