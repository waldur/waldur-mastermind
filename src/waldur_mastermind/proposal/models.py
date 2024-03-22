import logging
from datetime import timedelta

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import SafeAttributesMixin

from . import managers

logger = logging.getLogger(__name__)


class CallDocument(
    TimeStampedModel,
    core_models.UuidMixin,
):
    call = models.ForeignKey("Call", on_delete=models.CASCADE)
    file = models.FileField(
        upload_to="call_documents",
        blank=True,
        null=True,
        help_text="Documentation for call for proposals.",
    )


class CallManagingOrganisation(
    core_models.UuidMixin,
    core_models.DescribableMixin,
    structure_models.ImageModelMixin,
    structure_models.StructureModel,
    TimeStampedModel,
):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)

    class Permissions:
        customer_path = "customer"

    class Meta:
        verbose_name = _("Call managing organisation")

    def __str__(self):
        return str(self.customer)

    @classmethod
    def get_url_name(cls):
        return "call-managing-organisation"


class Call(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.StructureModel,
    structure_models.StructureLoggableMixin,
    core_models.BackendMixin,
):
    class States:
        DRAFT = "draft"
        ACTIVE = "active"
        ARCHIVED = "archived"

        CHOICES = (
            (DRAFT, "Draft"),
            (ACTIVE, "Active"),
            (ARCHIVED, "Archived"),
        )

    manager = models.ForeignKey(CallManagingOrganisation, on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    state = models.CharField(
        default=States.DRAFT, choices=States.CHOICES, db_index=True
    )
    offerings = models.ManyToManyField(
        marketplace_models.Offering, through="RequestedOffering"
    )
    documents = models.ManyToManyField(CallDocument, related_name="call_documents")
    objects = managers.CallManager()

    class Permissions:
        customer_path = "manager__customer"

    def __str__(self):
        return f"{self.name} | {self.manager.customer}"

    @property
    def reviewers(self):
        return get_users(self, RoleEnum.CALL_REVIEWER)


class RequestedOffering(
    SafeAttributesMixin,
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    class States:
        REQUESTED = "requested"
        ACCEPTED = "accepted"
        CANCELED = "canceled"

        CHOICES = (
            (REQUESTED, "Requested"),
            (ACCEPTED, "Accepted"),
            (CANCELED, "Canceled"),
        )

    class Permissions:
        customer_path = "offering__customer"

    approved_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
        blank=True,
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    state = models.CharField(
        default=States.REQUESTED, choices=States.CHOICES, db_index=True
    )
    call = models.ForeignKey(Call, on_delete=models.CASCADE)
    plan = models.ForeignKey(
        on_delete=models.CASCADE, to=marketplace_models.Plan, null=True, blank=True
    )


class Round(
    TimeStampedModel,
    core_models.UuidMixin,
):
    class ReviewStrategies:
        AFTER_ROUND = "after_round"
        AFTER_PROPOSAL = "after_proposal"

        CHOICES = (
            (AFTER_ROUND, "After round is closed"),
            (AFTER_PROPOSAL, "After proposal submission"),
        )

    class AllocationStrategies:
        BY_CALL_MANAGER = "by_call_manager"
        AUTOMATIC = "automatic"

        CHOICES = (
            (BY_CALL_MANAGER, "By call manager"),
            (AUTOMATIC, "Automatic based on review scoring"),
        )

    class AllocationTimes:
        ON_DECISION = "on_decision"
        FIXED_DATE = "fixed_date"

        CHOICES = (
            (ON_DECISION, "On decision"),
            (FIXED_DATE, "Fixed date"),
        )

    review_strategy = models.CharField(
        default=ReviewStrategies.AFTER_ROUND,
        choices=ReviewStrategies.CHOICES,
        db_index=True,
    )
    deciding_entity = models.CharField(
        default=AllocationStrategies.AUTOMATIC,
        choices=AllocationStrategies.CHOICES,
        db_index=True,
    )
    allocation_time = models.CharField(
        default=AllocationTimes.ON_DECISION,
        choices=AllocationTimes.CHOICES,
        db_index=True,
    )
    review_duration_in_days = models.PositiveIntegerField(null=True, blank=True)
    minimum_number_of_reviewers = models.PositiveIntegerField(null=True, blank=True)
    minimal_average_scoring = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    allocation_date = models.DateTimeField(null=True, blank=True)
    start_time = models.DateTimeField()
    cutoff_time = models.DateTimeField()
    call = models.ForeignKey(Call, on_delete=models.PROTECT)

    class Permissions:
        customer_path = "call__manager__customer"

    def __str__(self):
        return f"{self.call.name} | {self.start_time} - {self.cutoff_time}"


class ProposalDocumentation(
    TimeStampedModel,
    core_models.UuidMixin,
):
    proposal = models.ForeignKey("Proposal", on_delete=models.CASCADE)
    file = models.FileField(
        upload_to="proposal_project_supporting_documentation",
        blank=True,
        null=True,
        help_text="Upload supporting documentation in PDF format.",
    )


class Proposal(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.StructureLoggableMixin,
    structure_models.ProjectOECDFOS2007CodeMixin,
):
    class States:
        DRAFT = "draft"
        SUBMITTED = "submitted"
        IN_REVIEW = "in_review"
        IN_REVISION = "in_revision"
        ACCEPTED = "accepted"
        REJECTED = "rejected"
        CANCELED = "canceled"

        CHOICES = (
            (DRAFT, "Draft"),
            (SUBMITTED, "Submitted"),
            (IN_REVIEW, "In review"),
            (IN_REVISION, "In revision"),
            (ACCEPTED, "Accepted"),
            (REJECTED, "Rejected"),
            (CANCELED, "Canceled"),
        )

    round = models.ForeignKey(Round, on_delete=models.CASCADE)
    state = models.CharField(
        default=States.DRAFT, choices=States.CHOICES, db_index=True
    )
    project = models.ForeignKey(
        structure_models.Project, on_delete=models.PROTECT, null=True, editable=False
    )
    duration_in_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in days after provisioning of resources.",
    )
    approved_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
        blank=True,
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    project_summary = models.TextField(blank=True)
    project_duration = models.PositiveIntegerField(null=True, blank=True)
    project_is_confidential = models.BooleanField(default=False)
    project_has_civilian_purpose = models.BooleanField(default=False)

    resources = models.ManyToManyField(RequestedOffering, through="RequestedResource")

    tracker = FieldTracker()

    class Permissions:
        customer_path = "round__call__manager__customer"

    def __str__(self):
        return f"{self.name} | {self.round.start_time} - {self.round.cutoff_time} | {self.round.call}"

    @classmethod
    def get_url_name(cls):
        return "proposal-proposal"


class RequestedResource(
    core_models.UuidMixin,
    TimeStampedModel,
    core_models.DescribableMixin,
):
    class Permissions:
        project_path = "proposal__project"

    requested_offering = models.ForeignKey(
        RequestedOffering,
        related_name="+",
        on_delete=models.PROTECT,
    )
    attributes = models.JSONField(blank=True, default=dict)
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    resource = models.ForeignKey(
        marketplace_models.Resource,
        related_name="+",
        on_delete=models.SET_NULL,
        null=True,
    )
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE)


class Review(
    TimeStampedModel,
    core_models.UuidMixin,
):
    class States:
        CREATED = "created"
        IN_REVIEW = "in_review"
        SUBMITTED = "submitted"
        REJECTED = "rejected"

        CHOICES = (
            (CREATED, "Created"),
            (IN_REVIEW, "In review"),
            (SUBMITTED, "Submitted"),
            (REJECTED, "Rejected"),
        )

    proposal = models.ForeignKey(Proposal, on_delete=models.PROTECT)
    state = models.CharField(
        default=States.CREATED, choices=States.CHOICES, db_index=True
    )
    summary_score = models.PositiveSmallIntegerField(blank=True, default=0)
    summary_public_comment = models.TextField(blank=True)
    summary_private_comment = models.TextField(blank=True)
    comment_project_title = models.CharField(max_length=255, null=True, blank=True)
    comment_project_summary = models.CharField(max_length=255, null=True, blank=True)
    comment_project_description = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_duration = models.CharField(max_length=255, null=True, blank=True)
    comment_project_is_confidential = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_has_civilian_purpose = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_project_supporting_documentation = models.CharField(
        max_length=255, null=True, blank=True
    )
    comment_resource_requests = models.CharField(max_length=255, null=True, blank=True)
    comment_team = models.CharField(max_length=255, null=True, blank=True)

    reviewer = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )

    tracker = FieldTracker()

    @classmethod
    def get_url_name(cls):
        return "proposal-review"

    @property
    def review_end_date(self):
        if not self.proposal.round.review_duration_in_days:
            return

        return self.created + timedelta(
            days=self.proposal.round.review_duration_in_days
        )


class ReviewComment(
    TimeStampedModel,
    core_models.UuidMixin,
):
    review = models.ForeignKey(Review, on_delete=models.CASCADE)
    message = models.CharField(max_length=255)


class ResourceAllocator(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
):
    call = models.ForeignKey(Call, on_delete=models.CASCADE)
    project = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
