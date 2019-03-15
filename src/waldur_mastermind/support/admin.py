from __future__ import unicode_literals

from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import admin as core_admin
from waldur_core.core.admin import JsonWidget
from waldur_core.structure import admin as structure_admin

from . import models, backend
from .backend.basic import BasicBackend

User = get_user_model()


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return '{} - {}'.format(user.full_name, user.username)


class SupportUserAdminForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(SupportUserAdminForm, self).__init__(*args, **kwargs)
        self.fields['user'] = UserChoiceField(queryset=User.objects.all().order_by('full_name'))


class SupportUserAdmin(admin.ModelAdmin):
    form = SupportUserAdminForm


class OfferingAdminForm(forms.ModelForm):
    class Meta:
        widgets = {
            'report': JsonWidget(),
        }


class OfferingAdmin(admin.ModelAdmin):
    list_display = ('template', 'name', 'unit_price', 'unit', 'state',
                    'created', 'modified', 'issue_key')
    search_fields = ('name', 'template__name', 'issue__key')
    fields = ('name', 'unit_price', 'unit', 'template', 'issue',
              'project', 'state', 'product_code', 'article_code', 'report')
    form = OfferingAdminForm

    def issue_key(self, offering):
        return offering.issue and offering.issue.key or 'N/A'


class OfferingTemplateAdminForm(forms.ModelForm):
    class Meta:
        widgets = {
            'config': JsonWidget(),
        }


class OfferingTemplateAdmin(admin.ModelAdmin):
    form = OfferingTemplateAdminForm


class IssueAdmin(core_admin.ExtraActionsObjectMixin, structure_admin.BackendModelAdmin):
    exclude = ('resource_content_type', 'resource_object_id')

    def resolve(self, request, pk=None):
        issue = get_object_or_404(models.Issue, pk=pk)
        issue.set_resolved()
        message = _('Issue has been resolved.')
        self.message_user(request, message)
        return HttpResponseRedirect('../')

    def cancel(self, request, pk=None):
        issue = get_object_or_404(models.Issue, pk=pk)
        issue.set_canceled()
        message = _('Issue has been canceled.')
        self.message_user(request, message)
        return HttpResponseRedirect('../')

    def buttons_validate(request, obj):
        if isinstance(backend.get_active_backend(), BasicBackend) and obj.resolved is None:
            return True

    resolve.validator = buttons_validate
    cancel.validator = buttons_validate

    def get_extra_object_actions(self):
        return [
            self.resolve,
            self.cancel,
        ]


class TemplateAttachmentInline(admin.TabularInline):
    model = models.TemplateAttachment
    fields = ('name', 'file')


class TemplateAdmin(core_admin.ExcludedFieldsAdminMixin,
                    admin.ModelAdmin):
    list_display = ('name', 'issue_type', 'created')
    search_fields = ('name', 'native_name')
    fields = ('name', 'native_name',
              'description', 'native_description',
              'issue_type', 'created', 'modified')
    readonly_fields = ('created', 'modified')
    inlines = [TemplateAttachmentInline]

    @cached_property
    def excluded_fields(self):
        if not settings.WALDUR_CORE['NATIVE_NAME_ENABLED']:
            return ['native_name', 'native_description']
        return []


class RequestTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'issue_type_name', 'backend_id')
    search_fields = ('name',)


class IssueStatusAdmin(admin.ModelAdmin):
    list_display = ('name', 'type')


admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Issue, IssueAdmin)
admin.site.register(models.Comment, structure_admin.BackendModelAdmin)
admin.site.register(models.Attachment)
admin.site.register(models.SupportUser, SupportUserAdmin)
admin.site.register(models.Template, TemplateAdmin)
admin.site.register(models.OfferingTemplate, OfferingTemplateAdmin)
admin.site.register(models.OfferingPlan)
admin.site.register(models.TemplateStatusNotification)
admin.site.register(models.IgnoredIssueStatus)
admin.site.register(models.RequestType, RequestTypeAdmin)
admin.site.register(models.IssueStatus, IssueStatusAdmin)
