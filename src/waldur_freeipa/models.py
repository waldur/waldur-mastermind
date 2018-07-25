from __future__ import unicode_literals

import re

from django.conf import settings
from django.core import exceptions, validators
from django.db import models
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import python_2_unicode_compatible
from model_utils import FieldTracker

from waldur_core.core import models as core_models


def validate_username(value):
    if value in settings.WALDUR_FREEIPA['BLACKLISTED_USERNAMES']:
        raise exceptions.ValidationError(
            _('%(value)s is not valid FreeIPA username.'),
            params={'value': value},
        )


@python_2_unicode_compatible
class Profile(core_models.UuidMixin, models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL)
    username = models.CharField(
        _('username'), max_length=32, unique=True,
        help_text=_('Letters, numbers and ./+/-/_ characters'),
        validators=[
            validate_username,
            validators.RegexValidator(re.compile('^[a-zA-Z0-9_.][a-zA-Z0-9_.-]*[a-zA-Z0-9_.$-]?$'),
                                      _('Enter a valid username.'), 'invalid')
        ])
    agreement_date = models.DateTimeField(_('agreement date'), default=timezone.now,
                                          help_text=_('Indicates when the user has agreed with the policy.'))
    is_active = models.BooleanField(_('active'), default=True)
    tracker = FieldTracker()

    @property
    def gecos(self):
        param = []
        for field in ['full_name', 'email', 'phone_number']:
            value = getattr(self.user, field, None)
            if value:
                param.append(value)

        return ','.join(param)

    def __str__(self):
        return self.username
