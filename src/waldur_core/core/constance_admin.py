from constance import admin as constance_admin
from constance.forms import ConstanceForm
from django import forms
from django.conf import settings
from django.contrib import admin


class WaldurConstanceForm(ConstanceForm):
    """Inject per-setting choices from ``CONSTANCE_CONFIG_CHOICES``.

    Upstream :class:`ConstanceForm` builds each field from
    ``CONSTANCE_ADDITIONAL_FIELDS`` only, so ``ChoiceField`` is created with
    no ``choices`` and ``multiple_choice_field`` is rendered as a plain text
    input (it maps to a ``CharField`` subclass). This subclass patches both
    cases so the admin dropdowns render and validate the configured options.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices_map = getattr(settings, "CONSTANCE_CONFIG_CHOICES", {}) or {}
        config = getattr(settings, "CONSTANCE_CONFIG", {}) or {}

        for name, choices in choices_map.items():
            field = self.fields.get(name)
            if field is None:
                continue

            choices_list = list(choices)
            field_type = config.get(name, (None, None, None))
            field_type = field_type[2] if len(field_type) > 2 else None

            if field_type == "multiple_choice_field" or isinstance(
                field, forms.MultipleChoiceField
            ):
                self.fields[name] = forms.MultipleChoiceField(
                    label=name,
                    choices=choices_list,
                    required=False,
                )
            elif isinstance(field, forms.ChoiceField):
                field.choices = choices_list


class WaldurConstanceAdmin(constance_admin.ConstanceAdmin):
    change_list_form = WaldurConstanceForm


def register():
    """Replace constance's default admin with the Waldur-aware version."""
    if admin.site.is_registered(constance_admin.Config):
        admin.site.unregister([constance_admin.Config])
    admin.site.register([constance_admin.Config], WaldurConstanceAdmin)
