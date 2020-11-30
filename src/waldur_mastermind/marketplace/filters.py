import json

import django_filters
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from django_filters.widgets import BooleanWidget
from rest_framework import exceptions as rf_exceptions
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core.utils import is_uuid_like
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_pid import models as pid_models

from . import models


class ServiceProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    o = django_filters.OrderingFilter(fields=(('customer__name', 'customer_name'),))

    class Meta:
        model = models.ServiceProvider
        fields = []


class BaseOfferingFilter(structure_filters.NameFilterSet, django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    project_uuid = django_filters.UUIDFilter(method='filter_project')
    allowed_customer_uuid = django_filters.UUIDFilter(
        field_name='customer__uuid', method='filter_allowed_customer'
    )
    attributes = django_filters.CharFilter(
        field_name='attributes', method='filter_attributes'
    )
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.Offering.States.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.Offering.States.CHOICES
        },
    )
    category_uuid = django_filters.UUIDFilter(field_name='category__uuid')
    billable = django_filters.BooleanFilter(widget=BooleanWidget)
    o = django_filters.OrderingFilter(fields=('name', 'created'))

    def filter_allowed_customer(self, queryset, name, value):
        return queryset.filter_for_customer(value)

    def filter_project(self, queryset, name, value):
        return queryset.filter_for_project(value)

    def filter_attributes(self, queryset, name, value):
        try:
            value = json.loads(value)
        except ValueError:
            raise rf_exceptions.ValidationError(
                _('Filter attribute is not valid json.')
            )

        if not isinstance(value, dict):
            raise rf_exceptions.ValidationError(
                _('Filter attribute should be an dict.')
            )

        for k, v in value.items():
            if isinstance(v, list):
                # If a filter value is a list, use multiple choice.
                queryset = queryset.filter(
                    **{'attributes__{key}__has_any_keys'.format(key=k): v}
                )
            else:
                queryset = queryset.filter(attributes__contains={k: v})
        return queryset


class OfferingFilter(BaseOfferingFilter):
    shared = django_filters.BooleanFilter(widget=BooleanWidget)

    class Meta:
        model = models.Offering
        fields = ['shared', 'type']


class OfferingCustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        return queryset.filter_for_user(request.user)


class OfferingImportableFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if 'importable' in request.query_params:
            user = request.user

            if user.is_staff:
                return queryset

            used_offerings_ids = []

            queryset = queryset.filter(shared=False)

            owned_customers = set(
                structure_models.Customer.objects.all()
                .filter(
                    permissions__user=user,
                    permissions__is_active=True,
                    permissions__role=structure_models.CustomerRole.OWNER,
                )
                .distinct()
            )

            owned_offerings_ids = list(
                queryset.filter(
                    Q(allowed_customers__in=owned_customers)
                    | Q(customer__in=owned_customers)
                ).values_list('id', flat=True)
            )

            # Import private offerings must be available for admins and managers
            projects_ids = list(
                structure_models.ProjectPermission.objects.filter(
                    is_active=True,
                    user_id=user.id,
                    role__in=(
                        structure_models.ProjectRole.ADMINISTRATOR,
                        structure_models.ProjectRole.MANAGER,
                    ),
                ).values_list('project_id', flat=True)
            )

            for offering in queryset.all():
                if (
                    offering.scope
                    and offering.scope.scope
                    and offering.scope.scope.service_project_link
                    and offering.scope.scope.service_project_link.project.id
                    in projects_ids
                ):
                    used_offerings_ids.append(offering.id)

            return queryset.filter(
                id__in=list(owned_offerings_ids + used_offerings_ids)
            )
        return queryset


class OfferingFilterMixin:
    offering = django_filters.UUIDFilter(field_name='offering__uuid')
    offering_uuid = core_filters.URLFilter(
        view_name='marketplace-offering-detail', field_name='offering__uuid',
    )


class OfferingPermissionFilter(
    OfferingFilterMixin, structure_filters.UserPermissionFilter
):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='offering__customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='offering__customer__uuid')

    class Meta:
        model = models.OfferingPermission
        fields = []


class ScreenshotFilter(OfferingFilterMixin, django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=('name', 'created'))

    class Meta:
        model = models.Screenshot
        fields = []


class CartItemFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='project__customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='project__customer__uuid')
    project = core_filters.URLFilter(
        view_name='project-detail', field_name='project__uuid'
    )
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')

    class Meta:
        model = models.CartItem
        fields = []


class OrderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='project__customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='project__customer__uuid')
    project = core_filters.URLFilter(
        view_name='project-detail', field_name='project__uuid'
    )
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.Order.States.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.Order.States.CHOICES
        },
    )
    o = django_filters.OrderingFilter(
        fields=('created', 'approved_at', 'total_cost', 'state')
    )

    class Meta:
        model = models.Order
        fields = []


class OrderItemFilter(OfferingFilterMixin, django_filters.FilterSet):
    project_uuid = django_filters.UUIDFilter(field_name='order__project__uuid')
    category_uuid = django_filters.UUIDFilter(field_name='offering__category__uuid')
    provider_uuid = django_filters.UUIDFilter(field_name='offering__customer__uuid')
    customer_uuid = django_filters.UUIDFilter(
        field_name='order__project__customer__uuid'
    )
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.OrderItem.States.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.OrderItem.States.CHOICES
        },
    )
    type = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.OrderItem.Types.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.OrderItem.Types.CHOICES
        },
    )
    order = core_filters.URLFilter(
        view_name='marketplace-order-detail', field_name='order__uuid'
    )
    order_uuid = django_filters.UUIDFilter(field_name='order__uuid')

    resource = core_filters.URLFilter(
        view_name='marketplace-resource-detail', field_name='resource__uuid'
    )
    resource_uuid = django_filters.UUIDFilter(field_name='resource__uuid')

    o = django_filters.OrderingFilter(fields=('created',))

    class Meta:
        model = models.OrderItem
        fields = []


class ResourceFilter(
    OfferingFilterMixin, structure_filters.NameFilterSet, django_filters.FilterSet
):
    query = django_filters.CharFilter(method='filter_query')
    offering_type = django_filters.CharFilter(field_name='offering__type')
    offering_billable = django_filters.UUIDFilter(field_name='offering__billable')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    project_name = django_filters.CharFilter(field_name='project__name')
    customer_uuid = django_filters.UUIDFilter(field_name='project__customer__uuid')
    category_uuid = django_filters.UUIDFilter(field_name='offering__category__uuid')
    provider_uuid = django_filters.UUIDFilter(field_name='offering__customer__uuid')
    backend_id = django_filters.CharFilter(method='filter_backend_id')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.Resource.States.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.Resource.States.CHOICES
        },
    )
    o = django_filters.OrderingFilter(fields=('name', 'created',))

    class Meta:
        model = models.Resource
        fields = []

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            return queryset.filter(uuid=value)
        else:
            return queryset.filter(name__icontains=value)

    def filter_backend_id(self, queryset, name, value):
        resource_models = [
            b['resource_model']
            for b in manager.backends.values()
            if 'resource_model' in b.keys()
        ]
        for resource_model in resource_models:
            resources_ids = resource_model.objects.filter(backend_id=value).values_list(
                'id', flat=True
            )

            if not resources_ids:
                continue

            ct = ContentType.objects.get_for_model(resource_model)
            return queryset.filter(content_type=ct, object_id__in=resources_ids)

        return queryset.none()


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return []

    def get_field_name(self):
        return 'scope'


class PlanFilter(OfferingFilterMixin, django_filters.FilterSet):
    class Meta:
        model = models.Plan
        fields = []


class CategoryComponentUsageScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return [structure_models.Project, structure_models.Customer]

    def get_field_name(self):
        return 'scope'


class CategoryComponentUsageFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryComponentUsage
        fields = []

    date_before = django_filters.DateFilter(field_name='date', lookup_expr='lte')
    date_after = django_filters.DateFilter(field_name='date', lookup_expr='gte')


class ComponentUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name='marketplace-resource-detail', field_name='resource__uuid'
    )
    resource_uuid = django_filters.UUIDFilter(field_name='resource__uuid')
    offering_uuid = django_filters.UUIDFilter(field_name='resource__offering__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='resource__project__uuid')
    customer_uuid = django_filters.UUIDFilter(
        field_name='resource__project__customer__uuid'
    )
    date_before = django_filters.DateFilter(field_name='date', lookup_expr='lte')
    date_after = django_filters.DateFilter(field_name='date', lookup_expr='gte')
    type = django_filters.CharFilter(field_name='component__type')

    class Meta:
        model = models.ComponentUsage
        fields = []


class OfferingReferralFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(
        fields=('published', 'relation_type', 'resource_type',)
    )

    class Meta:
        model = pid_models.DataciteReferral
        fields = []


class OfferingReferralScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def is_anonymous_allowed(self):
        return settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_OFFERINGS']

    def get_related_models(self):
        return [models.Offering]

    def get_field_name(self):
        return 'scope'


class OfferingFileFilter(OfferingFilterMixin, django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=('name', 'created'))

    class Meta:
        model = models.OfferingFile
        fields = []


class ExternalOfferingFilterBackend(core_filters.ExternalFilterBackend):
    pass


class CustomerResourceFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if 'has_resources' in request.query_params:
            customers = models.Resource.objects.all().values_list(
                'project__customer_id', flat=True
            )
            queryset = queryset.filter(pk__in=customers)
        return queryset


class ServiceProviderOfferingFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer_uuid = request.query_params.get('service_provider_uuid')

        if customer_uuid and is_uuid_like(customer_uuid):
            customers = models.Resource.objects.filter(
                offering__customer__uuid=customer_uuid
            ).values_list('project__customer_id', flat=True)
            queryset = queryset.filter(pk__in=customers)
        return queryset


class CustomerServiceProviderFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        is_service_provider = request.query_params.get('is_service_provider')
        if is_service_provider in ['true', 'True']:
            customers = models.ServiceProvider.objects.values_list(
                'customer_id', flat=True
            )
            return queryset.filter(pk__in=customers)
        return queryset


structure_filters.ExternalCustomerFilterBackend.register(CustomerResourceFilter())
structure_filters.ExternalCustomerFilterBackend.register(
    ServiceProviderOfferingFilter()
)
structure_filters.ExternalCustomerFilterBackend.register(
    CustomerServiceProviderFilter()
)
