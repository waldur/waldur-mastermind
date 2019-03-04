from __future__ import unicode_literals

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


logger = logging.getLogger(__name__)

User = get_user_model()


@python_2_unicode_compatible
class Invitation(core_models.UuidMixin, TimeStampedModel, core_models.ErrorMessageMixin):
    class Permissions(object):
        customer_path = 'customer'

    class State(object):
        ACCEPTED = 'accepted'
        CANCELED = 'canceled'
        PENDING = 'pending'
        EXPIRED = 'expired'

        CHOICES = ((ACCEPTED, 'Accepted'), (CANCELED, 'Canceled'), (PENDING, 'Pending'), (EXPIRED, 'Expired'))

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='+', blank=True, null=True)

    customer = models.ForeignKey(structure_models.Customer, verbose_name=_('organization'), related_name='invitations')
    customer_role = structure_models.CustomerRole(verbose_name=_('organization role'), null=True, blank=True)

    project = models.ForeignKey(structure_models.Project, related_name='invitations', blank=True, null=True)
    project_role = structure_models.ProjectRole(null=True, blank=True)

    state = models.CharField(max_length=8, choices=State.CHOICES, default=State.PENDING)
    link_template = models.CharField(max_length=255, help_text=_('The template must include {uuid} parameter '
                                                                 'e.g. http://example.com/invitation/{uuid}'))
    email = models.EmailField(help_text=_('Invitation link will be sent to this email. Note that user can accept '
                                          'invitation with different email.'))
    civil_number = models.CharField(
        max_length=50, blank=True,
        help_text=_('Civil number of invited user. If civil number is not defined any user can accept invitation.'))

    def get_expiration_time(self):
        return self.created + settings.WALDUR_CORE['INVITATION_LIFETIME']

    @transaction.atomic
    def accept(self, user, replace_email=False):
        if self.project_role is not None:
            self.project.add_user(user, self.project_role, self.created_by)
        else:
            self.customer.add_user(user, self.customer_role, self.created_by)

        self.state = self.State.ACCEPTED
        self.save(update_fields=['state'])
        if replace_email and user.email != self.email:
            # Ensure that user wouldn't reuse existing email
            if User.objects.filter(email=self.email).exists():
                logger.debug('Another user with the same email already exists. '
                             'Invitation ID: %', self.id)
                return
            else:
                user.email = self.email
                user.save(update_fields=['email'])

    def cancel(self):
        self.state = self.State.CANCELED
        self.save(update_fields=['state'])

    def __str__(self):
        return self.email
