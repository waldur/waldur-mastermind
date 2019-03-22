import itertools
import json
import logging

from django import forms
from django.conf import settings
from django.conf.urls import url
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.core.exceptions import ValidationError
from django.db import models as django_models
from django.forms import ModelMultipleChoiceField, ModelForm, RadioSelect, ChoiceField, CharField
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ungettext
import six

from waldur_core.core import utils as core_utils
from waldur_core.core.admin import get_admin_url, ExecutorAdminAction, PasswordWidget, NativeNameAdminMixin, JsonWidget
from waldur_core.core.models import User
from waldur_core.core.tasks import send_task
from waldur_core.core.validators import BackendURLValidator
from waldur_core.quotas.admin import QuotaInline
from waldur_core.structure import models, SupportedServices, executors, utils

logger = logging.getLogger(__name__)


class BackendModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = super(BackendModelAdmin, self).get_readonly_fields(request, obj)

        if not obj:
            return fields

        excluded = self.get_exclude(request, obj) or tuple()
        if not settings.WALDUR_CORE['BACKEND_FIELDS_EDITABLE']:
            instance_class = type(obj)
            fields = fields + instance_class.get_backend_fields()
            fields = filter(lambda field: field not in excluded, fields)

        return fields


class FormRequestAdminMixin(object):
    """
    This mixin allows you to get current request user in the model admin form,
    which then passed to add_user method, so that user which granted role,
    is stored in the permission model.
    """

    def get_form(self, request, obj=None, **kwargs):
        form = super(FormRequestAdminMixin, self).get_form(request, obj=obj, **kwargs)
        form.request = request
        return form


class ChangeReadonlyMixin(object):
    add_readonly_fields = ()
    change_readonly_fields = ()

    def get_readonly_fields(self, request, obj=None):
        fields = super(ChangeReadonlyMixin, self).get_readonly_fields(request, obj)
        if hasattr(request, '_is_admin_add_view') and request._is_admin_add_view:
            return tuple(set(fields) | set(self.add_readonly_fields))
        else:
            return tuple(set(fields) | set(self.change_readonly_fields))

    def add_view(self, request, *args, **kwargs):
        request._is_admin_add_view = True
        return super(ChangeReadonlyMixin, self).add_view(request, *args, **kwargs)


class ProtectedModelMixin(object):
    def delete_view(self, request, *args, **kwargs):
        try:
            response = super(ProtectedModelMixin, self).delete_view(request, *args, **kwargs)
        except django_models.ProtectedError as e:
            self.message_user(request, e, messages.ERROR)
            return HttpResponseRedirect('.')
        else:
            return response


class ResourceCounterFormMixin(object):
    def get_vm_count(self, obj):
        return obj.quotas.get(name=obj.Quotas.nc_vm_count).usage

    get_vm_count.short_description = _('VM count')

    def get_app_count(self, obj):
        return obj.quotas.get(name=obj.Quotas.nc_app_count).usage

    get_app_count.short_description = _('Application count')

    def get_private_cloud_count(self, obj):
        return obj.quotas.get(name=obj.Quotas.nc_private_cloud_count).usage

    get_private_cloud_count.short_description = _('Private cloud count')


class CustomerAdminForm(ModelForm):
    owners = ModelMultipleChoiceField(User.objects.all().order_by('full_name'), required=False,
                                      widget=FilteredSelectMultiple(verbose_name=_('Owners'), is_stacked=False))
    support_users = ModelMultipleChoiceField(User.objects.all().order_by('full_name'), required=False,
                                             widget=FilteredSelectMultiple(verbose_name=_('Support users'),
                                                                           is_stacked=False))

    def __init__(self, *args, **kwargs):
        super(CustomerAdminForm, self).__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.owners = self.instance.get_owners()
            self.support_users = self.instance.get_support_users()
            self.fields['owners'].initial = self.owners
            self.fields['support_users'].initial = self.support_users
        else:
            self.owners = User.objects.none()
            self.support_users = User.objects.none()
        self.fields['agreement_number'].initial = models.get_next_agreement_number()

        textarea_attrs = {'cols': '40', 'rows': '4'}
        self.fields['contact_details'].widget.attrs = textarea_attrs
        self.fields['access_subnets'].widget.attrs = textarea_attrs
        type_choices = ['']
        type_choices.extend(settings.WALDUR_CORE['COMPANY_TYPES'])
        self.fields['type'] = ChoiceField(required=False, choices=[(t, t) for t in type_choices])

    def save(self, commit=True):
        customer = super(CustomerAdminForm, self).save(commit=False)

        if not customer.pk:
            customer.save()

        self.populate_users('owners', customer, models.CustomerRole.OWNER)
        self.populate_users('support_users', customer, models.CustomerRole.SUPPORT)

        return customer

    def populate_users(self, field_name, customer, role):
        field = getattr(self, field_name)
        new_users = self.cleaned_data[field_name]

        removed_users = field.exclude(pk__in=new_users)
        for user in removed_users:
            customer.remove_user(user, role, self.request.user)

        added_users = new_users.exclude(pk__in=field)
        for user in added_users:
            # User role within customer must be unique.
            if not customer.has_user(user):
                customer.add_user(user, role, self.request.user)

        self.save_m2m()

    def clean(self):
        cleaned_data = super(CustomerAdminForm, self).clean()
        owners = self.cleaned_data['owners']
        support_users = self.cleaned_data['support_users']
        invalid_users = set(owners) & set(support_users)
        if invalid_users:
            invalid_users_list = ', '.join(map(six.text_type, invalid_users))
            raise ValidationError(_('User role within organization must be unique. '
                                    'Role assignment of The following users is invalid: %s.') % invalid_users_list)
        return cleaned_data

    def clean_accounting_start_date(self):
        accounting_start_date = self.cleaned_data['accounting_start_date']
        if 'accounting_start_date' in self.changed_data and accounting_start_date < timezone.now():
            # If accounting_start_date < timezone.now(), we change accounting_start_date
            # but not raise an exception, because accounting_start_date default value is
            # timezone.now(), but init time of form and submit time of form are always diff.
            # And user will get an exception always if set default value.
            return timezone.now()

        return accounting_start_date


class CustomerAdmin(FormRequestAdminMixin,
                    ResourceCounterFormMixin,
                    NativeNameAdminMixin,
                    ProtectedModelMixin,
                    admin.ModelAdmin):
    form = CustomerAdminForm
    fields = ('name', 'uuid', 'image', 'native_name', 'abbreviation', 'division', 'contact_details',
              'registration_code', 'backend_id',
              'agreement_number', 'email', 'phone_number', 'access_subnets',
              'country', 'vat_code', 'is_company', 'owners', 'support_users',
              'type', 'address', 'postal', 'bank_name', 'bank_account',
              'accounting_start_date', 'default_tax_percent', 'blocked')
    list_display = ('name', 'uuid', 'abbreviation',
                    'created', 'accounting_start_date',
                    'get_vm_count', 'get_app_count', 'get_private_cloud_count')
    list_filter = ('blocked', 'division')
    search_fields = ('name', 'uuid', 'abbreviation')
    readonly_fields = ('uuid',)
    inlines = [QuotaInline]

    def get_readonly_fields(self, request, obj=None):
        fields = super(CustomerAdmin, self).get_readonly_fields(request, obj)
        if obj and obj.is_billable():
            return fields + ('accounting_start_date',)
        return fields


class ProjectAdminForm(ModelForm):
    admins = ModelMultipleChoiceField(User.objects.all().order_by('full_name'), required=False,
                                      widget=FilteredSelectMultiple(verbose_name=_('Admins'), is_stacked=False))
    managers = ModelMultipleChoiceField(User.objects.all().order_by('full_name'), required=False,
                                        widget=FilteredSelectMultiple(verbose_name=_('Managers'), is_stacked=False))
    support_users = ModelMultipleChoiceField(User.objects.all().order_by('full_name'), required=False,
                                             widget=FilteredSelectMultiple(verbose_name=_('Support users'),
                                                                           is_stacked=False))

    def __init__(self, *args, **kwargs):
        super(ProjectAdminForm, self).__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.admins = self.instance.get_users(models.ProjectRole.ADMINISTRATOR)
            self.managers = self.instance.get_users(models.ProjectRole.MANAGER)
            self.support_users = self.instance.get_users(models.ProjectRole.SUPPORT)
            self.fields['admins'].initial = self.admins
            self.fields['managers'].initial = self.managers
            self.fields['support_users'].initial = self.support_users
        else:
            for field_name in ('admins', 'managers', 'support_users'):
                setattr(self, field_name, User.objects.none())

    def clean(self):
        cleaned_data = super(ProjectAdminForm, self).clean()
        admins = self.cleaned_data['admins']
        managers = self.cleaned_data['managers']
        support_users = self.cleaned_data['support_users']
        for xs, ys in itertools.combinations([set(admins), set(managers), set(support_users)], 2):
            invalid_users = xs & ys
            if invalid_users:
                invalid_users_list = ', '.join(map(six.text_type, invalid_users))
                raise ValidationError(_('User role within project must be unique. '
                                        'Role assignment of The following users is invalid: %s.') % invalid_users_list)
        return cleaned_data

    def save(self, commit=True):
        project = super(ProjectAdminForm, self).save(commit=False)

        if not project.pk:
            project.save()

        self.populate_users('admins', project, models.ProjectRole.ADMINISTRATOR)
        self.populate_users('managers', project, models.ProjectRole.MANAGER)
        self.populate_users('support_users', project, models.ProjectRole.SUPPORT)

        return project

    def populate_users(self, field_name, project, role):
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


class ProjectAdmin(FormRequestAdminMixin,
                   ResourceCounterFormMixin,
                   ProtectedModelMixin,
                   ChangeReadonlyMixin,
                   admin.ModelAdmin):
    form = ProjectAdminForm

    fields = ('name', 'description', 'customer', 'type',
              'admins', 'managers', 'support_users', 'certifications')

    list_display = ['name', 'uuid', 'customer', 'created', 'get_type_name',
                    'get_vm_count', 'get_app_count', 'get_private_cloud_count']
    search_fields = ['name', 'uuid']
    change_readonly_fields = ['customer']
    inlines = [QuotaInline]
    filter_horizontal = ('certifications',)
    actions = ('cleanup',)

    class Cleanup(ExecutorAdminAction):
        executor = executors.ProjectCleanupExecutor
        short_description = _('Delete projects with all resources')

    cleanup = Cleanup()

    def get_type_name(self, project):
        return project.type and project.type.name or ''

    get_type_name.short_description = _('Type')
    get_type_name.admin_order_field = 'type__name'


class ServiceCertificationAdmin(admin.ModelAdmin):
    list_display = ('name', 'link')
    search_fields = ['name', 'link']
    list_filter = ('service_settings',)


class ServiceSettingsAdminForm(ModelForm):
    backend_url = CharField(max_length=200, required=False, validators=[BackendURLValidator()])

    def clean(self):
        cleaned_data = super(ServiceSettingsAdminForm, self).clean()
        service_type = cleaned_data.get('type')
        if not service_type:
            return

        field_info = utils.get_all_services_field_info()
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
                    logger.warning('Incorrect field %s in %s required_fields' %
                                   (field, service_type))

        # Check required extra fields of service type
        try:
            if 'options' in cleaned_data:
                options = json.loads(cleaned_data.get('options'))
                unfilled = set(extra_fields_required[service_type]) - set(options.keys()) - set(fields_default.keys())

                if unfilled:
                    self.add_error('options', _('This field must include keys: %s') %
                                   ', '.join(unfilled))
        except ValueError:
            self.add_error('options', _('JSON is not valid'))

    class Meta:
        widgets = {
            'options': JsonWidget(),
            'geolocations': JsonWidget(),
            'username': forms.TextInput(attrs={'autocomplete': 'new-password'}),
            'password': PasswordWidget(attrs={'autocomplete': 'new-password'}),
            'token': PasswordWidget(attrs={'autocomplete': 'new-password'}),
        }

    def __init__(self, *args, **kwargs):
        super(ServiceSettingsAdminForm, self).__init__(*args, **kwargs)
        self.fields['type'] = ChoiceField(choices=SupportedServices.get_choices(),
                                          widget=RadioSelect)


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
    list_display = ('name', 'customer', 'get_type_display', 'state', 'error_message', 'uuid')
    list_filter = (ServiceTypeFilter, 'state')
    search_fields = ('name', 'uuid')
    change_readonly_fields = ('customer',)
    actions = ['pull']
    form = ServiceSettingsAdminForm
    fields = ('type', 'name', 'uuid', 'backend_url', 'username', 'password',
              'token', 'domain', 'certificate', 'options', 'customer',
              'state', 'error_message', 'tags', 'homepage', 'terms_of_services',
              'certifications', 'geolocations')
    inlines = [QuotaInline]
    filter_horizontal = ('certifications',)
    common_fields = ('type', 'name', 'uuid', 'options', 'customer',
                     'state', 'error_message', 'tags', 'homepage', 'terms_of_services',
                     'certifications', 'geolocations')

    # must be specified explicitly not to be constructed from model name by default.
    change_form_template = 'admin/structure/servicesettings/change_form.html'

    def get_type_display(self, obj):
        return obj.get_type_display()

    get_type_display.short_description = 'Type'

    def add_view(self, *args, **kwargs):
        self.exclude = getattr(self, 'add_exclude', ())
        return super(PrivateServiceSettingsAdmin, self).add_view(*args, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        field_info = utils.get_all_services_field_info()
        service_field_names = field_info.fields
        service_fields_required = field_info.fields_required

        for service_name in service_field_names:
            service_field_names[service_name].extend(self.common_fields)

        extra_context['service_fields'] = json.dumps(service_field_names)
        extra_context['service_fields_required'] = json.dumps(service_fields_required)
        return super(PrivateServiceSettingsAdmin, self).changeform_view(request, object_id, form_url, extra_context)

    def get_readonly_fields(self, request, obj=None):
        fields = super(PrivateServiceSettingsAdmin, self).get_readonly_fields(request, obj)
        if not obj:
            return fields + ('state',)
        elif obj.scope:
            return fields + ('options',)
        return fields

    def get_urls(self):
        my_urls = [
            url(r'^(.+)/change/services/$', self.admin_site.admin_view(self.services)),
        ]
        return my_urls + super(PrivateServiceSettingsAdmin, self).get_urls()

    def services(self, request, pk=None):
        settings = models.ServiceSettings.objects.get(id=pk)
        projects = {}

        spl_model = SupportedServices.get_related_models(settings)['service_project_link']
        for spl in spl_model.objects.filter(service__settings=settings):
            projects.setdefault(spl.project.id, {
                'name': six.text_type(spl.project),
                'url': get_admin_url(spl.project),
                'services': [],
            })
            projects[spl.project.id]['services'].append({
                'name': six.text_type(spl.service),
                'url': get_admin_url(spl.service),
            })

        return render(request, 'structure/service_settings_entities.html', {'projects': projects.values()})

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
        fields = super(SharedServiceSettingsAdmin, self).get_fields(request, obj)
        return [field for field in fields if field != 'customer']

    def get_list_display(self, request):
        fields = super(SharedServiceSettingsAdmin, self).get_list_display(request)
        return [field for field in fields if field != 'customer']

    def save_form(self, request, form, change):
        obj = super(SharedServiceSettingsAdmin, self).save_form(request, form, change)

        """If required field is not filled, but it has got a default value, we set a default value."""
        field_info = utils.get_all_services_field_info()
        extra_fields_default = field_info.extra_fields_default[obj.type]
        extra_fields_required = field_info.extra_fields_required[obj.type]
        default = (set(extra_fields_required) - set(obj.options.keys())) & set(extra_fields_default.keys())

        if default:
            for d in default:
                obj.options[d] = extra_fields_default[d]

        if not change:
            obj.shared = True
        return obj

    class ConnectShared(ExecutorAdminAction):
        executor = executors.ServiceSettingsConnectSharedExecutor
        short_description = _('Create SPLs and services for shared service settings')

        def validate(self, service_settings):
            if not service_settings.shared:
                raise ValidationError(_('It is impossible to connect not shared settings.'))

    connect_shared = ConnectShared()


class ServiceAdmin(admin.ModelAdmin):
    list_display = ('settings', 'customer')
    ordering = ('customer',)


class ServiceProjectLinkAdmin(admin.ModelAdmin):
    list_display = ('get_service_name', 'get_customer_name', 'get_project_name')
    list_filter = ('service__settings', 'project__name', 'service__settings__name')
    ordering = ('service__customer__name', 'project__name')
    list_display_links = ('get_service_name',)
    search_fields = ('service__customer__name', 'project__name', 'service__settings__name')
    inlines = [QuotaInline]

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ('service', 'project')
        return ()

    def get_queryset(self, request):
        queryset = super(ServiceProjectLinkAdmin, self).get_queryset(request)
        return queryset.select_related('service', 'project', 'project__customer')

    def get_service_name(self, obj):
        return obj.service.settings.name

    get_service_name.short_description = _('Service')

    def get_project_name(self, obj):
        return obj.project.name

    get_project_name.short_description = _('Project')

    def get_customer_name(self, obj):
        return obj.service.customer.name

    get_customer_name.short_description = _('Customer')


class DerivedFromSharedSettingsResourceFilter(SimpleListFilter):
    title = _('service settings')
    parameter_name = 'shared__exact'

    def lookups(self, request, model_admin):
        return ((1, _('Shared')), (0, _('Private')))

    def queryset(self, request, queryset):
        if self.value() is not None:
            return queryset.filter(service_project_link__service__settings__shared=self.value())
        else:
            return queryset


class ResourceAdmin(BackendModelAdmin):
    readonly_fields = ('error_message',)
    list_display = ('uuid', 'name', 'backend_id', 'state', 'created',
                    'get_service', 'get_project', 'error_message', 'get_settings_shared')
    list_filter = ('state', DerivedFromSharedSettingsResourceFilter)

    def get_settings_shared(self, obj):
        return obj.service_project_link.service.settings.shared

    get_settings_shared.short_description = _('Are service settings shared')

    def get_service(self, obj):
        return obj.service_project_link.service

    get_service.short_description = _('Service')
    get_service.admin_order_field = 'service_project_link__service__settings__name'

    def get_project(self, obj):
        return obj.service_project_link.project

    get_project.short_description = _('Project')
    get_project.admin_order_field = 'service_project_link__project__name'


class PublishableResourceAdmin(ResourceAdmin):
    list_display = ResourceAdmin.list_display + ('publishing_state',)


class VirtualMachineAdmin(ResourceAdmin):
    readonly_fields = ResourceAdmin.readonly_fields + ('image_name',)

    actions = ['detect_coordinates']

    def detect_coordinates(self, request, queryset):
        send_task('structure', 'detect_vm_coordinates_batch')([core_utils.serialize_instance(vm) for vm in queryset])

        tasks_scheduled = queryset.count()
        message = ungettext(
            'Coordinates detection has been scheduled for one virtual machine.',
            'Coordinates detection has been scheduled for %(tasks_scheduled)d virtual machines.',
            tasks_scheduled
        )
        message = message % {'tasks_scheduled': tasks_scheduled}

        self.message_user(request, message)

    detect_coordinates.short_description = _('Detect coordinates of virtual machines')


class DivisionTypeAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ['name']


class DivisionAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'parent')
    search_fields = ['name']
    list_filter = ('type',)


admin.site.register(models.ServiceCertification, ServiceCertificationAdmin)
admin.site.register(models.Customer, CustomerAdmin)
admin.site.register(models.ProjectType, admin.ModelAdmin)
admin.site.register(models.Project, ProjectAdmin)
admin.site.register(models.PrivateServiceSettings, PrivateServiceSettingsAdmin)
admin.site.register(models.SharedServiceSettings, SharedServiceSettingsAdmin)
admin.site.register(models.DivisionType, DivisionTypeAdmin)
admin.site.register(models.Division, DivisionAdmin)
