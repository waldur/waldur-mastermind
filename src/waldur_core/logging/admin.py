import json

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group
from django.forms import ModelForm
from jsoneditor.forms import JSONEditor
import six

from waldur_core.logging import models
from waldur_core.logging.loggers import get_valid_events, get_event_groups


class JSONMultipleChoiceField(forms.MultipleChoiceField):

    def prepare_value(self, value):
        if isinstance(value, six.string_types):
            return json.loads(value)
        return value


class BaseHookForm(forms.ModelForm):
    event_types = JSONMultipleChoiceField(
        choices=[(e, e) for e in get_valid_events()],
        widget=forms.SelectMultiple(attrs={'size': '30'}),
        required=False,
    )

    event_groups = JSONMultipleChoiceField(
        choices=[(g, g) for g in get_event_groups()],
        widget=forms.SelectMultiple(attrs={'size': '30'}),
        required=False,
    )


class SystemNotificationForm(BaseHookForm):

    class Meta:
        model = models.SystemNotification
        exclude = 'uuid',

    def __init__(self, *args, **kwargs):
        super(SystemNotificationForm, self).__init__(*args, **kwargs)
        self.fields['hook_content_type'].queryset = models.BaseHook.get_all_content_types()


class AlertAdminForm(ModelForm):
    class Meta:
        widgets = {
            'context': JSONEditor(),
        }


class SystemNotificationAdmin(admin.ModelAdmin):
    form = SystemNotificationForm
    list_display = 'hook_content_type',


class AlertAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'alert_type', 'created', 'closed', 'scope', 'severity')
    list_filter = ('alert_type', 'created', 'closed', 'severity')
    ordering = ('alert_type',)
    base_model = models.Alert
    form = AlertAdminForm


class BaseHookAdmin(admin.ModelAdmin):
    form = BaseHookForm
    list_display = ('uuid', 'user', 'is_active', 'event_types', 'event_groups')


class WebHookAdmin(BaseHookAdmin):
    list_display = BaseHookAdmin.list_display + ('destination_url',)


class EmailHookAdmin(BaseHookAdmin):
    list_display = BaseHookAdmin.list_display + ('email',)


class PushHookAdmin(BaseHookAdmin):
    list_display = BaseHookAdmin.list_display + ('type', 'device_id')


# This hack is needed because core admin is imported several times.
if admin.site.is_registered(Group):
    admin.site.unregister(Group)

admin.site.register(models.Alert, AlertAdmin)
admin.site.register(models.SystemNotification, SystemNotificationAdmin)
admin.site.register(models.WebHook, WebHookAdmin)
admin.site.register(models.EmailHook, EmailHookAdmin)
admin.site.register(models.PushHook, PushHookAdmin)
