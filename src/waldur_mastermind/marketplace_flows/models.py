from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core.mixins import ReviewMixin as CoreReviewMixin
from waldur_core.core.mixins import ReviewStateMixin
from waldur_core.core.models import UuidMixin
from waldur_core.structure.models import (
    CUSTOMER_DETAILS_FIELDS,
    Customer,
    CustomerDetailsMixin,
    Project,
    ProjectDetailsMixin,
    ProjectRole,
)
from waldur_mastermind.marketplace.models import (
    Offering,
    Order,
    OrderItem,
    ResourceDetailsMixin,
)
from waldur_mastermind.support import models as support_models

User = get_user_model()


class ReviewMixin(CoreReviewMixin):
    class Meta:
        abstract = True

    def approve(self, user, comment=None):
        super().approve(user, comment)
        self.flow.approve()

    def reject(self, user, comment=None):
        super().reject(user, comment)
        self.flow.reject()


class CustomerCreateRequest(ReviewMixin, CustomerDetailsMixin):
    def approve(self, user, comment=None):
        super().approve(user, comment)
        self.flow.project_create_request.approve(user, comment)

    def reject(self, user, comment=None):
        super().reject(user, comment)
        self.flow.project_create_request.reject(user, comment)


class ProjectCreateRequest(ReviewMixin, ProjectDetailsMixin):
    class Permissions:
        customer_path = 'flow__customer'


class ResourceCreateRequest(ReviewMixin, ResourceDetailsMixin):
    class Permissions:
        customer_path = 'offering__customer'


class FlowTracker(ReviewStateMixin, TimeStampedModel, UuidMixin):
    """
    This model allows to track consecutive creation of customer, project and resource.
    Customer field is filled either initially or when customer creation request is fulfilled.
    Order item is created when service provider approves resource creation request.
    """

    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')

    customer_create_request = models.OneToOneField(
        CustomerCreateRequest,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='flow',
    )
    project_create_request = models.OneToOneField(
        ProjectCreateRequest,
        on_delete=models.CASCADE,
        related_name='flow',
    )
    resource_create_request = models.OneToOneField(
        ResourceCreateRequest,
        on_delete=models.CASCADE,
        related_name='flow',
    )

    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.CASCADE, related_name='+'
    )
    order_item = models.ForeignKey(
        OrderItem, null=True, blank=True, on_delete=models.CASCADE, related_name='+'
    )
    tracker = FieldTracker()

    class Meta:
        ordering = ['-created']

    @transaction.atomic
    def submit(self):
        super(FlowTracker, self).submit()
        if self.customer_create_request:
            self.customer_create_request.submit()
        self.project_create_request.submit()
        self.resource_create_request.submit()

    @transaction.atomic
    def cancel(self):
        super(FlowTracker, self).cancel()
        if self.customer_create_request:
            self.customer_create_request.cancel()
        self.project_create_request.cancel()
        self.resource_create_request.cancel()

    def reject(self):
        self.state = self.States.REJECTED
        self.save(update_fields=['state'])

    def approve(self):
        requests = (
            'customer_create_request',
            'project_create_request',
            'resource_create_request',
        )
        if all(
            not getattr(self, request)
            or getattr(self, request).state == ReviewStateMixin.States.APPROVED
            for request in requests
        ):
            if self.customer_create_request:
                self.customer = Customer.objects.create(
                    **{
                        k: getattr(self.customer_create_request, k)
                        for k in CUSTOMER_DETAILS_FIELDS
                    }
                )
            project = Project.objects.create(
                customer=self.customer,
                name=self.project_create_request.name,
                description=self.project_create_request.description,
                end_date=self.project_create_request.end_date,
            )
            project.add_user(self.requested_by, ProjectRole.MANAGER)
            order = Order.objects.create(
                project=project,
                created_by=self.requested_by,
                state=Order.States.EXECUTING,
            )
            self.order_item = OrderItem.objects.create(
                order=order,
                offering=self.resource_create_request.offering,
                plan=self.resource_create_request.plan,
                attributes=self.resource_create_request.attributes,
                limits=self.resource_create_request.limits,
                state=OrderItem.States.EXECUTING,
            )
            self.order_item.init_cost()
            self.order_item.save()
            self.state = self.States.APPROVED
            self.save(update_fields=['customer', 'order_item', 'state'])


class OfferingStateRequest(CoreReviewMixin, UuidMixin):
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    offering = models.ForeignKey(
        on_delete=models.CASCADE, to=Offering, null=True, blank=True, related_name='+'
    )
    issue = models.OneToOneField(
        on_delete=models.SET_NULL,
        to=support_models.Issue,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created']

    @transaction.atomic
    def approve(self, user=None, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = self.States.APPROVED
        self.save(
            update_fields=['reviewed_by', 'reviewed_at', 'review_comment', 'state']
        )
        self.offering.activate()
        self.offering.save(update_fields=['state'])

    @transaction.atomic
    def reject(self, user=None, comment=None):
        self.reviewed_by = user
        self.review_comment = comment
        self.reviewed_at = timezone.now()
        self.state = self.States.REJECTED
        self.save(
            update_fields=['reviewed_by', 'reviewed_at', 'review_comment', 'state']
        )

    @classmethod
    def get_url_name(cls):
        return 'marketplace-offering-activate-request'
