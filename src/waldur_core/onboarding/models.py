import logging

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.core.models import ErrorMessageMixin, TimeStampedModel, User, UuidMixin
from waldur_core.structure import models as structure_models

from . import enums

logger = logging.getLogger(__name__)


class OnboardingCountryChecklistConfiguration(UuidMixin, TimeStampedModel):
    """
    Maps countries to their onboarding checklists.

    This allows flexible configuration of which checklist to use for each country.
    """

    country = models.CharField(
        max_length=2,
        unique=True,
        help_text=_("ISO country code (e.g., 'EE' for Estonia)"),
    )
    checklist = models.ForeignKey(
        checklist_models.Checklist,
        on_delete=models.CASCADE,
        related_name="country_configurations",
        help_text=_("Checklist to use for this country's onboarding"),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this country configuration is active"),
    )

    class Meta:
        ordering = ["country"]
        verbose_name = _("Onboarding country checklist configuration")
        verbose_name_plural = _("Onboarding country checklist configurations")

    def __str__(self):
        return f"{self.country} → {self.checklist.name}"


class OnboardingQuestionMetadata(UuidMixin, TimeStampedModel):
    """
    Stores onboarding-specific metadata for checklist questions.

    Defines how question answers should be mapped to:
    - Customer model fields (registration_code, name, email, etc.) - saved to Customer
    - Intent fields (intent, purpose, etc.) - stays with verification as metadata
    """

    question = models.OneToOneField(
        checklist_models.Question,
        on_delete=models.CASCADE,
        related_name="onboarding_metadata",
        help_text=_("Question this metadata applies to"),
    )

    # Customer field mapping (saved to Customer model)
    maps_to_customer_field = models.CharField(
        max_length=100,
        blank=True,
        help_text=_(
            "Customer model field name to map this answer to (e.g., 'registration_code', 'email', 'vat_code')"
        ),
    )

    # Intent field (stays with verification as onboarding metadata)
    intent_field = models.CharField(
        max_length=50,
        blank=True,
        help_text=_(
            "Type of intent/purpose field (e.g., 'intent', 'registration_purpose') - stays with verification"
        ),
    )

    class Meta:
        verbose_name = _("Onboarding Question Metadata")
        verbose_name_plural = _("Onboarding Question Metadata")

    def __str__(self):
        parts = []
        if self.maps_to_customer_field:
            parts.append(f"customer:{self.maps_to_customer_field}")
        if self.intent_field:
            parts.append(f"intent:{self.intent_field}")

        metadata_str = ", ".join(parts) if parts else "no mapping"
        return f"{self.question.description[:50]} → {metadata_str}"


class OnboardingVerification(UuidMixin, ErrorMessageMixin, TimeStampedModel):
    """
    Tracks company onboarding validation attempts.

    This model records the validation process from request to completion,
    supporting future extension to multiple validation methods.

    Required fields (legal_person_identifier) are provided during creation.
    Optionally uses checklist system for flexible, country-specific additional data collection.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="onboarding_verifications",
        help_text=_("User requesting company onboarding"),
    )

    # Country determines which checklist to use
    country = models.CharField(
        max_length=2, help_text=_("ISO country code (e.g., 'EE' for Estonia)")
    )

    legal_person_identifier = models.CharField(
        max_length=50,
        blank=True,
        help_text=_(
            "Official company registration code (required for automatic validation)"
        ),
    )
    legal_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Company name(optional, for reference)"),
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
        return f"Verification {self.uuid} - {self.country}/{self.legal_person_identifier or 'pending'} - {self.status}"

    def get_or_create_checklist_completion(self):
        """Get or create checklist completion for this verification."""
        # Find country-specific onboarding checklist via CountryChecklistConfiguration
        try:
            config = OnboardingCountryChecklistConfiguration.objects.get(
                country=self.country, is_active=True
            )
            checklist = config.checklist
        except OnboardingCountryChecklistConfiguration.DoesNotExist:
            logger.warning(
                f"No onboarding checklist configuration found for country {self.country}. "
                "Staff should create a CountryChecklistConfiguration via admin."
            )
            return None

        # Validate checklist type
        if (
            checklist.checklist_type
            != checklist_enums.ChecklistTypes.CUSTOMER_ONBOARDING
        ):
            logger.error(
                f"Checklist {checklist.name} is not of type CUSTOMER_ONBOARDING. "
                f"Current type: {checklist.checklist_type}"
            )
            return None

        # Get or create completion linked to this verification via generic foreign key
        content_type = ContentType.objects.get_for_model(OnboardingVerification)
        completion, created = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id=self.id,
            )
        )

        if created:
            logger.info(
                f"Created checklist completion for verification {self.uuid} with checklist {checklist.name}"
            )

        completion.update_completion_status()

        return completion

    def extract_data_from_checklist(self):
        """
        Extract company data from checklist answers.

        Returns dict with keys 'customer_data' and 'onboarding_metadata'.
        Note: Verification fields (e.g legal_person_identifier) are provided during creation, not extracted from checklist.
        """
        completion = self.get_or_create_checklist_completion()
        if not completion:
            return {}

        answers = completion.answers.select_related("question").all()

        extracted_data = {
            "customer_data": {},
            "onboarding_metadata": {},  # Intents, purposes, etc. - not mapped to Customer
        }

        for answer in answers:
            question = answer.question
            answer_value = answer.answer_data

            # Get onboarding-specific metadata from OnboardingQuestionMetadata model
            try:
                metadata = OnboardingQuestionMetadata.objects.get(question=question)
            except OnboardingQuestionMetadata.DoesNotExist:
                # No metadata defined for this question, skip mapping
                continue

            # Map to customer fields (will be saved to Customer model)
            if metadata.maps_to_customer_field:
                extracted_data["customer_data"][metadata.maps_to_customer_field] = (
                    answer_value
                )

            # Store intent fields (intents, purposes, etc.) - stays with verification
            if metadata.intent_field and not metadata.maps_to_customer_field:
                extracted_data["onboarding_metadata"][metadata.intent_field] = (
                    answer_value
                )

        return extracted_data

    def get_onboarding_metadata(self) -> dict:
        """
        Get onboarding-specific metadata like intents, purposes, etc.

        Returns:
            dict: Onboarding metadata, e.g., {'intent': ['Research', 'Commercial'], 'purpose': 'Education'}
        """
        extracted = self.extract_data_from_checklist()
        return extracted.get("onboarding_metadata", {})

    def get_user_submitted_customer_data(self) -> dict:
        """
        Get customer-related data submitted by the user via checklist answers.

        Returns:
            dict: Customer-related data, e.g., {'email': 'john@example.com', 'vat_code': 'EE123456789'}
        """
        extracted = self.extract_data_from_checklist()
        return extracted.get("customer_data", {})

    def create_customer_if_verified(self):
        """Create customer if verification is successful and no customer exists."""
        if self.status != enums.VerificationStatus.VERIFIED:
            raise ValueError(
                "Cannot create customer: verification status must be 'verified'"
            )

        if self.customer is not None:
            raise ValueError(
                "Cannot create customer: customer already exists for this verification"
            )

        completion = self.get_or_create_checklist_completion()
        if completion and not completion.is_completed:
            raise ValueError(
                "Cannot create customer: checklist has required fields that are not completed. "
                "Please complete all required checklist questions before creating a customer."
            )

        if self.legal_person_identifier:
            customer_exists = structure_models.Customer.objects.filter(
                registration_code=self.legal_person_identifier, country=self.country
            ).exists()

            if customer_exists:
                raise ValueError(
                    f"Cannot create customer: customer with registration code "
                    f"{self.legal_person_identifier} already exists for country {self.country}"
                )

        # Extract data from checklist
        extracted = self.extract_data_from_checklist()

        # Prioritize API data from verified_company_data, fallback to checklist answers
        customer_data = {
            "name": (
                self.verified_company_data.get("name")  # First priority: API data
                or extracted["customer_data"].get(
                    "name"
                )  # Second priority: checklist answer
                or self.legal_name  # Third priority: stored legal_name
                or f"Company {self.legal_person_identifier}"  # Fallback: generated name
            ),
            "country": self.country,
            "registration_code": self.legal_person_identifier,
        }

        # Add any additional fields from checklist answers
        for key, value in extracted["customer_data"].items():
            if key not in customer_data and value:  # Don't override existing fields
                customer_data[key] = value

        # Create the customer
        self.customer = structure_models.Customer.objects.create(**customer_data)
        self.save(update_fields=["customer"])

        logger.info(
            "Customer (ID=%s) created from onboarding verification (UUID=%s)",
            self.customer.id,
            self.uuid,
        )

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
        blank=True,
        null=True,
        help_text=_("User's explanation for why they should be authorized"),
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

    def approve_justification(self, user, notes=""):
        """
        Approve the justification and update verification status to VERIFIED.

        Args:
            user: User approving the justification
            notes: Optional staff notes about the decision
        """
        self.validation_decision = enums.ReviewDecision.APPROVED
        self.validated_by = user
        self.validated_at = timezone.now()
        self.staff_notes = notes
        self.save()

        # Update verification status
        self.verification.status = enums.VerificationStatus.VERIFIED
        self.verification.save(update_fields=["status"])

        logger.info(
            "Justification (UUID=%s) for verification (UUID=%s) is approved by user %s(%s)",
            self.uuid,
            self.verification.uuid,
            user.full_name,
            user.uuid,
        )

    def reject_justification(self, user, notes=""):
        """
        Reject the justification and update verification status to FAILED.

        Args:
            user: User rejecting the justification
            notes: Optional staff notes about the decision
        """

        self.validation_decision = enums.ReviewDecision.REJECTED
        self.validated_by = user
        self.validated_at = timezone.now()
        self.staff_notes = notes
        self.save()

        # Update verification status to FAILED
        self.verification.status = enums.VerificationStatus.FAILED
        self.verification.save(update_fields=["status"])

        logger.info(
            "Justification (UUID=%s) for verification (UUID=%s) is rejected by staff user %s(%s)",
            self.uuid,
            self.verification.uuid,
            user.full_name,
            user.uuid,
        )


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
