import uuid

import django_filters
from django import forms
from django.conf import settings as django_settings
from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Concat
from django.utils import timezone
from django_filters.widgets import BooleanWidget
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core import models as core_models
from waldur_core.core.filters import ExternalFilterBackend
from waldur_core.core.utils import get_ordering, is_uuid_like, order_with_nulls
from waldur_core.structure import models
from waldur_core.structure.managers import (
    filter_queryset_by_user_ip,
    filter_queryset_for_user,
)
from waldur_core.structure.registry import SupportedServices
from waldur_mastermind.billing import models as billing_models

User = auth.get_user_model()


class NameFilterSet(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    name_exact = django_filters.CharFilter(field_name='name', lookup_expr='exact')


class ScopeTypeFilterBackend(BaseFilterBackend):
    """Scope filters:

    * ?scope = ``URL``
    * ?scope_type = ``string`` (can be list)
    """

    content_type_field = 'content_type'
    scope_param = 'scope_type'
    scope_models = {
        'customer': models.Customer,
        'project': models.Project,
        'resource': models.BaseResource,
    }

    @classmethod
    def get_scope_type(cls, model):
        for scope_type, scope_model in cls.scope_models.items():
            if issubclass(model, scope_model):
                return scope_type

    @classmethod
    def _get_scope_models(cls, types):
        for scope_type, scope_model in cls.scope_models.items():
            if scope_type in types:
                try:
                    for submodel in scope_model.get_all_models():
                        yield submodel
                except AttributeError:
                    yield scope_model

    @classmethod
    def _get_scope_content_types(cls, types):
        return ContentType.objects.get_for_models(
            *cls._get_scope_models(types)
        ).values()

    def filter_queryset(self, request, queryset, view):
        if self.scope_param in request.query_params:
            content_types = self._get_scope_content_types(
                request.query_params.getlist(self.scope_param)
            )
            return queryset.filter(
                **{'%s__in' % self.content_type_field: content_types}
            )
        return queryset


class GenericRoleFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        queryset = filter_queryset_for_user(queryset, request.user)
        return filter_queryset_by_user_ip(queryset, request)


class GenericUserFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user_uuid = request.query_params.get('user_uuid')
        if not user_uuid:
            return queryset

        try:
            uuid.UUID(user_uuid)
        except ValueError:
            return queryset.none()

        try:
            user = User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            return queryset.none()

        return filter_queryset_for_user(queryset, user)


class CustomerFilter(NameFilterSet):
    query = django_filters.CharFilter(method='filter_query')
    native_name = django_filters.CharFilter(lookup_expr='icontains')
    abbreviation = django_filters.CharFilter(lookup_expr='icontains')
    contact_details = django_filters.CharFilter(lookup_expr='icontains')
    division_uuid = django_filters.ModelMultipleChoiceFilter(
        field_name='division__uuid',
        label='division_uuid',
        to_field_name='uuid',
        queryset=models.Division.objects.all(),
    )
    division_name = django_filters.CharFilter(
        field_name='division__name', lookup_expr='icontains'
    )
    division_type_uuid = django_filters.ModelMultipleChoiceFilter(
        field_name='division__type__uuid',
        label='division_type_uuid',
        to_field_name='uuid',
        queryset=models.DivisionType.objects.all(),
    )

    division_type_name = django_filters.CharFilter(
        field_name='division__type__name', lookup_expr='icontains'
    )

    class Meta:
        model = models.Customer
        fields = [
            'name',
            'abbreviation',
            'contact_details',
            'native_name',
            'registration_code',
            'agreement_number',
            'backend_id',
            'archived',
        ]

    def filter_query(self, queryset, name, value):
        if value:
            return queryset.filter(
                Q(name__icontains=value)
                | Q(native_name__icontains=value)
                | Q(abbreviation__icontains=value)
                | Q(domain__icontains=value)
                | Q(uuid__icontains=value)
                | Q(registration_code__icontains=value)
                | Q(agreement_number__contains=value)
                | Q(projects__name__icontains=value)
            ).distinct()
        return queryset


class OwnedByCurrentUserFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        value = request.query_params.get('owned_by_current_user')
        boolean_field = forms.NullBooleanField()

        try:
            value = boolean_field.to_python(value)
        except exceptions.ValidationError:
            value = None

        if value:
            return queryset.filter(
                permissions__user=request.user,
                permissions__is_active=True,
                permissions__role=models.CustomerRole.OWNER,
            )
        return queryset


class ExternalCustomerFilterBackend(ExternalFilterBackend):
    pass


class AccountingStartDateFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        query = Q(accounting_start_date__gt=timezone.now())
        return filter_by_accounting_is_running(request, queryset, query)


class CustomerAccountingStartDateFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if queryset.model == models.Customer:
            query = Q(accounting_start_date__gt=timezone.now())
        else:
            query = Q(customer__accounting_start_date__gt=timezone.now())
        return filter_by_accounting_is_running(request, queryset, query)


def filter_by_accounting_is_running(request, queryset, query):
    if not django_settings.WALDUR_CORE['ENABLE_ACCOUNTING_START_DATE']:
        return queryset

    value = request.query_params.get('accounting_is_running')
    boolean_field = forms.NullBooleanField()

    try:
        value = boolean_field.to_python(value)
    except exceptions.ValidationError:
        value = None

    if value is None:
        return queryset

    if value:
        return queryset.exclude(query)
    else:
        return queryset.filter(query)


class ProjectTypeFilter(NameFilterSet):
    class Meta:
        model = models.ProjectType
        fields = ['name']


class ProjectFilter(NameFilterSet):
    customer = django_filters.UUIDFilter(
        field_name='customer__uuid',
        distinct=True,
    )

    customer_name = django_filters.CharFilter(
        field_name='customer__name', distinct=True, lookup_expr='icontains'
    )

    customer_native_name = django_filters.CharFilter(
        field_name='customer__native_name', distinct=True, lookup_expr='icontains'
    )

    customer_abbreviation = django_filters.CharFilter(
        field_name='customer__abbreviation', distinct=True, lookup_expr='icontains'
    )

    description = django_filters.CharFilter(lookup_expr='icontains')

    query = django_filters.CharFilter(method='filter_query')

    o = django_filters.OrderingFilter(
        fields=(
            ('name', 'name'),
            ('created', 'created'),
            ('customer__name', 'customer_name'),
            ('customer__native_name', 'customer_native_name'),
            ('customer__abbreviation', 'customer_abbreviation'),
            ('estimated_cost', 'estimated_cost'),
        )
    )

    class Meta:
        model = models.Project
        fields = [
            'name',
            'customer',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            'description',
            'created',
            'query',
            'backend_id',
        ]

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            return queryset.filter(
                Q(uuid=value)
                | Q(resource__backend_id=value)
                | Q(resource__effective_id=value)
            )
        else:
            return queryset.filter(
                Q(resource__name=value)
                | Q(name__icontains=value)
                | Q(resource__backend_id=value)
                | Q(resource__effective_id=value)
            )


class CustomerUserFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer_uuid = request.query_params.get('customer_uuid')
        if not customer_uuid:
            return queryset

        try:
            uuid.UUID(customer_uuid)
        except ValueError:
            return queryset.none()

        return queryset.filter(
            Q(
                customerpermission__customer__uuid=customer_uuid,
                customerpermission__is_active=True,
            )
            | Q(
                projectpermission__project__customer__uuid=customer_uuid,
                projectpermission__is_active=True,
            )
        ).distinct()


class ProjectUserFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        project_uuid = request.query_params.get('project_uuid')
        if not project_uuid:
            return queryset

        try:
            uuid.UUID(project_uuid)
        except ValueError:
            return queryset.none()

        return queryset.filter(
            projectpermission__project__uuid=project_uuid,
            projectpermission__is_active=True,
        ).distinct()


def filter_visible_users(queryset, user, extra=None):
    connected_customers_query = models.Customer.objects.all()
    if not (user.is_staff or user.is_support):
        connected_customers_query = connected_customers_query.filter(
            Q(permissions__user=user, permissions__is_active=True)
            | Q(projects__permissions__user=user, projects__permissions__is_active=True)
        ).distinct()

    connected_customers = list(connected_customers_query.all())

    subquery = Q(
        customerpermission__customer__in=connected_customers,
        customerpermission__is_active=True,
    ) | Q(
        projectpermission__project__customer__in=connected_customers,
        projectpermission__is_active=True,
    )

    queryset = queryset.filter(subquery | Q(uuid=user.uuid) | (extra or Q())).distinct()

    if not (user.is_staff or user.is_support):
        queryset = queryset.filter(is_active=True, is_staff=False)

    return queryset


class UserFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user

        if not django_settings.WALDUR_CORE.get('SHOW_ALL_USERS', False) and not (
            user.is_staff or user.is_support
        ):
            queryset = filter_visible_users(queryset, user, self.get_extra_q(user))

        return queryset.order_by('username')

    _extra_query = []

    @classmethod
    def register_extra_query(cls, func_get_query):
        """
        Add extra Q for user list queryset
        :param func_get_query: a function that takes User object and returns Q object
        :return: None
        """
        cls._extra_query.append(func_get_query)

    @classmethod
    def get_extra_q(cls, user):
        result = Q()
        for q in cls._extra_query:
            result = result | q(user)
        return result


class BaseUserFilter(django_filters.FilterSet):
    full_name = django_filters.CharFilter(
        method='filter_by_full_name', label='Full name'
    )
    username = django_filters.CharFilter()
    native_name = django_filters.CharFilter(lookup_expr='icontains')
    organization = django_filters.CharFilter(lookup_expr='icontains')
    job_title = django_filters.CharFilter(lookup_expr='icontains')
    email = django_filters.CharFilter(lookup_expr='icontains')
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value)

    class Meta:
        model = User
        fields = [
            'full_name',
            'native_name',
            'organization',
            'email',
            'phone_number',
            'description',
            'job_title',
            'username',
            'civil_number',
            'is_active',
            'registration_method',
        ]


class UserFilter(BaseUserFilter):
    is_staff = django_filters.BooleanFilter(widget=BooleanWidget)
    is_support = django_filters.BooleanFilter(widget=BooleanWidget)
    username = django_filters.CharFilter(field_name='username', lookup_expr='exact')

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            (('first_name', 'last_name'), 'full_name'),
            'native_name',
            'email',
            'phone_number',
            'description',
            'organization',
            'job_title',
            'username',
            'is_active',
            'registration_method',
            'is_staff',
            'is_support',
        )
    )


class UserConcatenatedNameOrderingBackend(BaseFilterBackend):
    """Filter user by concatenated first_name + last_name + username with ?o=concatenated_name"""

    def filter_queryset(self, request, queryset, view):
        queryset = self._filter_queryset(request, queryset, view)
        return BaseUserFilter(
            request.query_params, queryset=queryset, request=request
        ).qs

    def _filter_queryset(self, request, queryset, view):
        if 'o' not in request.query_params:
            return queryset
        if request.query_params['o'] == 'concatenated_name':
            order_by = 'concatenated_name'
        elif request.query_params['o'] == '-concatenated_name':
            order_by = '-concatenated_name'
        else:
            return queryset
        return queryset.annotate(
            concatenated_name=Concat('first_name', 'last_name', 'username')
        ).order_by(order_by)


class UserPermissionFilter(django_filters.FilterSet):
    user = django_filters.UUIDFilter(field_name='user__uuid')
    user_url = core_filters.URLFilter(
        view_name='user-detail',
        field_name='user__uuid',
    )
    username = django_filters.CharFilter(
        field_name='user__username',
        lookup_expr='exact',
    )
    full_name = django_filters.CharFilter(
        method='filter_by_full_name', label='User full name contains'
    )
    native_name = django_filters.CharFilter(
        field_name='user__native_name',
        lookup_expr='icontains',
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, 'user')

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            ('user__username', 'username'),
            (('user__first_name', 'user__last_name'), 'full_name'),
            ('user__native_name', 'native_name'),
            ('user__email', 'email'),
            ('expiration_time', 'expiration_time'),
            ('created', 'created'),
            ('role', 'role'),
        )
    )


class ProjectPermissionFilter(UserPermissionFilter):
    class Meta:
        fields = ['role']
        model = models.ProjectPermission

    customer = django_filters.UUIDFilter(
        field_name='project__customer__uuid',
    )
    project = django_filters.UUIDFilter(
        field_name='project__uuid',
    )
    project_url = core_filters.URLFilter(
        view_name='project-detail',
        field_name='project__uuid',
    )
    project_name = django_filters.CharFilter(
        field_name='project__name',
        lookup_expr='icontains',
    )


class CustomerPermissionFilter(UserPermissionFilter):
    class Meta:
        fields = ['role']
        model = models.CustomerPermission

    customer = django_filters.UUIDFilter(
        field_name='customer__uuid',
    )
    customer_url = core_filters.URLFilter(
        view_name='customer-detail',
        field_name='customer__uuid',
    )
    customer_name = django_filters.CharFilter(
        field_name='customer__name',
        lookup_expr='icontains',
    )


class CustomerPermissionReviewFilter(django_filters.FilterSet):
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    reviewer_uuid = django_filters.UUIDFilter(field_name='reviewer__uuid')
    o = django_filters.OrderingFilter(fields=('created', 'closed'))

    class Meta:
        model = models.CustomerPermissionReview
        fields = [
            'is_pending',
        ]


class SshKeyFilter(NameFilterSet):
    uuid = django_filters.UUIDFilter()
    user_uuid = django_filters.UUIDFilter(field_name='user__uuid')

    o = django_filters.OrderingFilter(fields=('name',))

    class Meta:
        model = core_models.SshPublicKey
        fields = [
            'name',
            'fingerprint',
            'uuid',
            'user_uuid',
            'is_shared',
        ]


class ServiceTypeFilter(django_filters.Filter):
    def filter(self, qs, value):
        value = SupportedServices.get_filter_mapping().get(value)
        return super(ServiceTypeFilter, self).filter(qs, value)


class ServiceSettingsFilter(NameFilterSet):
    type = ServiceTypeFilter()
    state = core_filters.StateFilter()
    customer = django_filters.UUIDFilter(field_name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    scope_uuid = django_filters.UUIDFilter(
        method=core_filters.get_generic_field_filter(
            models.BaseResource.get_all_models()
        ),
        label='Scope UUID',
    )

    class Meta:
        model = models.ServiceSettings
        fields = ('name', 'type', 'state', 'shared', 'scope_uuid')


class ServiceSettingsScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return models.BaseResource.get_all_models()

    def get_field_name(self):
        return 'scope'


class BaseResourceFilter(NameFilterSet):
    def __init__(self, *args, **kwargs):
        super(BaseResourceFilter, self).__init__(*args, **kwargs)
        self.filters['o'] = django_filters.OrderingFilter(fields=self.ORDERING_FIELDS)

    # customer
    customer = django_filters.UUIDFilter(field_name='project__customer__uuid')
    customer_uuid = django_filters.UUIDFilter(field_name='project__customer__uuid')
    customer_name = django_filters.CharFilter(
        field_name='project__customer__name',
        lookup_expr='icontains',
    )
    customer_native_name = django_filters.CharFilter(
        field_name='project__customer__native_name',
        lookup_expr='icontains',
    )
    customer_abbreviation = django_filters.CharFilter(
        field_name='project__customer__abbreviation',
        lookup_expr='icontains',
    )
    # project
    project = django_filters.UUIDFilter(field_name='project__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    project_name = django_filters.CharFilter(
        field_name='project__name', lookup_expr='icontains'
    )
    # service settings
    service_settings_uuid = django_filters.UUIDFilter(
        field_name='service_settings__uuid'
    )
    service_settings_name = django_filters.CharFilter(
        field_name='service_settings__name',
        lookup_expr='icontains',
    )
    # resource
    description = django_filters.CharFilter(lookup_expr='icontains')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in core_models.StateMixin.States.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in core_models.StateMixin.States.CHOICES
        },
    )
    uuid = django_filters.UUIDFilter(lookup_expr='exact')
    backend_id = django_filters.CharFilter(field_name='backend_id', lookup_expr='exact')
    external_ip = core_filters.EmptyFilter()

    ORDERING_FIELDS = (
        ('name', 'name'),
        ('state', 'state'),
        ('project__customer__name', 'customer_name'),
        (
            'project__customer__native_name',
            'customer_native_name',
        ),
        (
            'project__customer__abbreviation',
            'customer_abbreviation',
        ),
        ('project__name', 'project_name'),
        ('service_settings__name', 'service_name'),
        ('created', 'created'),
    )

    class Meta:
        model = models.BaseResource
        fields = (
            # customer
            'customer',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            # project
            'project',
            'project_uuid',
            'project_name',
            # service settings
            'service_settings_name',
            'service_settings_uuid',
            # resource
            'name',
            'name_exact',
            'description',
            'state',
            'uuid',
            'backend_id',
        )


class StartTimeFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        order_by = get_ordering(request)
        if order_by not in ('start_time', '-start_time'):
            return queryset
        return order_with_nulls(queryset, order_by)


class BaseServicePropertyFilter(NameFilterSet):
    class Meta:
        fields = ('name',)


class ServicePropertySettingsFilter(BaseServicePropertyFilter):
    settings_uuid = django_filters.UUIDFilter(field_name='settings__uuid')
    settings = core_filters.URLFilter(
        view_name='servicesettings-detail', field_name='settings__uuid', distinct=True
    )

    class Meta(BaseServicePropertyFilter.Meta):
        fields = BaseServicePropertyFilter.Meta.fields + ('settings',)


class DivisionFilter(NameFilterSet):
    type = django_filters.CharFilter(field_name='type__name', lookup_expr='iexact')
    type_uuid = django_filters.UUIDFilter(field_name='type__uuid')
    type_url = core_filters.URLFilter(
        view_name='division-type-detail',
        field_name='type__uuid',
    )
    parent = django_filters.UUIDFilter(field_name='parent__uuid')

    class Meta:
        model = models.Division
        fields = [
            'name',
        ]


class DivisionTypesFilter(NameFilterSet):
    class Meta:
        model = models.DivisionType
        fields = [
            'name',
        ]


class UserRolesFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer = view.get_object()
        project_roles = request.query_params.getlist('project_role')
        organization_roles = request.query_params.getlist('organization_role')

        query = Q()

        if project_roles:
            # Filter project permissions by current customer
            query = query | Q(
                projectpermission__role__in=project_roles,
                projectpermission__project__customer_id=customer.id,
                projectpermission__is_active=True,
            )

        if organization_roles:
            # Filter customer permissions by current customer
            query = query | Q(
                customerpermission__role__in=organization_roles,
                customerpermission__customer_id=customer.id,
                customerpermission__is_active=True,
            )

        return queryset.filter(query)


class ProjectEstimatedCostFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        order_by = get_ordering(request)
        if order_by not in ('estimated_cost', '-estimated_cost'):
            return queryset

        ct = ContentType.objects.get_for_model(models.Project)
        estimates = billing_models.PriceEstimate.objects.filter(
            content_type=ct, object_id=OuterRef('pk')
        )
        queryset = queryset.annotate(
            estimated_cost=Subquery(estimates.values('total')[:1])
        )
        return order_with_nulls(queryset, order_by)


class NotificationTemplateFilter(NameFilterSet):
    path = django_filters.CharFilter(lookup_expr='icontains')
    path_exact = django_filters.CharFilter(field_name='path', lookup_expr='exact')

    class Meta:
        model = core_models.NotificationTemplate
        fields = [
            'name',
            'path',
        ]
