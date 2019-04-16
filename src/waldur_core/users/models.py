from __future__ import unicode_literals

from django.conf import settings
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


@python_2_unicode_compatible
class Invitation(core_models.UuidMixin,
                 TimeStampedModel,
                 core_models.ErrorMessageMixin,
                 core_models.UserDetailsMixin):
    class Permissions(object):
        customer_path = 'customer'

    class State(object):
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

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', blank=True, null=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', blank=True, null=True)

    customer = models.ForeignKey(structure_models.Customer, verbose_name=_('organization'), related_name='invitations')
    customer_role = structure_models.CustomerRole(verbose_name=_('organization role'), null=True, blank=True)

    project = models.ForeignKey(structure_models.Project, related_name='invitations', blank=True, null=True)
    project_role = structure_models.ProjectRole(null=True, blank=True)

    state = models.CharField(max_length=10, choices=State.CHOICES, default=State.PENDING)
    link_template = models.CharField(max_length=255, help_text=_('The template must include {uuid} parameter '
                                                                 'e.g. http://example.com/invitation/{uuid}'))
    email = models.EmailField(help_text=_('Invitation link will be sent to this email. Note that user can accept '
                                          'invitation with different email.'))
    civil_number = models.CharField(
        max_length=50, blank=True,
        help_text=_('Civil number of invited user. If civil number is not defined any user can accept invitation.'))
    tax_number = models.CharField(_('tax number'), max_length=50, blank=True)

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
