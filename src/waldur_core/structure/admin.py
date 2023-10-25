import collections
import itertools
import json
import logging
from functools import lru_cache

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models as django_models
from django.db import transaction
from django.forms import CharField, ChoiceField, ModelForm, ModelMultipleChoiceField
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ungettext
from reversion.admin import VersionAdmin

from waldur_core.core import utils as core_utils
from waldur_core.core.admin import (
    ExecutorAdminAction,
    ExtraActionsMixin,
    JsonWidget,
    NativeNameAdminMixin,
    PasswordWidget,
    ReadOnlyAdminMixin,
)
from waldur_core.core.admin_filters import RelatedOnlyDropdownFilter
from waldur_core.core.models import Notification, NotificationTemplate, User
from waldur_core.core.utils import get_fake_context
from waldur_core.core.validators import BackendURLValidator
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure import executors, models
from waldur_core.structure.registry import SupportedServices, get_service_type
from waldur_core.structure.serializers import (
    ServiceOptionsSerializer,
    get_options_serializer_class,
)
from waldur_geo_ip import tasks as geo_ip_tasks

from .widgets import ScrolledSelectMultiple

logger = logging.getLogger(__name__)


FieldInfo = collections.namedtuple(
    'FieldInfo', 'fields fields_required extra_fields_required extra_fields_default'
)


@lru_cache(maxsize=1)
def get_all_services_field_info():
    services_fields = dict()
    services_fields_default_value = dict()
    services_fields_required = dict()
    services_extra_fields_required = dict()

    for serializer_class in ServiceOptionsSerializer.get_subclasses():
        serializer = serializer_class(context=get_fake_context())
        service_type = get_service_type(serializer_class)
        if not service_type:
            continue
        serializer_fields = serializer.get_fields().items()
        services_fields[service_type] = [
            name for name, field in serializer_fields if not field.source
        ]
        services_fields_required[service_type] = [
            name
            for name, field in serializer_fields
            if field.required and not field.source
        ]
        services_extra_fields_required[service_type] = [
            name for name, field in serializer_fields if field.required and field.source
        ]
        services_fields_default_value[service_type] = {
            name: field.default
            for name, field in serializer_fields
            if field.required and field.source
        }

    return FieldInfo(
        fields=services_fields,
        fields_required=services_fields_required,
        extra_fields_required=services_extra_fields_required,
        extra_fields_default=services_fields_default_value,
    )


class BackendModelAdmin(admin.ModelAdmin):
    def get_list_filter(self, request):
        try:
            self.model._meta.get_field('settings')
            return ('settings__shared', ('settings', RelatedOnlyDropdownFilter))
        except FieldDoesNotExist:
            return self.list_filter

    def lookup_allowed(self, lookup, value):
        if lookup == 'settings__shared__exact':
            return True
        return super().lookup_allowed(lookup, value)

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)

        if not obj:
            return fields

        excluded = self.get_exclude(request, obj) or tuple()
        if not settings.WALDUR_CORE['BACKEND_FIELDS_EDITABLE']:
            instance_class = type(obj)
            fields = fields + instance_class.get_backend_fields()
            fields = list(filter(lambda field: field not in excluded, fields))

        return fields


class FormRequestAdminMixin:
    """
    This mixin allows you to get current request user in the model admin form,
    which then passed to add_user method, so that user which granted role,
    is stored in the permission model.
    """

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        form.request = request
        return form


class ChangeReadonlyMixin:
    add_readonly_fields = ()
    change_readonly_fields = ()

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if hasattr(request, '_is_admin_add_view') and request._is_admin_add_view:
            return tuple(set(fields) | set(self.add_readonly_fields))
        else:
            return tuple(set(fields) | set(self.change_readonly_fields))

    def add_view(self, request, *args, **kwargs):
        request._is_admin_add_view = True
        return super().add_view(request, *args, **kwargs)


class ProtectedModelMixin:
    def delete_view(self, request, *args, **kwargs):
        try:
            response = super().delete_view(request, *args, **kwargs)
        except django_models.ProtectedError as e:
            self.message_user(request, e, messages.ERROR)
            return HttpResponseRedirect('.')
        else:
            return response


class CustomerAdminForm(ModelForm):
    owners = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Owners')),
    )
    support_users = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Support users')),
    )
    service_managers = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Service managers')),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.owners = self.instance.get_owners()
            self.support_users = self.instance.get_support_users()
            self.service_managers = self.instance.get_service_managers()
            self.fields['owners'].initial = self.owners
            self.fields['support_users'].initial = self.support_users
            self.fields['service_managers'].initial = self.service_managers
        else:
            self.owners = User.objects.none()
            self.support_users = User.objects.none()
            self.service_managers = User.objects.none()

        textarea_attrs = {'cols': '40', 'rows': '4'}
        self.fields['contact_details'].widget.attrs = textarea_attrs
        self.fields['access_subnets'].widget.attrs = textarea_attrs

    def save(self, commit=True):
        customer = super().save(commit=False)

        if not customer.pk:
            customer.save()

        self.populate_users('owners', customer, RoleEnum.CUSTOMER_OWNER)
        self.populate_users('support_users', customer, RoleEnum.CUSTOMER_SUPPORT)
        self.populate_users('service_managers', customer, RoleEnum.CUSTOMER_MANAGER)

        return customer

    def populate_users(self, field_name, customer: models.Customer, role):
        field = getattr(self, field_name)
        new_users = self.cleaned_data[field_name]

        removed_users = field.exclude(pk__in=new_users)
        for user in removed_users:
            customer.remove_user(user, role, self.request.user)

        added_users = new_users.exclude(pk__in=field)
        for user in added_users:
            if not customer.has_user(user, role):
                customer.add_user(user, role, self.request.user)

        self.save_m2m()

    def clean(self):
        cleaned_data = super().clean()
        owners = self.cleaned_data['owners']
        support_users = self.cleaned_data['support_users']
        invalid_users = set(owners) & set(support_users)
        if invalid_users:
            invalid_users_list = ', '.join(map(str, invalid_users))
            raise ValidationError(
                _(
                    'User cannot be owner and support at the same time. '
                    'Role assignment of The following users is invalid: %s.'
                )
                % invalid_users_list
            )
        return cleaned_data

    def clean_accounting_start_date(self):
        accounting_start_date = self.cleaned_data['accounting_start_date']
        if (
            'accounting_start_date' in self.changed_data
            and accounting_start_date < timezone.now()
        ):
            # If accounting_start_date < timezone.now(), we change accounting_start_date
            # but not raise an exception, because accounting_start_date default value is
            # timezone.now(), but init time of form and submit time of form are always diff.
            # And user will get an exception always if set default value.
            return timezone.now()

        return accounting_start_date


class CustomerAdmin(
    VersionAdmin,
    FormRequestAdminMixin,
    NativeNameAdminMixin,
    ProtectedModelMixin,
    admin.ModelAdmin,
):
    form = CustomerAdminForm
    fields = (
        'name',
        'uuid',
        'image',
        'native_name',
        'abbreviation',
        'division',
        'contact_details',
        'registration_code',
        'backend_id',
        'domain',
        'agreement_number',
        'sponsor_number',
        'email',
        'phone_number',
        'access_subnets',
        'homepage',
        'country',
        'vat_code',
        'owners',
        'support_users',
        'service_managers',
        'address',
        'postal',
        'latitude',
        'longitude',
        'bank_name',
        'bank_account',
        'accounting_start_date',
        'default_tax_percent',
        'blocked',
        'archived',
        'inet',
    )
    list_display = (
        'name',
        'uuid',
        'abbreviation',
        'created',
        'accounting_start_date',
    )
    list_filter = ('blocked', 'archived', 'division')
    search_fields = ('name', 'uuid', 'abbreviation')
    date_hierarchy = 'created'
    readonly_fields = ('uuid',)
    inlines = []

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and obj.is_billable():
            return fields + ('accounting_start_date',)
        return fields

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        models.Project.objects.filter(customer__in=queryset, is_removed=True).delete()
        queryset.delete()


class ProjectAdminForm(ModelForm):
    admins = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Admins')),
    )
    managers = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Managers')),
    )
    members = ModelMultipleChoiceField(
        User.objects.all().order_by('first_name', 'last_name'),
        required=False,
        widget=ScrolledSelectMultiple(verbose_name=_('Members')),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.admins = self.instance.get_users(models.ProjectRole.ADMINISTRATOR)
            self.managers = self.instance.get_users(models.ProjectRole.MANAGER)
            self.members = self.instance.get_users(models.ProjectRole.MEMBER)
            self.fields['admins'].initial = self.admins
            self.fields['managers'].initial = self.managers
            self.fields['members'].initial = self.members
        else:
            for field_name in ('admins', 'managers', 'members'):
                setattr(self, field_name, User.objects.none())

    def clean(self):
        cleaned_data = super().clean()
        admins = self.cleaned_data['admins']
        managers = self.cleaned_data['managers']
        members = self.cleaned_data['members']
        for xs, ys in itertools.combinations(
            [set(admins), set(managers), set(members)], 2
        ):
            invalid_users = xs & ys
            if invalid_users:
                invalid_users_list = ', '.join(map(str, invalid_users))
                raise ValidationError(
                    _(
                        'User role within project must be unique. '
                        'Role assignment of the following users is invalid: %s.'
                    )
                    % invalid_users_list
                )
        return cleaned_data

    def save(self, commit=True):
        project = super().save(commit=False)

        if not project.pk:
            project.save()

        self.populate_users('admins', project, models.ProjectRole.ADMINISTRATOR)
        self.populate_users('managers', project, models.ProjectRole.MANAGER)
        self.populate_users('members', project, models.ProjectRole.MEMBER)

        return project

    def populate_users(self, field_name, project: models.Project, role):
        field = getattr(self, field_name)
        new_users = self.cleaned_data[field_name]

        removed_users = field.exclude(pk__in=new_users)
        for user in removed_users:
            project.remove_user(user, role, self.request.user)

        added_users = new_users.exclude(pk__in=field)
        for user in added_users:
            # User role within project must be unique.
            if not project.has_user(user):
                project.add_user(user, role, self.request.user)
        self.save_m2m()


class ProjectAdmin(
    ExtraActionsMixin,
    FormRequestAdminMixin,
    ProtectedModelMixin,
    ChangeReadonlyMixin,
    admin.ModelAdmin,
):
    form = ProjectAdminForm

    fields = (
        'name',
        'description',
        'customer',
        'type',
        'admins',
        'managers',
        'members',
        'oecd_fos_2007_code',
        'image',
    )

    list_display = [
        'name',
        'uuid',
        'customer',
        'created',
        'get_type_name',
    ]
    list_filter = ['customer']
    search_fields = ['name', 'uuid']
    change_readonly_fields = ['customer']
    actions = ('cleanup', 'sync_remote')

    class Cleanup(ExecutorAdminAction):
        executor = executors.ProjectCleanupExecutor
        short_description = _('Delete projects with all resources')

    cleanup = Cleanup()

    def sync_remote(self, request, queryset):
        from waldur_mastermind.marketplace_remote.tasks import sync_remote_project

        sync_remote_project.delay(
            [core_utils.serialize_instance(project) for project in queryset]
        )
        tasks_scheduled = queryset.count()
        message = ungettext(
            'Remote project synchronization has been scheduled for one project .',
            'Remote project synchronization has been scheduled for %(tasks_scheduled)d projects.',
            tasks_scheduled,
        )
        message = message % {'tasks_scheduled': tasks_scheduled}

        self.message_user(request, message)

    sync_remote.short_description = _('Sync project in remote offerings')

    def get_type_name(self, project):
        return project.type and project.type.name or ''

    get_type_name.short_description = _('Type')
    get_type_name.admin_order_field = 'type__name'

    def get_extra_actions(self):
        return [
            self.clean_remote_projects,
        ]

    def clean_remote_projects(self, request):
        from waldur_mastermind.marketplace_remote import (
            tasks as marketplace_remote_tasks,
        )

        marketplace_remote_tasks.clean_remote_projects.delay()
        self.message_user(request, _('Cleaning up remote projects has been scheduled.'))
        return redirect(reverse('admin:structure_project_changelist'))

    def get_queryset(self, request):
        return models.Project.available_objects.all()


class ServiceSettingsAdminForm(ModelForm):
    backend_url = CharField(
        max_length=200, required=False, validators=[BackendURLValidator()]
    )

    def clean(self):
        cleaned_data = super().clean()
        service_type = cleaned_data.get('type')
        if not service_type:
            return

        field_info = get_all_services_field_info()
        fields_required = field_info.fields_required
        extra_fields_required = field_info.extra_fields_required
        fields_default = field_info.extra_fields_default[service_type]

        # Check required fields of service type
        for field in fields_required[service_type]:
            value = cleaned_data.get(field)
            if not value:
                try:
                    self.add_error(field, _('This field is required.'))
                except ValueError:
                    logger.warning(
                        'Incorrect field %s in %s required_fields'
                        % (field, service_type)
                    )

        # Check required extra fields of service type
        try:
            if 'options' in cleaned_data:
                options = {
                    'backend_url': cleaned_data.get('backend_url'),
                    'username': cleaned_data.get('username'),
                    'password': cleaned_data.get('password'),
                    'domain': cleaned_data.get('domain'),
                    'token': cleaned_data.get('token'),
                    **json.loads(cleaned_data.get('options')),
                }
                unfilled = (
                    set(extra_fields_required[service_type])
                    - set(options.keys())
                    - set(fields_default.keys())
                )

                if unfilled:
                    self.add_error(
                        'options',
                        _('This field must include keys: %s') % ', '.join(unfilled),
                    )
                options_serializer_class = get_options_serializer_class(service_type)
                if options_serializer_class:
                    options_serializer = options_serializer_class(data=options)
                    if not options_serializer.is_valid():
                        self.add_error('options', json.dumps(options_serializer.errors))
                    else:
                        if 'options' in options_serializer.validated_data:
                            cleaned_data['options'] = json.dumps(
                                options_serializer.validated_data['options']
                            )
        except ValueError:
            self.add_error('options', _('JSON is not valid'))

        return cleaned_data

    class Meta:
        widgets = {
            'options': JsonWidget(),
            'geolocations': JsonWidget(),
            'username': forms.TextInput(attrs={'autocomplete': 'new-password'}),
            'password': PasswordWidget(attrs={'autocomplete': 'new-password'}),
            'token': PasswordWidget(attrs={'autocomplete': 'new-password'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['type'] = ChoiceField(choices=SupportedServices.get_choices())


class ServiceTypeFilter(SimpleListFilter):
    title = 'type'
    parameter_name = 'type'

    def lookups(self, request, model_admin):
        return SupportedServices.get_choices()

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(type=self.value())
        else:
            return queryset


class PrivateServiceSettingsAdmin(ChangeReadonlyMixin, admin.ModelAdmin):
    readonly_fields = ('error_message', 'uuid')
    list_display = (
        'name',
        'customer',
        'is_active',
        'get_type_display',
        'state',
        'error_message',
        'uuid',
    )
    list_filter = (ServiceTypeFilter, 'state')
    search_fields = ('name', 'uuid')
    change_readonly_fields = ('customer',)
    actions = ['pull']
    form = ServiceSettingsAdminForm
    fields = (
        'type',
        'name',
        'is_active',
        'uuid',
        'backend_url',
        'username',
        'password',
        'token',
        'domain',
        'certificate',
        'options',
        'customer',
        'state',
        'error_message',
        'terms_of_services',
    )
    common_fields = (
        'type',
        'name',
        'is_active',
        'uuid',
        'options',
        'customer',
        'state',
        'error_message',
        'terms_of_services',
    )

    # must be specified explicitly not to be constructed from model name by default.
    change_form_template = 'admin/structure/servicesettings/change_form.html'

    def get_type_display(self, obj):
        return obj.get_type_display()

    get_type_display.short_description = 'Type'

    def add_view(self, *args, **kwargs):
        self.exclude = getattr(self, 'add_exclude', ())
        return super().add_view(*args, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        field_info = get_all_services_field_info()
        service_field_names = field_info.fields
        service_fields_required = field_info.fields_required

        for service_name in service_field_names:
            service_field_names[service_name].extend(self.common_fields)

        extra_context['service_fields'] = json.dumps(service_field_names)
        extra_context['service_fields_required'] = json.dumps(service_fields_required)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if not obj:
            return fields + ('state',)
        elif obj.scope:
            return fields + ('options',)
        return fields

    class Pull(ExecutorAdminAction):
        executor = executors.ServiceSettingsPullExecutor
        short_description = _('Pull')

        def validate(self, service_settings):
            States = models.ServiceSettings.States
            if service_settings.state not in (States.OK, States.ERRED):
                raise ValidationError(_('Service settings has to be OK or erred.'))

    pull = Pull()

    def save_model(self, request, obj, form, change):
        obj.save()
        if not change:
            executors.ServiceSettingsCreateExecutor.execute(obj)


class SharedServiceSettingsAdmin(PrivateServiceSettingsAdmin):
    actions = ['pull', 'connect_shared']

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        return [field for field in fields if field != 'customer']

    def get_list_display(self, request):
        fields = super().get_list_display(request)
        return [field for field in fields if field != 'customer']

    def save_form(self, request, form, change):
        obj = super().save_form(request, form, change)

        """If required field is not filled, but it has got a default value, we set a default value."""
        field_info = get_all_services_field_info()
        extra_fields_default = field_info.extra_fields_default[obj.type]
        extra_fields_required = field_info.extra_fields_required[obj.type]
        default = (set(extra_fields_required) - set(obj.options.keys())) & set(
            extra_fields_default.keys()
        )

        if default:
            for d in default:
                obj.options[d] = extra_fields_default[d]

        if not change:
            obj.shared = True
        return obj


class ServicePropertyAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    search_fields = ('name',)
    list_filter = (('settings', RelatedOnlyDropdownFilter),)
    readonly_fields = ('name', 'settings')
    list_display = ('name', 'settings')


class DerivedFromSharedSettingsResourceFilter(SimpleListFilter):
    title = _('service settings')
    parameter_name = 'shared__exact'

    def lookups(self, request, model_admin):
        return ((1, _('Shared')), (0, _('Private')))

    def queryset(self, request, queryset):
        if self.value() is not None:
            return queryset.filter(service_settings__shared=self.value())
        else:
            return queryset


class ResourceAdmin(BackendModelAdmin):
    readonly_fields = ('error_message',)
    list_display = (
        'uuid',
        'name',
        'backend_id',
        'state',
        'created',
        'service_settings',
        'project',
        'error_message',
        'get_settings_shared',
    )
    list_filter = BackendModelAdmin.list_filter + (
        'state',
        'project',
        'project__customer',
        DerivedFromSharedSettingsResourceFilter,
    )
    search_fields = ('name',)

    def get_settings_shared(self, obj):
        return obj.service_settings.shared

    get_settings_shared.short_description = _('Are service settings shared')


class PublishableResourceAdmin(ResourceAdmin):
    list_display = ResourceAdmin.list_display + ('publishing_state',)


class VirtualMachineAdmin(ResourceAdmin):
    readonly_fields = ResourceAdmin.readonly_fields + ('image_name',)

    actions = ['detect_coordinates']

    def detect_coordinates(self, request, queryset):
        geo_ip_tasks.detect_vm_coordinates_batch.delay(
            [core_utils.serialize_instance(vm) for vm in queryset]
        )
        tasks_scheduled = queryset.count()
        message = ungettext(
            'Coordinates detection has been scheduled for one virtual machine.',
            'Coordinates detection has been scheduled for %(tasks_scheduled)d virtual machines.',
            tasks_scheduled,
        )
        message = message % {'tasks_scheduled': tasks_scheduled}

        self.message_user(request, message)

    detect_coordinates.short_description = _('Detect coordinates of virtual machines')


class DivisionTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'uuid')
    search_fields = ['name']


class DivisionAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'parent', 'uuid')
    search_fields = ['name']
    list_filter = ('type',)


class UserAgreementAdmin(admin.ModelAdmin):
    fields = ('content', 'agreement_type', 'created', 'modified')
    readonly_fields = ('created', 'modified')
    search_fields = ['content']
    list_filter = ('agreement_type',)


class TemplateInline(admin.TabularInline):
    model = Notification.templates.through


class NotificationAdmin(admin.ModelAdmin):
    list_display = ('key', 'description', 'enabled', 'created', 'modified')
    readonly_fields = ('created', 'modified')
    search_fields = ('key',)
    inlines = [
        TemplateInline,
    ]
    exclude = ('templates',)


class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ('path', 'name')
    search_fields = ('path', 'name')
    inlines = [
        TemplateInline,
    ]


admin.site.register(models.Customer, CustomerAdmin)
admin.site.register(models.ProjectType, admin.ModelAdmin)
admin.site.register(models.Project, ProjectAdmin)
admin.site.register(models.PrivateServiceSettings, PrivateServiceSettingsAdmin)
admin.site.register(models.SharedServiceSettings, SharedServiceSettingsAdmin)
admin.site.register(models.DivisionType, DivisionTypeAdmin)
admin.site.register(models.Division, DivisionAdmin)
admin.site.register(models.UserAgreement, UserAgreementAdmin)
admin.site.register(NotificationTemplate, NotificationTemplateAdmin)
admin.site.register(Notification, NotificationAdmin)
