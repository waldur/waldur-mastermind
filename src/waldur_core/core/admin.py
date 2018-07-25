from __future__ import unicode_literals

from collections import defaultdict
import copy
import json

from django import forms
from django.conf import settings
from django.conf.urls import url
from django.contrib import admin, messages
from django.contrib.admin import forms as admin_forms
from django.contrib.admin import widgets
from django.contrib.auth import admin as auth_admin, get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from rest_framework import permissions as rf_permissions
from reversion.admin import VersionAdmin
import six

from waldur_core.core import models
from waldur_core.core.authentication import can_access_admin_site


def get_admin_url(obj):
    return reverse('admin:%s_%s_change' % (obj._meta.app_label, obj._meta.model_name), args=[obj.id])


def render_to_readonly(value):
    return "<p>{0}</p>".format(value)


class ReadonlyTextWidget(forms.TextInput):
    def format_value(self, value):
        return value

    def render(self, name, value, attrs=None, renderer=None):
        return render_to_readonly(self.format_value(value))


class PasswordWidget(forms.PasswordInput):
    template_name = 'admin/core/widgets/password-widget.html'

    def __init__(self, attrs=None):
        super(PasswordWidget, self).__init__(attrs, render_value=True)


def format_json_field(value):
    template = '<div><pre style="overflow: hidden">{0}</pre></div>'
    formatted_value = json.dumps(value, indent=True)
    return template.format(formatted_value)


class OptionalChoiceField(forms.ChoiceField):
    def __init__(self, choices=(), *args, **kwargs):
        empty = [('', '---------')]
        choices = empty + sorted(choices, key=lambda pair: pair[1])
        super(OptionalChoiceField, self).__init__(choices, *args, **kwargs)


class UserCreationForm(auth_admin.UserCreationForm):
    class Meta(object):
        model = get_user_model()
        fields = ("username",)

    # overwritten to support custom User model
    def clean_username(self):
        # Since User.username is unique, this check is redundant,
        # but it sets a nicer error message than the ORM. See #13147.
        username = self.cleaned_data["username"]
        try:
            get_user_model()._default_manager.get(username=username)
        except get_user_model().DoesNotExist:
            return username
        raise forms.ValidationError(
            self.error_messages['duplicate_username'],
            code='duplicate_username',
        )


class UserChangeForm(auth_admin.UserChangeForm):
    class Meta(object):
        model = get_user_model()
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super(UserChangeForm, self).__init__(*args, **kwargs)
        competences = [(key, key) for key in settings.WALDUR_CORE.get('USER_COMPETENCE_LIST', [])]
        self.fields['preferred_language'] = OptionalChoiceField(choices=settings.LANGUAGES, required=False)
        self.fields['competence'] = OptionalChoiceField(choices=competences, required=False)

    def clean_civil_number(self):
        # Empty string should be converted to None.
        # Otherwise uniqueness constraint is violated.
        # See also: http://stackoverflow.com/a/1400046/175349
        civil_number = self.cleaned_data.get('civil_number')
        if civil_number:
            return civil_number.strip()
        return None


class ExcludedFieldsAdminMixin(admin.ModelAdmin):
    """
    This mixin allows to toggle display of fields in Django model admin according to custom logic.
    It's expected that inherited class has implemented excluded_fields property.
    """

    @cached_property
    def excluded_fields(self):
        return []

    def filter_excluded_fields(self, fields):
        return [field for field in fields if field not in self.excluded_fields]

    def exclude_fields_from_fieldset(self, fieldset):
        name, options = fieldset
        fields = options.get('fields', ())
        options = copy.copy(options)
        options['fields'] = self.filter_excluded_fields(fields)
        return (name, options)

    def get_fields(self, request, obj=None):
        fields = super(ExcludedFieldsAdminMixin, self).get_fields(request, obj)
        return self.filter_excluded_fields(fields)

    def get_list_display(self, request):
        fields = super(ExcludedFieldsAdminMixin, self).get_list_display(request)
        return self.filter_excluded_fields(fields)

    def get_search_fields(self, request):
        fields = super(ExcludedFieldsAdminMixin, self).get_search_fields(request)
        return self.filter_excluded_fields(fields)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super(ExcludedFieldsAdminMixin, self).get_fieldsets(request, obj)
        return map(self.exclude_fields_from_fieldset, fieldsets)


class NativeNameAdminMixin(ExcludedFieldsAdminMixin):
    @cached_property
    def excluded_fields(self):
        if not settings.WALDUR_CORE['NATIVE_NAME_ENABLED']:
            return ['native_name']
        return []


class UserAdmin(NativeNameAdminMixin, auth_admin.UserAdmin):
    list_display = ('username', 'uuid', 'email', 'full_name', 'native_name', 'is_active', 'is_staff', 'is_support')
    search_fields = ('username', 'uuid', 'full_name', 'native_name', 'email', 'civil_number')
    list_filter = ('is_active', 'is_staff', 'is_support')
    fieldsets = (
        (None, {'fields': ('username', 'password', 'registration_method', 'uuid')}),
        (_('Personal info'), {'fields': (
            'civil_number', 'full_name', 'native_name', 'email',
            'preferred_language', 'competence', 'phone_number'
        )}),
        (_('Organization'), {'fields': ('organization', 'job_title',)}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_support', 'customer_roles', 'project_roles')}),
        (_('Important dates'), {'fields': ('last_login', 'date_joined', 'agreement_date')}),
    )
    readonly_fields = ('registration_method', 'agreement_date', 'customer_roles', 'project_roles', 'uuid',
                       'last_login', 'date_joined')
    form = UserChangeForm
    add_form = UserCreationForm

    def customer_roles(self, instance):
        from waldur_core.structure.models import CustomerPermission
        permissions = CustomerPermission.objects.filter(user=instance, is_active=True).order_by('customer')

        return format_html_join(
            mark_safe('<br/>'),  # nosec
            '<a href={}>{}</a>',
            ((get_admin_url(permission.customer), six.text_type(permission)) for permission in permissions),
        ) or mark_safe("<span class='errors'>%s</span>" % _('User has no roles in any organization.'))  # nosec

    customer_roles.short_description = _('Roles in organizations')

    def project_roles(self, instance):
        from waldur_core.structure.models import ProjectPermission
        permissions = ProjectPermission.objects.filter(user=instance, is_active=True).order_by('project')

        return format_html_join(
            mark_safe('<br/>'),  # nosec
            '<a href={}>{}</a>',
            ((get_admin_url(permission.project), six.text_type(permission)) for permission in permissions),
        ) or mark_safe("<span class='errors'>%s</span>" % _('User has no roles in any project.'))  # nosec

    project_roles.short_description = _('Roles in projects')


class SshPublicKeyAdmin(admin.ModelAdmin):
    list_display = ('user', 'name', 'fingerprint')
    search_fields = ('user__name', 'name', 'fingerprint')
    readonly_fields = ('user', 'name', 'fingerprint', 'public_key')


class CustomAdminAuthenticationForm(admin_forms.AdminAuthenticationForm):
    error_messages = {
        'invalid_login': _("Please enter the correct %(username)s and password "
                           "for a staff or a support account. Note that both fields may be "
                           "case-sensitive."),
    }

    def confirm_login_allowed(self, user):
        if not can_access_admin_site(user):
            return super(CustomAdminAuthenticationForm, self).confirm_login_allowed(user)


class CustomAdminSite(admin.AdminSite):
    site_title = _('Waldur MasterMind admin')
    site_header = _('Waldur MasterMind administration')
    index_title = _('Waldur MasterMind administration')
    login_form = CustomAdminAuthenticationForm

    def has_permission(self, request):
        is_safe = request.method in rf_permissions.SAFE_METHODS
        return can_access_admin_site(request.user) and (is_safe or request.user.is_staff)

    @classmethod
    def clone_default(cls):
        instance = cls()
        instance._registry = admin.site._registry.copy()
        instance._actions = admin.site._actions.copy()
        instance._global_actions = admin.site._global_actions.copy()
        return instance


admin_site = CustomAdminSite.clone_default()
admin.site = admin_site
admin.site.register(models.User, UserAdmin)
admin.site.register(models.SshPublicKey, SshPublicKeyAdmin)

# TODO: Extract common classes to admin_utils module and remove hack.
# This hack is needed because admin is imported several times.
# Please note that admin module should NOT be imported by other apps.
if admin.site.is_registered(Group):
    admin.site.unregister(Group)


class ReversionAdmin(VersionAdmin):
    def add_view(self, request, form_url='', extra_context=None):
        # Revision creation is ignored in this method because it has to be implemented in model.save method
        return super(VersionAdmin, self).add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        # Revision creation is ignored in this method because it has to be implemented in model.save method
        return super(VersionAdmin, self).change_view(request, object_id, form_url, extra_context)


class ExecutorAdminAction(object):
    """ Add executor as action to admin model.

    Usage example:
        class PullSecurityGroups(ExecutorAdminAction):
            executor = executors.TenantPullSecurityGroupsExecutor  # define executor
            short_description = 'Pull security groups'  # description for admin page

            def validate(self, tenant):
                if tenant.state != Tenant.States.OK:
                    raise ValidationError('Tenant has to be in state OK to pull security groups.')

        pull_security_groups = PullSecurityGroups()  # this action could be registered as admin action

    """
    executor = NotImplemented

    def __call__(self, admin_class, request, queryset):
        errors = defaultdict(list)
        successfully_executed = []
        for instance in queryset:
            try:
                self.validate(instance)
            except ValidationError as e:
                errors[six.text_type(e)].append(instance)
            else:
                self.executor.execute(instance)
                successfully_executed.append(instance)

        if successfully_executed:
            message = _('Operation was successfully scheduled for %(count)d instances: %(names)s') % dict(
                count=len(successfully_executed),
                names=', '.join([six.text_type(i) for i in successfully_executed])
            )
            admin_class.message_user(request, message)

        for error, instances in errors.items():
            message = _('Failed to schedule operation for %(count)d instances: %(names)s. Error: %(message)s') % dict(
                count=len(instances),
                names=', '.join([six.text_type(i) for i in instances]),
                message=error,
            )
            admin_class.message_user(request, message, level=messages.ERROR)

    def validate(self, instance):
        """ Raise validation error if action cannot be performed for given instance """
        pass


class ExtraActionsMixin(object):
    """
    Allows to add extra actions to admin list page.
    """
    change_list_template = 'admin/core/change_list.html'

    def get_extra_actions(self):
        raise NotImplementedError('Method "get_extra_actions" should be implemented in ExtraActionsMixin.')

    def get_urls(self):
        """
        Inject extra action URLs.
        """
        urls = []

        for action in self.get_extra_actions():
            regex = r'^{}/$'.format(self._get_action_href(action))
            view = self.admin_site.admin_view(action)
            urls.append(url(regex, view))

        return urls + super(ExtraActionsMixin, self).get_urls()

    def changelist_view(self, request, extra_context=None):
        """
        Inject extra links into template context.
        """
        links = []

        for action in self.get_extra_actions():
            links.append({
                'label': self._get_action_label(action),
                'href': self._get_action_href(action)
            })

        extra_context = extra_context or {}
        extra_context['extra_links'] = links

        return super(ExtraActionsMixin, self).changelist_view(
            request, extra_context=extra_context,
        )

    def _get_action_href(self, action):
        return action.__name__

    def _get_action_label(self, action):
        return getattr(action, 'name', action.__name__.replace('_', ' ').capitalize())


class UpdateOnlyModelAdmin(object):

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class GBtoMBWidget(widgets.AdminIntegerFieldWidget):
    def value_from_datadict(self, data, files, name):
        value = super(GBtoMBWidget, self).value_from_datadict(data, files, name) or 0
        value = int(value) * 1024
        return value

    def format_value(self, value):
        return int(value) / 1024

    def render(self, name, value, attrs=None, renderer=None):
        result = super(GBtoMBWidget, self).render(name, value, attrs)
        return '<label>%s GB</label>' % result
