import logging

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import SafeAttributesMixin

logger = logging.getLogger(__name__)


class CallManagingOrganisation(
    core_models.UuidMixin,
    core_models.DescribableMixin,
    structure_models.ImageModelMixin,
    structure_models.StructureModel,
    TimeStampedModel,
):
    customer = models.OneToOneField(structure_models.Customer, on_delete=models.CASCADE)

    class Permissions:
        customer_path = 'customer'

    class Meta:
        verbose_name = _('Call managing organisation')

    def __str__(self):
        return str(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'call-managing-organisation'


class Call(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    class RoundStrategies:
        ONE_TIME = 1
        REGULAR = 2

        CHOICES = (
            (ONE_TIME, 'One-time'),
            (REGULAR, 'Regular'),
        )

    class ReviewStrategies:
        AFTER_ROUND = 1
        AFTER_APPLICATION = 2

        CHOICES = (
            (AFTER_ROUND, 'After round is closed'),
            (AFTER_APPLICATION, 'After application submission'),
        )

    class AllocationStrategies:
        BY_CALL_MANAGER = 1
        AUTOMATIC = 2

        CHOICES = (
            (BY_CALL_MANAGER, 'By call manager'),
            (AUTOMATIC, 'Automatic based on review scoring'),
        )

    class States:
        DRAFT = 1
        ACTIVE = 2
        ARCHIVED = 3

        CHOICES = (
            (DRAFT, 'Draft'),
            (ACTIVE, 'Active'),
            (ARCHIVED, 'Archived'),
        )

    manager = models.ForeignKey(CallManagingOrganisation, on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        core_models.User, on_delete=models.PROTECT, null=True
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    round_strategy = FSMIntegerField(
        default=RoundStrategies.REGULAR,
        choices=RoundStrategies.CHOICES,
    )
    review_strategy = FSMIntegerField(
        default=ReviewStrategies.AFTER_ROUND, choices=ReviewStrategies.CHOICES
    )
    allocation_strategy = FSMIntegerField(
        default=AllocationStrategies.AUTOMATIC, choices=AllocationStrategies.CHOICES
    )
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    offerings = models.ManyToManyField(
        marketplace_models.Offering, through='RequestedOffering'
    )

    class Permissions:
        customer_path = 'manager__customer'


class RequestedOffering(SafeAttributesMixin, core_models.UuidMixin, TimeStampedModel):
    class States:
        REQUESTED = 1
        ACCEPTED = 2
        CANCELED = 3

        CHOICES = (
            (REQUESTED, 'Requested'),
            (ACCEPTED, 'Accepted'),
            (CANCELED, 'Canceled'),
        )

    approved_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name='+',
        blank=True,
    )
    created_by = models.ForeignKey(
        core_models.User,
        on_delete=models.PROTECT,
        null=True,
        related_name='+',
    )
    state = FSMIntegerField(default=States.REQUESTED, choices=States.CHOICES)
    call = models.ForeignKey(Call, on_delete=models.CASCADE)


class Round(
    TimeStampedModel,
    core_models.UuidMixin,
):
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    call = models.ForeignKey(Call, on_delete=models.PROTECT)


class Proposal(
    TimeStampedModel,
    core_models.UuidMixin,
    core_models.NameMixin,
):
    class States:
        DRAFT = 1
        ACTIVE = 2
        CANCELLED = 3

        CHOICES = (
            (DRAFT, 'Draft'),
            (ACTIVE, 'Active'),
            (CANCELLED, 'Cancelled'),
        )

    round = models.ForeignKey(Round, on_delete=models.PROTECT)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    project = models.ForeignKey(structure_models.Project, on_delete=models.PROTECT)
    duration_requested = models.DateTimeField()
    resource_usage = models.JSONField()


class Review(
    TimeStampedModel,
    core_models.UuidMixin,
):
    class States:
        DRAFT = 1
        ACTIVE = 2
        CANCELLED = 3

        CHOICES = (
            (DRAFT, 'Draft'),
            (ACTIVE, 'Active'),
            (CANCELLED, 'Cancelled'),
        )

    proposal = models.ForeignKey(Proposal, on_delete=models.PROTECT)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    points = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=255, blank=True)
    version = models.CharField(max_length=255, blank=True)


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
