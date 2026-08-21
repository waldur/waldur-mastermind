import logging

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.core.models import ErrorMessageMixin, TimeStampedModel, User, UuidMixin
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure import models as structure_models

from . import enums

logger = logging.getLogger(__name__)


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
        ordering = ["-created", "id"]
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


class OnboardingVerification(
    UuidMixin, ErrorMessageMixin, TimeStampedModel, LoggableMixin
):
    """
    Tracks company onboarding validation attempts.

    This model records the validation process from request to completion.
    User selects a validation_method (ariregister, wirtschaftscompass, etc.) to perform automatic validation.

    Required fields (legal_person_identifier) are provided during creation.
    Uses portal-wide checklists for intent and customer data collection.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="onboarding_verifications",
        help_text=_("User requesting company onboarding"),
    )

    # Country is optional, primarily for context/display purposes
    country = models.CharField(
        max_length=2,
        blank=True,
        help_text=_(
            "ISO country code (e.g., 'EE', 'AT') for context. Can be inferred from validation_method."
        ),
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

    tracker = FieldTracker(fields=["status"])

    class Meta:
        ordering = ["-created", "id"]

    def __str__(self):
        method_display = self.validation_method or "manual"
        return f"Verification {self.uuid} - {method_display}/{self.legal_person_identifier or 'pending'} - {self.status}"

    def get_log_fields(self):
        """Define fields to include in event logs."""
        return (
            "uuid",
            "user_full_name",
            "user_username",
            "legal_person_identifier",
            "company_name",
            "validation_method",
            "status",
            "country",
        )

    @property
    def user_full_name(self):
        """Return user's full name for logging."""
        return self.user.full_name if self.user else "Unknown"

    @property
    def user_username(self):
        """Return user's username for logging."""
        return self.user.username if self.user else "Unknown"

    def get_or_create_checklist_completion(self, checklist_type):
        """
        Get or create checklist completion for this verification.

        Per deployment there should be two onboarding checklists:
        - ONBOARDING_CUSTOMER_DATA (checklist_type): Questions mapping to Customer model fields
        - ONBOARDING_INTENT_DATA (checklist_type): Questions about business intent/purpose

        Args:
            checklist_type: checklist_enums.ChecklistTypes value (ONBOARDING_CUSTOMER_DATA or ONBOARDING_INTENT_DATA)

        Returns:
            ChecklistCompletion or None if no checklist configured
        """
        # Validate checklist type
        if checklist_type not in [
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        ]:
            logger.error(f"Invalid checklist_type: {checklist_type}")
            return None

        try:
            checklist = checklist_models.Checklist.objects.get(
                checklist_type=checklist_type
            )
        except checklist_models.Checklist.DoesNotExist:
            logger.warning(
                f"No {checklist_type} checklist found. "
                "Please create a checklist with this type."
            )
            return None
        except checklist_models.Checklist.MultipleObjectsReturned:
            logger.error(
                f"Multiple {checklist_type} checklists found. "
                "Only one checklist per type should exist for onboarding."
            )
            checklist = checklist_models.Checklist.objects.filter(
                checklist_type=checklist_type
            ).first()

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
                f"Created {checklist_type} checklist completion for verification {self.uuid} with checklist {checklist.name}"
            )

        completion.update_completion_status()

        return completion

    def extract_data_from_checklist(self):
        """
        Extract data from checklist answers.
        Extracts from both CUSTOMER and INTENT checklists if configured.
        Note: Verification fields (e.g legal_person_identifier) are provided during creation, not extracted from checklist.
        """
        extracted_data = {
            "customer_data": {},
            "onboarding_metadata": {},  # Intents, purposes, etc. - not mapped to Customer
        }

        # Extract from both checklist types
        for checklist_type in [
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        ]:
            completion = self.get_or_create_checklist_completion(checklist_type)
            if not completion:
                continue

            answers = completion.answers.select_related("question").all()

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

    def get_onboarding_metadata_display(self) -> dict:
        """
        Get onboarding-specific metadata with human-readable values.

        For select-type questions, this resolves UUIDs to their human-readable labels.

        Returns:
            dict: Onboarding metadata with resolved labels, e.g., {'intent': ['Research', 'Commercial']}
        """
        metadata_display = {}

        # Check both checklist types for intent fields
        for checklist_type in [
            checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
            checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA,
        ]:
            completion = self.get_or_create_checklist_completion(checklist_type)
            if not completion:
                continue

            answers = completion.answers.select_related("question").all()

            for answer in answers:
                question = answer.question
                answer_value = answer.answer_data

                try:
                    metadata = OnboardingQuestionMetadata.objects.get(question=question)
                except OnboardingQuestionMetadata.DoesNotExist:
                    continue

                # Only process intent fields (not customer fields)
                if metadata.intent_field and not metadata.maps_to_customer_field:
                    display_value = question.get_answer_display(answer_value)

                    # Format multi-select answers with quotes and proper spacing
                    if isinstance(display_value, list):
                        # Convert list to formatted string: "Option 1", "Option 2", "Option 3"
                        display_value = ", ".join(f'"{item}"' for item in display_value)

                    metadata_display[metadata.intent_field] = display_value

        return metadata_display

    def get_user_submitted_customer_data(self) -> dict:
        """
        Get customer-related data submitted by the user via checklist answers.

        Returns:
            dict: Customer-related data, e.g., {'email': 'john@example.com', 'vat_code': 'EE123456789'}
        """
        extracted = self.extract_data_from_checklist()
        return extracted.get("customer_data", {})

    def can_customer_be_created(self) -> tuple[bool, str | None]:
        """
        Check if a customer can be created from this verification.

        Validation rules:
        - For automatic validation (validation_method is set AND no justification):
          * Only checks INTENT checklist completion
          * Customer checklist is bypassed (data validated by business registry)
        - For manual validation (no validation_method OR has a justification):
          * Checks both CUSTOMER and INTENT checklist completion
          * This includes fallback when automatic validation fails and user submits justification

        Returns:
            tuple: (can_create: bool, error_message: str or None)
                   If can_create is False, error_message explains why
        """
        # Check verification status
        if self.status != enums.VerificationStatus.VERIFIED:
            return (
                False,
                f"Verification status must be 'verified', currently '{self.status}'",
            )

        # Check if customer already exists
        if self.customer is not None:
            return False, "Customer already exists for this verification"

        # Check if there's a justification (indicates manual validation flow)
        has_justification = self.justifications.exists()

        # Determine validation flow:
        # - Automatic validation: validation_method is set AND no justification
        # - Manual validation: no validation_method OR has justification (fallback after automatic validation failed)
        is_automatic_validation = self.validation_method and not has_justification

        if is_automatic_validation:
            # Automatic validation: Only check INTENT checklist
            intent_completion = self.get_or_create_checklist_completion(
                checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
            )
            if intent_completion and not intent_completion.is_completed:
                return (
                    False,
                    "Checklist has required fields that are not completed. "
                    "Please complete all required intent questions before creating a customer.",
                )
        else:
            # Manual validation: Check both CUSTOMER and INTENT checklists
            customer_completion = self.get_or_create_checklist_completion(
                checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA
            )
            intent_completion = self.get_or_create_checklist_completion(
                checklist_enums.ChecklistTypes.ONBOARDING_INTENT_DATA
            )

            if customer_completion and not customer_completion.is_completed:
                return (
                    False,
                    "Customer related checklist has required questions that are not completed. "
                    "Please complete all required customer data questions before creating a customer.",
                )

            if intent_completion and not intent_completion.is_completed:
                return (
                    False,
                    "Intent related checklist has required questions that are not completed. "
                    "Please complete all required intent questions before creating a customer.",
                )

        # Check if customer with same registration code already exists
        if self.legal_person_identifier:
            customer_exists = structure_models.Customer.objects.filter(
                registration_code=self.legal_person_identifier, country=self.country
            ).exists()

            if customer_exists:
                return (
                    False,
                    f"Customer with registration code {self.legal_person_identifier} "
                    f"already exists for country {self.country}",
                )

        return True, None

    def create_customer_if_verified(self):
        """Create customer if verification is successful and no customer exists."""
        can_create, error_message = self.can_customer_be_created()
        if not can_create:
            raise ValueError(f"Cannot create customer: {error_message}")

        # Extract data from checklist
        extracted = self.extract_data_from_checklist()

        # Extract address and postal from verified_company_data if available
        # These should be pre-normalized by the backend (e.g., Estonian backend)
        address_from_api = self.verified_company_data.get("address")
        postal_from_api = self.verified_company_data.get("postal")
        vat_number_from_api = self.verified_company_data.get("vat_number")

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
            "registration_code": self.legal_person_identifier,
        }

        country = self.country or extracted["customer_data"].get("country")
        if country:
            customer_data["country"] = country

        # Add address and postal from API if available
        if address_from_api:
            customer_data["address"] = address_from_api
        if postal_from_api:
            customer_data["postal"] = postal_from_api
        if vat_number_from_api:
            customer_data["vat_code"] = vat_number_from_api

        # Add any additional fields from checklist answers
        for key, value in extracted["customer_data"].items():
            if key not in customer_data and value:  # Don't override existing fields
                customer_data[key] = value

        # Create the customer
        self.customer = structure_models.Customer.objects.create(**customer_data)
        self.save(update_fields=["customer"])

        # Add user as customer owner. force=True: the creator must own the new
        # organization even if that role is concealed for it.
        self.customer.add_user(self.user, RoleEnum.CUSTOMER_OWNER, force=True)

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

    class Meta:
        ordering = ["-created", "id"]

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
        ordering = ["-created", "id"]

    def __str__(self):
        return f"Documentation for {self.justification} - {self.file.name if self.file else 'No file'}"
