from django.conf import settings
from django.db import models, transaction
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.signals import permissions_request_approved


class BaseInvitation(core_models.UuidMixin, TimeStampedModel):
    class Meta:
        abstract = True

    created_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name='+',
        blank=True,
        null=True,
    )

    customer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.Customer,
        verbose_name=_('organization'),
    )
    customer_role = structure_models.CustomerRole(
        verbose_name=_('organization role'), null=True, blank=True
    )

    project = models.ForeignKey(
        on_delete=models.CASCADE, to=structure_models.Project, blank=True, null=True,
    )
    project_role = structure_models.ProjectRole(null=True, blank=True)


class GroupInvitation(BaseInvitation):
    is_active = models.BooleanField(default=True)

    class Permissions:
        customer_path = 'customer'

    def get_expiration_time(self):
        return self.created + settings.WALDUR_CORE['GROUP_INVITATION_LIFETIME']

    def cancel(self):
        self.is_active = False
        self.save(update_fields=['is_active'])

    def __str__(self):
        if self.customer_role:
            return '%s %s' % (self.customer, self.customer_role)

        return '%s %s' % (self.project, self.project_role)


class Invitation(
    BaseInvitation, core_models.ErrorMessageMixin, core_models.UserDetailsMixin,
):
    class Permissions:
        customer_path = 'customer'

    class State:
        REQUESTED = 'requested'
        REJECTED = 'rejected'
        PENDING = 'pending'
        ACCEPTED = 'accepted'
        CANCELED = 'canceled'
        EXPIRED = 'expired'

        CHOICES = (
            (REQUESTED, 'Requested'),
            (REJECTED, 'Rejected'),
            (PENDING, 'Pending'),
            (ACCEPTED, 'Accepted'),
            (CANCELED, 'Canceled'),
            (EXPIRED, 'Expired'),
        )

    approved_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name='+',
        blank=True,
        null=True,
    )

    state = models.CharField(
        max_length=10, choices=State.CHOICES, default=State.PENDING
    )
    email = models.EmailField(
        help_text=_(
            'Invitation link will be sent to this email. Note that user can accept '
            'invitation with different email.'
        )
    )
    civil_number = models.CharField(
        max_length=50,
        blank=True,
        help_text=_(
            'Civil number of invited user. If civil number is not defined any user can accept invitation.'
        ),
    )
    tax_number = models.CharField(_('tax number'), max_length=50, blank=True)
    full_name = models.CharField(_('full name'), max_length=100, blank=True)

    def get_expiration_time(self):
        return self.created + settings.WALDUR_CORE['INVITATION_LIFETIME']

    def accept(self, user):
        if self.project_role is not None:
            self.project.add_user(user, self.project_role, self.created_by)
        else:
            self.customer.add_user(user, self.customer_role, self.created_by)

        self.state = self.State.ACCEPTED
        self.save(update_fields=['state'])

    def cancel(self):
        self.state = self.State.CANCELED
        self.save(update_fields=['state'])

    def approve(self, user):
        self.state = self.State.PENDING
        self.approved_by = user
        self.save(update_fields=['state', 'approved_by'])

    def reject(self):
        self.state = self.State.REJECTED
        self.save(update_fields=['state'])

    def __str__(self):
        return self.email


def filter_own_requests(user):
    return PermissionRequest.objects.filter(created_by=user)


class PermissionRequest(core_mixins.ReviewMixin, core_models.UuidMixin):
    class Permissions:
        customer_path = 'invitation__customer'
        build_query = filter_own_requests

    invitation = models.ForeignKey(on_delete=models.PROTECT, to=GroupInvitation)

    created_by = models.ForeignKey(
        on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL, related_name='+',
    )

    @transaction.atomic
    def approve(self, user, comment=None):
        super().approve(user, comment)

        if self.invitation.project_role:
            permission, created = self.invitation.project.add_user(
                self.created_by, self.invitation.project_role
            )
            structure = self.invitation.project
        elif self.invitation.customer_role:
            permission, created = self.invitation.customer.add_user(
                self.created_by, self.invitation.customer_role
            )
            structure = self.invitation.customer

        permissions_request_approved.send(
            sender=self.__class__, permission=permission, structure=structure,
        )
