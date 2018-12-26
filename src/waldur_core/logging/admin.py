import json

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group
from django.db import transaction
from django.forms import ModelForm
from django.shortcuts import redirect
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _
from waldur_core.core.admin import JsonWidget
import six

from waldur_core.core.admin import ExtraActionsMixin, UpdateOnlyModelAdmin
from waldur_core.core.utils import serialize_instance
from waldur_core.logging import models, tasks
from waldur_core.logging.loggers import get_valid_events, get_event_groups


class JSONMultipleChoiceField(forms.MultipleChoiceField):

    def prepare_value(self, value):
        if isinstance(value, six.string_types):
            return json.loads(value)
        return value


class BaseHookForm(forms.ModelForm):
    event_types = JSONMultipleChoiceField(
        choices=lambda: [(e, e) for e in get_valid_events()],
        widget=forms.SelectMultiple(attrs={'size': '30'}),
        required=False,
    )

    event_groups = JSONMultipleChoiceField(
        choices=lambda: [(g, g) for g in get_event_groups()],
        widget=forms.SelectMultiple(attrs={'size': '30'}),
        required=False,
    )


class SystemNotificationForm(BaseHookForm):
    roles = JSONMultipleChoiceField(
        choices=[(g, g) for g in models.SystemNotification.get_valid_roles()],
        widget=forms.SelectMultiple(attrs={'size': '30'}),
        required=True,
    )

    class Meta:
        model = models.SystemNotification
        exclude = 'uuid',

    def __init__(self, *args, **kwargs):
        super(SystemNotificationForm, self).__init__(*args, **kwargs)
        self.fields['hook_content_type'].queryset = models.BaseHook.get_all_content_types()


class AlertAdminForm(ModelForm):
    class Meta:
        widgets = {
            'context': JsonWidget(),
        }


class SystemNotificationAdmin(admin.ModelAdmin):
    form = SystemNotificationForm
    list_display = ('name', 'hook_content_type', 'roles')


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


class ReportAdmin(UpdateOnlyModelAdmin, ExtraActionsMixin, admin.ModelAdmin):
    list_display = ('created', 'state', 'get_filesize')
    readonly_fields = ('state', 'file', 'get_filesize', 'error_message')
    exclude = ('file_size',)

    def get_filesize(self, obj):
        if obj.file_size:
            return filesizeformat(obj.file_size)
        else:
            return 'N/A'

    get_filesize.short_description = 'File size'

    def get_extra_actions(self):
        return [self.create_report]

    def create_report(self, request):
        with transaction.atomic():
            report = models.Report.objects.create()
            serialized_report = serialize_instance(report)
            transaction.on_commit(lambda: tasks.create_report.delay(serialized_report))
        message = _('Report creation has been scheduled')
        self.message_user(request, message)
        return redirect(reverse('admin:logging_report_changelist'))

    create_report.short_description = _('Create report')


# This hack is needed because core admin is imported several times.
if admin.site.is_registered(Group):
    admin.site.unregister(Group)

admin.site.register(models.Alert, AlertAdmin)
admin.site.register(models.SystemNotification, SystemNotificationAdmin)
admin.site.register(models.WebHook, WebHookAdmin)
admin.site.register(models.EmailHook, EmailHookAdmin)
admin.site.register(models.PushHook, PushHookAdmin)
admin.site.register(models.Report, ReportAdmin)
