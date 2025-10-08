from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core.models import ErrorMessageMixin, TimeStampedModel, User, UuidMixin
from waldur_core.structure import models as structure_models

from . import enums


class OnboardingVerification(UuidMixin, ErrorMessageMixin, TimeStampedModel):
    """
    Tracks company onboarding validation attempts.

    This model records the validation process from request to completion,
    supporting future extension to multiple validation methods.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="onboarding_verifications",
        help_text=_("User requesting company onboarding"),
    )

    # Company details for validation
    country = models.CharField(
        max_length=2, help_text=_("ISO country code (e.g., 'EE' for Estonia)")
    )
    legal_person_identifier = models.CharField(
        max_length=50, help_text=_("Official company registration code")
    )
    legal_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Claimed company name (optional, for reference)"),
    )

    # Customer creation metadata
    # Format: dict with Customer model fields, e.g.:
    # {
    #     "name": "Company Name",
    #     "country": "EE",
    #     "registration_code": "12345678",
    # }
    user_submitted_customer_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Additional customer metadata submitted by user for manual verification cases. "
            "Should contain valid Customer model fields."
        ),
    )

    status = models.CharField(
        max_length=20,
        choices=enums.VerificationStatus.CHOICES,
        default=enums.VerificationStatus.PENDING,
    )
    validation_method = models.CharField(
        max_length=50,
        choices=enums.ValidationMethod.CHOICES,
        blank=True,
        help_text=_("Method used for validation"),
    )
    # Detailed results
    verified_user_roles = models.JSONField(
        default=list, help_text=_("Roles the user has in the company")
    )
    verified_company_data = models.JSONField(
        default=dict, help_text=_("Company information retrieved during validation")
    )
    raw_response = models.JSONField(
        default=dict, help_text=_("Raw API response for debugging and auditing")
    )

    # Timeline tracking
    validated_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When validation was completed")
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When this verification expires")
    )

    # Result - created after successful validation
    customer = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onboarding_verifications",
        help_text=_("Customer created after successful validation"),
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"Verification {self.uuid} - {self.country}/{self.legal_person_identifier} - {self.status}"

    def can_create_customer(self):
        """Check if customer can be created from this verification."""
        if not self.status == enums.VerificationStatus.VERIFIED:
            return False

        if self.customer is not None:
            return False

        customer_exists = structure_models.Customer.objects.filter(
            registration_code=self.legal_person_identifier, country=self.country
        ).exists()

        if customer_exists:
            return False

        return True

    def create_customer_if_verified(self):
        """Create customer if verification is successful and no customer exists."""
        if not self.can_create_customer():
            raise ValueError(
                "Cannot create customer: verification not valid or customer with same registration code already exists"
            )

        # Prioritize API data from verified_company_data, fallback to user_submitted_customer_metadata for manual cases
        customer_data = {
            "name": (
                self.verified_company_data.get("name")  # First priority: API data
                or self.user_submitted_customer_metadata.get(
                    "name"
                )  # Second priority: manual input
                or self.legal_name  # Third priority: reference name from request
                or f"Company {self.legal_person_identifier}"  # Fallback: generated name
            ),
            "country": self.country,
            "registration_code": self.legal_person_identifier,
        }

        # Add any additional fields from user_submitted_customer_metadata that aren't overridden
        for key, value in self.user_submitted_customer_metadata.items():
            if key not in customer_data and value:  # Don't override existing fields
                customer_data[key] = value

        # Create the customer
        self.customer = structure_models.Customer.objects.create(**customer_data)
        self.save(update_fields=["customer"])

        return self.customer


class OnboardingJustification(UuidMixin, TimeStampedModel):
    """
    User justification when automatic validation fails.

    Allows users to explain why they should be authorized even when
    automatic validation fails (e.g., research groups, subsidiaries).
    """

    verification = models.ForeignKey(
        OnboardingVerification, on_delete=models.CASCADE, related_name="justifications"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="onboarding_justifications"
    )
    user_justification = models.TextField(
        help_text=_("User's explanation for why they should be authorized")
    )

    # Review status
    validated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_justifications",
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_decision = models.CharField(
        max_length=20,
        choices=enums.ReviewDecision.CHOICES,
        default=enums.ReviewDecision.PENDING,
    )
    staff_notes = models.TextField(
        blank=True, help_text=_("Administrator notes on the review decision")
    )

    def __str__(self):
        return f"Justification for {self.verification} by {self.user}"


class OnboardingJustificationDocumentation(
    TimeStampedModel,
    UuidMixin,
):
    """Supporting documentation files uploaded for manual onboarding justifications."""

    justification = models.ForeignKey(
        OnboardingJustification,
        on_delete=models.CASCADE,
        related_name="supporting_documentation",
    )
    file = models.FileField(
        upload_to="onboarding_justification_documentation",
        blank=True,
        null=True,
        help_text=_("Upload supporting documentation."),
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"Documentation for {self.justification} - {self.file.name if self.file else 'No file'}"
