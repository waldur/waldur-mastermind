from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _


@deconstructible
class ActionsValidator:
    def __init__(self, available_actions):
        self.available_actions = available_actions

    def __call__(self, value):
        actions = set(value.split(','))
        if actions - {a.__name__ for a in self.available_actions}:
            raise ValidationError(
                _("%(value)s includes unavailable actions."),
                params={"value": value},
            )

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__)
            and self.available_actions == other.available_actions
        )
