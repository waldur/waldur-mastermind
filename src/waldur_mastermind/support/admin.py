from __future__ import unicode_literals

from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from jsoneditor.forms import JSONEditor

from waldur_core.structure import admin as structure_admin

from . import models


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
            'report': JSONEditor(),
        }


class OfferingAdmin(admin.ModelAdmin):
    list_display = ('type', 'name', 'unit_price', 'unit', 'state')
    fields = ('name', 'unit_price', 'unit', 'type', 'issue',
              'project', 'state', 'product_code', 'article_code', 'report')
    form = OfferingAdminForm


class IssueAdmin(structure_admin.BackendModelAdmin):
    exclude = ('resource_content_type', 'resource_object_id')


admin.site.register(models.Offering, OfferingAdmin)
admin.site.register(models.Issue, IssueAdmin)
admin.site.register(models.Comment, structure_admin.BackendModelAdmin)
admin.site.register(models.Attachment)
admin.site.register(models.SupportUser, SupportUserAdmin)
