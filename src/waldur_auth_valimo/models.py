from __future__ import unicode_literals

from random import randint

from django.conf import settings
from django.db import models
from django_fsm import FSMField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models


def _default_message():
    return '{0:4.0f}'.format(randint(0, 9999)).replace(' ', '0')  # nosec


class AuthResult(core_models.UuidMixin, core_models.ErrorMessageMixin, TimeStampedModel):
    class States:
        SCHEDULED = 'Scheduled'
        PROCESSING = 'Processing'
        OK = 'OK'
        CANCELED = 'Canceled'
        ERRED = 'Erred'

        CHOICES = ((SCHEDULED, SCHEDULED), (PROCESSING, PROCESSING), (OK, OK), (CANCELED, CANCELED), (ERRED, ERRED))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='auth_valimo_results', null=True)
    phone = models.CharField(max_length=30)
    message = models.CharField(max_length=4, default=_default_message, help_text='This message will be shown to user.')
    state = FSMField(choices=States.CHOICES, default=States.SCHEDULED)
    details = models.CharField(max_length=255, blank=True, help_text='Cancellation details.')
    backend_transaction_id = models.CharField(max_length=100, blank=True)

    # for consistency with other models with state
    @property
    def human_readable_state(self):
        return self.state

    @transition(field=state, source=States.SCHEDULED, target=States.PROCESSING)
    def begin_processing(self):
        pass

    @transition(field=state, source=States.PROCESSING, target=States.OK)
    def set_ok(self):
        pass

    @transition(field=state, source=States.PROCESSING, target=States.CANCELED)
    def set_canceled(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def set_erred(self):
        pass
