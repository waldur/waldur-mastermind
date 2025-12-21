from random import randint

from django.conf import settings
from django.db import models
from django_fsm import FSMField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models

from . import enums


def _default_message():
    return f"{randint(0, 9999):4.0f}".replace(" ", "0")  # noqa: S311


class AuthResult(
    core_models.UuidMixin, core_models.ErrorMessageMixin, TimeStampedModel
):
    user = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="auth_valimo_results",
        null=True,
    )
    phone = models.CharField(max_length=30)
    message = models.CharField(
        max_length=4,
        default=_default_message,
        help_text="This message will be shown to user.",
    )
    state = FSMField(
        choices=enums.AuthResultStates.choices, default=enums.AuthResultStates.SCHEDULED
    )
    details = models.CharField(
        max_length=255, blank=True, help_text="Cancellation details."
    )
    backend_transaction_id = models.CharField(max_length=100, blank=True)

    @transition(
        field=state,
        source=enums.AuthResultStates.SCHEDULED,
        target=enums.AuthResultStates.PROCESSING,
    )
    def begin_processing(self):
        pass

    @transition(
        field=state,
        source=enums.AuthResultStates.PROCESSING,
        target=enums.AuthResultStates.OK,
    )
    def set_ok(self):
        pass

    @transition(
        field=state,
        source=enums.AuthResultStates.PROCESSING,
        target=enums.AuthResultStates.CANCELED,
    )
    def set_canceled(self):
        pass

    @transition(field=state, source="*", target=enums.AuthResultStates.ERRED)
    def set_erred(self):
        pass
