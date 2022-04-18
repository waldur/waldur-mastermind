import copy
import logging

import reversion
from django.conf import settings
from django.db import transaction
from django.db.models import (
    Count,
    ExpressionWrapper,
    F,
    OuterRef,
    PositiveSmallIntegerField,
    Q,
    Subquery,
)
from django.db.models.aggregates import Sum
from django.db.models.fields import FloatField
from django.db.models.functions.math import Ceil
from django.http.response import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from rest_framework import exceptions as rf_exceptions
from rest_framework import mixins
from rest_framework import permissions as rf_permissions
from rest_framework import status, views
from rest_framework import viewsets as rf_viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response

from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.core.utils import is_uuid_like, month_start, order_with_nulls
from waldur_core.media.utils import format_pdf_response
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import utils as structure_utils
from waldur_core.structure import views as structure_views
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.permissions import _has_owner_access
from waldur_core.structure.registry import get_resource_type
from waldur_core.structure.serializers import (
    ProjectUserSerializer,
    get_resource_serializer_class,
)
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import serializers as invoice_serializers
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace.utils import validate_attributes
from waldur_pid import models as pid_models

from . import filters, log, models, permissions, plugins, serializers, tasks, utils

logger = logging.getLogger(__name__)


class BaseMarketplaceView(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    update_permissions = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_owner
    ]


class PublicViewsetMixin:
    def get_permissions(self):
        if settings.WALDUR_MARKETPLACE[
            'ANONYMOUS_USER_CAN_VIEW_OFFERINGS'
        ] and self.action in ['list', 'retrieve']:
            return [rf_permissions.AllowAny()]
        else:
            return super(PublicViewsetMixin, self).get_permissions()


class ServiceProviderViewSet(PublicViewsetMixin, BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all().order_by('customer__name')
    serializer_class = serializers.ServiceProviderSerializer
    filterset_class = filters.ServiceProviderFilter
    api_secret_code_permissions = (
        projects_permissions
    ) = (
        project_permissions_permissions
    ) = keys_permissions = users_permissions = set_offerings_username_permissions = [
        structure_permissions.is_owner
    ]

    @action(detail=True, methods=['GET', 'POST'])
    def api_secret_code(self, request, uuid=None):
        """On GET request - return service provider api_secret_code.
        On POST - generate new service provider api_secret_code.
        """
        service_provider = self.get_object()
        if request.method == 'GET':
            return Response(
                {'api_secret_code': service_provider.api_secret_code},
                status=status.HTTP_200_OK,
            )
        else:
            service_provider.generate_api_secret_code()
            service_provider.save()
            return Response(
                {
                    'detail': _('Api secret code updated.'),
                    'api_secret_code': service_provider.api_secret_code,
                },
                status=status.HTTP_200_OK,
            )

    def get_customer_project_ids(self):
        service_provider = self.get_object()
        return utils.get_service_provider_project_ids(service_provider)

    def get_customer_user_ids(self):
        service_provider = self.get_object()
        return utils.get_service_provider_user_ids(self.request.user, service_provider)

    @action(detail=True, methods=['GET'])
    def projects(self, request, uuid=None):
        project_ids = self.get_customer_project_ids()
        projects = structure_models.Project.available_objects.filter(id__in=project_ids)
        page = self.paginate_queryset(projects)
        serializer = structure_serializers.ProjectSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=['GET'])
    def project_permissions(self, request, uuid=None):
        project_ids = self.get_customer_project_ids()
        permissions = structure_models.ProjectPermission.objects.filter(
            project_id__in=project_ids, is_active=True
        )
        page = self.paginate_queryset(permissions)
        serializer = structure_serializers.ProjectPermissionLogSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=['GET'])
    def keys(self, request, uuid=None):
        user_ids = self.get_customer_user_ids()
        keys = core_models.SshPublicKey.objects.filter(user_id__in=user_ids)
        page = self.paginate_queryset(keys)
        serializer = structure_serializers.SshKeySerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=['GET'])
    def users(self, request, uuid=None):
        user_ids = self.get_customer_user_ids()
        users = core_models.User.objects.filter(id__in=user_ids)
        page = self.paginate_queryset(users)
        serializer = structure_serializers.UserSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    def check_related_resources(request, view, obj=None):
        if obj and obj.has_active_offerings:
            raise rf_exceptions.ValidationError(
                _('Service provider has active offerings. Please archive them first.')
            )

    destroy_permissions = [structure_permissions.is_owner, check_related_resources]

    @action(detail=True, methods=['POST'])
    def set_offerings_username(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_uuid = serializer.validated_data['user_uuid']
        username = serializer.validated_data['username']

        try:
            user = core_models.User.objects.get(uuid=user_uuid)
        except core_models.User.DoesNotExist:
            validation_message = f'A user with the uuid [{user_uuid}] is not found.'
            raise rf_exceptions.ValidationError(_(validation_message))

        user_projects_ids = structure_models.ProjectPermission.objects.filter(
            user=user,
            is_active=True,
        ).values_list('project_id', flat=True)
        offering_ids = (
            models.Resource.objects.exclude(state=models.Resource.States.TERMINATED)
            .filter(
                project_id__in=user_projects_ids,
                offering__customer=self.get_object().customer,
            )
            .values_list('offering_id', flat=True)
        )

        for offering_id in offering_ids:
            models.OfferingUser.objects.update_or_create(
                user=user, offering_id=offering_id, defaults={'username': username}
            )

        return Response(
            {
                'detail': _('Offering users have been set.'),
            },
            status=status.HTTP_201_CREATED,
        )

    set_offerings_username_serializer_class = serializers.SetOfferingsUsernameSerializer


class CategoryViewSet(PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.CategorySerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryFilter

    create_permissions = (
        update_permissions
    ) = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_staff
    ]


def can_update_offering(request, view, obj=None):
    offering = obj

    if not offering:
        return

    if offering.state == models.Offering.States.DRAFT:
        if offering.has_user(request.user) or _has_owner_access(
            request.user, offering.customer
        ):
            return
        else:
            raise rf_exceptions.PermissionDenied()
    else:
        structure_permissions.is_staff(request, view)


def validate_offering_update(offering):
    if offering.state == models.Offering.States.ARCHIVED:
        raise rf_exceptions.ValidationError(
            _('It is not possible to update archived offering.')
        )


class OfferingViewSet(
    core_views.CreateReversionMixin,
    core_views.UpdateReversionMixin,
    PublicViewsetMixin,
    BaseMarketplaceView,
):
    """
    This viewset enables uniform implementation of resource import.

    Consider the following example:

    importable_resources_backend_method = 'get_tenants_for_import'
    import_resource_executor = executors.TenantImportExecutor

    It is expected that importable_resources_backend_method returns list of dicts, each of which
    contains two mandatory fields: name and backend_id, and one optional field called extra.
    This optional field should be list of dicts, each of which contains two mandatory fields: name and value.

    Note that there are only 3 mandatory parameters:
    * importable_resources_backend_method
    * importable_resources_serializer_class
    * import_resource_serializer_class
    """

    queryset = models.Offering.objects.all()
    serializer_class = serializers.OfferingDetailsSerializer
    create_serializer_class = serializers.OfferingCreateSerializer
    update_serializer_class = (
        partial_update_serializer_class
    ) = serializers.OfferingUpdateSerializer
    filterset_class = filters.OfferingFilter
    filter_backends = (
        DjangoFilterBackend,
        filters.OfferingCustomersFilterBackend,
        filters.OfferingImportableFilterBackend,
        filters.ExternalOfferingFilterBackend,
    )

    def get_queryset(self):
        queryset = super(OfferingViewSet, self).get_queryset()
        if self.request.user.is_anonymous:
            return queryset.filter(
                state__in=[
                    models.Offering.States.ACTIVE,
                    models.Offering.States.PAUSED,
                ],
                shared=True,
            )
        return queryset

    @action(detail=True, methods=['post'])
    def activate(self, request, uuid=None):
        return self._update_state('activate')

    @action(detail=True, methods=['post'])
    def draft(self, request, uuid=None):
        return self._update_state('draft')

    @action(detail=True, methods=['post'])
    def pause(self, request, uuid=None):
        return self._update_state('pause', request)

    pause_serializer_class = serializers.OfferingPauseSerializer

    @action(detail=True, methods=['post'])
    def unpause(self, request, uuid=None):
        return self._update_state('unpause', request)

    @action(detail=True, methods=['post'])
    def archive(self, request, uuid=None):
        return self._update_state('archive')

    def _update_state(self, action, request=None):
        offering = self.get_object()

        try:
            getattr(offering, action)()
        except TransitionNotAllowed:
            raise rf_exceptions.ValidationError(_('Offering state is invalid.'))

        with reversion.create_revision():
            if request:
                serializer = self.get_serializer(
                    offering, data=request.data, partial=True
                )
                serializer.is_valid(raise_exception=True)
                offering = serializer.save()

            offering.save(update_fields=['state'])
            reversion.set_user(self.request.user)
            reversion.set_comment(
                f'Offering state has been updated using method {action}'
            )
        return Response(
            {
                'detail': _('Offering state updated.'),
                'state': offering.get_state_display(),
            },
            status=status.HTTP_200_OK,
        )

    pause_permissions = unpause_permissions = archive_permissions = [
        permissions.user_is_owner_or_service_manager,
    ]

    activate_permissions = [structure_permissions.is_staff]

    activate_validators = pause_validators = archive_validators = destroy_validators = [
        structure_utils.check_customer_blocked
    ]

    update_permissions = partial_update_permissions = [can_update_offering]

    update_validators = partial_update_validators = [
        validate_offering_update,
        structure_utils.check_customer_blocked,
    ]

    def perform_create(self, serializer):
        customer = serializer.validated_data['customer']
        structure_utils.check_customer_blocked(customer)

        super(OfferingViewSet, self).perform_create(serializer)

    @action(detail=True, methods=['get'])
    def importable_resources(self, request, uuid=None):
        offering = self.get_object()
        method = plugins.manager.get_importable_resources_backend_method(offering.type)
        if not method:
            raise rf_exceptions.ValidationError(
                'Current offering plugin does not support resource import'
            )

        backend = offering.scope.get_backend()
        resources = getattr(backend, method)()
        page = self.paginate_queryset(resources)
        return self.get_paginated_response(page)

    importable_resources_permissions = [permissions.user_can_list_importable_resources]

    import_resource_permissions = [permissions.user_can_list_importable_resources]

    import_resource_serializer_class = serializers.ImportResourceSerializer

    @action(detail=True, methods=['post'])
    def import_resource(self, request, uuid=None):
        import_resource_serializer = self.get_serializer(data=request.data)
        import_resource_serializer.is_valid(raise_exception=True)

        plan = import_resource_serializer.validated_data.get('plan', None)
        project = import_resource_serializer.validated_data['project']
        backend_id = import_resource_serializer.validated_data['backend_id']

        offering = self.get_object()
        backend = offering.scope.get_backend()
        method = plugins.manager.import_resource_backend_method(offering.type)
        if not method:
            raise rf_exceptions.ValidationError(
                'Current offering plugin does not support resource import'
            )

        resource_model = plugins.manager.get_resource_model(offering.type)

        if resource_model.objects.filter(
            service_settings=offering.scope, backend_id=backend_id
        ).exists():
            raise rf_exceptions.ValidationError(
                _('Resource has been imported already.')
            )

        try:
            resource = getattr(backend, method)(backend_id=backend_id, project=project)
        except ServiceBackendError as e:
            raise rf_exceptions.ValidationError(str(e))
        else:
            resource_imported.send(
                sender=resource.__class__,
                instance=resource,
                plan=plan,
                offering=offering,
            )

        import_resource_executor = plugins.manager.get_import_resource_executor(
            offering.type
        )

        if import_resource_executor:
            transaction.on_commit(lambda: import_resource_executor.execute(resource))

        marketplace_resource = models.Resource.objects.get(scope=resource)
        resource_serializer = serializers.ResourceSerializer(
            marketplace_resource, context=self.get_serializer_context()
        )

        return Response(data=resource_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def update_attributes(self, request, uuid=None):
        offering = self.get_object()
        if not isinstance(request.data, dict):
            raise rf_exceptions.ValidationError('Dictionary is expected.')
        validate_attributes(request.data, offering.category)
        offering.attributes = request.data
        with reversion.create_revision():
            offering.save(update_fields=['attributes'])
            reversion.set_user(self.request.user)
            reversion.set_comment('Offering attributes have been updated via REST API')
        return Response(status=status.HTTP_200_OK)

    update_attributes_permissions = [permissions.user_is_owner_or_service_manager]
    update_attributes_validators = [validate_offering_update]

    def _update_action(self, request):
        offering = self.get_object()
        serializer = self.get_serializer(offering, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def update_location(self, request, uuid=None):
        return self._update_action(request)

    update_location_permissions = [permissions.user_is_owner_or_service_manager]
    update_location_validators = [validate_offering_update]
    update_location_serializer_class = serializers.OfferingLocationUpdateSerializer

    @action(detail=True, methods=['post'])
    def update_description(self, request, uuid=None):
        return self._update_action(request)

    update_description_permissions = [permissions.user_is_owner_or_service_manager]
    update_description_validators = [validate_offering_update]
    update_description_serializer_class = (
        serializers.OfferingDescriptionUpdateSerializer
    )

    @action(detail=True, methods=['post'])
    def update_overview(self, request, uuid=None):
        return self._update_action(request)

    update_overview_permissions = [permissions.user_is_owner_or_service_manager]
    update_overview_validators = [validate_offering_update]
    update_overview_serializer_class = serializers.OfferingOverviewUpdateSerializer

    @action(detail=True, methods=['post'])
    def update_options(self, request, uuid=None):
        return self._update_action(request)

    update_options_permissions = [permissions.user_is_owner_or_service_manager]
    update_options_validators = [validate_offering_update]
    update_options_serializer_class = serializers.OfferingOptionsUpdateSerializer

    @action(detail=True, methods=['post'])
    def update_secret_options(self, request, uuid=None):
        return self._update_action(request)

    update_secret_options_permissions = [permissions.user_is_owner_or_service_manager]
    update_secret_options_validators = [validate_offering_update]
    update_secret_options_serializer_class = (
        serializers.OfferingSecretOptionsUpdateSerializer
    )

    @action(detail=True, methods=['post'])
    def update_thumbnail(self, request, uuid=None):
        offering = self.get_object()
        serializer = serializers.OfferingThumbnailSerializer(
            instance=offering, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_thumbnail_permissions = [permissions.user_can_update_thumbnail]

    @action(detail=True, methods=['post'])
    def delete_thumbnail(self, request, uuid=None):
        offering = self.get_object()
        offering.thumbnail = None
        offering.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_thumbnail_permissions = update_thumbnail_permissions

    @action(detail=True)
    def customers(self, request, uuid):
        offering = self.get_object()
        active_customers = utils.get_active_customers(request, self)
        customer_queryset = utils.get_offering_customers(offering, active_customers)
        serializer_class = structure_serializers.CustomerSerializer
        serializer = serializer_class(
            instance=customer_queryset, many=True, context=self.get_serializer_context()
        )
        page = self.paginate_queryset(serializer.data)
        return self.get_paginated_response(page)

    customers_permissions = [structure_permissions.is_owner]

    def get_stats(self, get_queryset, serializer, serializer_context=None):
        offering = self.get_object()
        active_customers = utils.get_active_customers(self.request, self)
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource__offering=offering,
            invoice__customer__in=active_customers,
            invoice__created__gte=start,
            invoice__created__lte=end,
        )
        queryset = get_queryset(invoice_items)
        serializer = serializer(
            instance=queryset, many=True, context=serializer_context
        )
        page = self.paginate_queryset(serializer.data)
        return self.get_paginated_response(page)

    @action(detail=True)
    def costs(self, *args, **kwargs):
        return self.get_stats(utils.get_offering_costs, serializers.CostsSerializer)

    costs_permissions = [structure_permissions.is_owner]

    @action(detail=True)
    def component_stats(self, *args, **kwargs):
        offering = self.get_object()
        offering_components_map = {
            component.type: component for component in offering.components.all()
        }

        def get_offering_component_stats(invoice_items):
            return (
                invoice_items.filter(
                    details__offering_component_type__in=offering_components_map.keys()
                )
                .values(
                    'details__offering_component_type',
                    'invoice__year',
                    'invoice__month',
                )
                .order_by(
                    'details__offering_component_type',
                    'invoice__year',
                    'invoice__month',
                )
                .annotate(total_quantity=Sum('quantity'))
            )

        serializer_context = {
            'offering_components_map': offering_components_map,
        }
        return self.get_stats(
            get_offering_component_stats,
            serializers.OfferingComponentStatSerializer,
            serializer_context,
        )

    component_stats_permissions = [structure_permissions.is_owner]

    @action(detail=True, methods=['post'])
    def update_divisions(self, request, uuid):
        offering = self.get_object()
        serializer = serializers.DivisionsSerializer(
            instance=offering, context={'request': request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_divisions_permissions = [structure_permissions.is_owner]

    @action(detail=True, methods=['post'])
    def delete_divisions(self, request, uuid=None):
        offering = self.get_object()
        offering.divisions.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_divisions_permissions = update_divisions_permissions

    @action(detail=False, permission_classes=[], filter_backends=[DjangoFilterBackend])
    def groups(self, *args, **kwargs):
        OFFERING_LIMIT = 4
        qs = self.filter_queryset(
            self.get_queryset().filter(shared=True, state=models.Offering.States.ACTIVE)
        )
        customer_ids = self.paginate_queryset(
            qs.order_by('customer__name')
            .values_list('customer_id', flat=True)
            .distinct()
        )
        customers = {
            customer.id: customer
            for customer in structure_models.Customer.objects.filter(
                id__in=customer_ids
            )
        }
        return self.get_paginated_response(
            data=[
                {
                    'customer_name': customers[customer_id].name,
                    'customer_uuid': customers[customer_id].uuid.hex,
                    'offerings': [
                        {
                            'offering_name': offering.name,
                            'offering_uuid': offering.uuid.hex,
                        }
                        for offering in qs.filter(customer_id=customer_id)[
                            :OFFERING_LIMIT
                        ]
                    ],
                }
                for customer_id in customer_ids
            ]
        )


class OfferingReferralsViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    queryset = pid_models.DataciteReferral.objects.all()
    serializer_class = serializers.OfferingReferralSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.OfferingReferralScopeFilterBackend,
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.OfferingReferralFilter


class OfferingPermissionViewSet(structure_views.BasePermissionViewSet):
    queryset = models.OfferingPermission.objects.filter(is_active=True).order_by(
        '-created'
    )
    serializer_class = serializers.OfferingPermissionSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.OfferingPermissionFilter
    scope_field = 'offering'


class OfferingPermissionLogViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    queryset = models.OfferingPermission.objects.filter(is_active=None).order_by(
        'offering__name'
    )
    serializer_class = serializers.OfferingPermissionLogSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.OfferingPermissionFilter


class PlanUsageReporter:
    """
    This class provides aggregate counts of how many plans of a
    certain type for each offering is used.
    """

    def __init__(self, view, request):
        self.view = view
        self.request = request

    def get_report(self):
        plans = models.Plan.objects.exclude(offering__billable=False)

        query = self.parse_query()
        if query:
            plans = self.apply_filters(query, plans)

        resources = self.get_subquery()
        remaining = ExpressionWrapper(
            F('limit') - F('usage'), output_field=PositiveSmallIntegerField()
        )
        plans = plans.annotate(
            usage=Subquery(resources[:1]), limit=F('max_amount')
        ).annotate(remaining=remaining)
        plans = self.apply_ordering(plans)

        return self.serialize(plans)

    def parse_query(self):
        if self.request.query_params:
            serializer = serializers.PlanUsageRequestSerializer(
                data=self.request.query_params
            )
            serializer.is_valid(raise_exception=True)
            return serializer.validated_data
        return None

    def get_subquery(self):
        # Aggregate
        resources = (
            models.Resource.objects.filter(plan_id=OuterRef('pk'))
            .exclude(state=models.Resource.States.TERMINATED)
            .annotate(count=Count('*'))
            .order_by()
            .values_list('count', flat=True)
        )

        # Workaround for Django bug:
        # https://code.djangoproject.com/ticket/28296
        # It allows to remove extra GROUP BY clause from the subquery.
        resources.query.group_by = []

        return resources

    def apply_filters(self, query, plans):
        if query.get('offering_uuid'):
            plans = plans.filter(offering__uuid=query.get('offering_uuid'))

        if query.get('customer_provider_uuid'):
            plans = plans.filter(
                offering__customer__uuid=query.get('customer_provider_uuid')
            )

        return plans

    def apply_ordering(self, plans):
        param = (
            self.request.query_params and self.request.query_params.get('o') or '-usage'
        )
        return order_with_nulls(plans, param)

    def serialize(self, plans):
        page = self.view.paginate_queryset(plans)
        serializer = serializers.PlanUsageResponseSerializer(page, many=True)
        return self.view.get_paginated_response(serializer.data)


def validate_plan_update(plan):
    if models.Resource.objects.filter(plan=plan).exists():
        raise rf_exceptions.ValidationError(
            _('It is not possible to update plan because it is used by resources.')
        )


def validate_plan_archive(plan):
    if plan.archived:
        raise rf_exceptions.ValidationError(_('Plan is already archived.'))


class PlanViewSet(core_views.UpdateReversionMixin, BaseMarketplaceView):
    queryset = models.Plan.objects.all()
    serializer_class = serializers.PlanDetailsSerializer
    filterset_class = filters.PlanFilter
    filter_backends = (DjangoFilterBackend, filters.PlanFilterBackend)

    disabled_actions = ['destroy']
    update_validators = partial_update_validators = [validate_plan_update]

    archive_permissions = [structure_permissions.is_owner]
    archive_validators = [validate_plan_archive]

    @action(detail=True, methods=['post'])
    def archive(self, request, uuid=None):
        plan = self.get_object()
        with reversion.create_revision():
            plan.archived = True
            plan.save(update_fields=['archived'])
            reversion.set_user(self.request.user)
            reversion.set_comment('Plan has been archived.')
        return Response(
            {'detail': _('Plan has been archived.')}, status=status.HTTP_200_OK
        )

    @action(detail=False)
    def usage_stats(self, request):
        return PlanUsageReporter(self, request).get_report()

    @action(detail=True, methods=['post'])
    def update_divisions(self, request, uuid):
        plan = self.get_object()
        serializer = serializers.DivisionsSerializer(
            instance=plan, context={'request': request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_divisions_permissions = [structure_permissions.is_owner]

    @action(detail=True, methods=['post'])
    def delete_divisions(self, request, uuid=None):
        plan = self.get_object()
        plan.divisions.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_divisions_permissions = update_divisions_permissions


class PlanComponentViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.PlanComponent.objects.filter()
    serializer_class = serializers.PlanComponentSerializer
    filterset_class = filters.PlanComponentFilter
    lookup_field = 'uuid'

    def get_queryset(self):
        queryset = super(PlanComponentViewSet, self).get_queryset()
        if self.request.user.is_anonymous:
            return queryset.filter(
                plan__offering__shared=True,
            )


class ScreenshotViewSet(
    core_views.CreateReversionMixin,
    core_views.UpdateReversionMixin,
    BaseMarketplaceView,
):
    queryset = models.Screenshot.objects.all().order_by('offering__name')
    serializer_class = serializers.ScreenshotSerializer
    filterset_class = filters.ScreenshotFilter


class OrderViewSet(BaseMarketplaceView):
    queryset = models.Order.objects.all()
    serializer_class = serializers.OrderSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OrderFilter
    destroy_validators = partial_update_validators = [
        structure_utils.check_customer_blocked
    ]

    def get_queryset(self):
        """
        Orders are available to both service provider and service consumer.
        """
        if self.request.user.is_staff or self.request.user.is_support:
            return self.queryset

        return self.queryset.filter(
            Q(
                project__permissions__user=self.request.user,
                project__permissions__is_active=True,
            )
            | Q(
                project__customer__permissions__user=self.request.user,
                project__customer__permissions__is_active=True,
            )
            | Q(
                items__offering__customer__permissions__user=self.request.user,
                items__offering__customer__permissions__is_active=True,
            )
        ).distinct()

    @action(detail=True, methods=['post'])
    def approve(self, request, uuid=None):
        tasks.approve_order(self.get_object(), request.user)

        return Response(
            {'detail': _('Order has been approved.')}, status=status.HTTP_200_OK
        )

    approve_validators = [
        core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL),
        structure_utils.check_customer_blocked,
        structure_utils.check_project_end_date,
    ]
    approve_permissions = [permissions.user_can_approve_order_permission]

    @action(detail=True, methods=['post'])
    def reject(self, request, uuid=None):
        order = self.get_object()
        order.reject()
        order.save(update_fields=['state'])
        return Response(
            {'detail': _('Order has been rejected.')}, status=status.HTTP_200_OK
        )

    reject_validators = [
        core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL),
        structure_utils.check_customer_blocked,
    ]
    reject_permissions = [permissions.user_can_reject_order]

    @action(detail=True)
    def pdf(self, request, uuid=None):
        order = self.get_object()

        file = utils.create_order_pdf(order)
        filename = order.get_filename()
        return format_pdf_response(file, filename)

    def perform_create(self, serializer):
        project = serializer.validated_data['project']
        structure_utils.check_customer_blocked(project)
        structure_utils.check_project_end_date(project)

        super(OrderViewSet, self).perform_create(serializer)


class PluginViewSet(views.APIView):
    def get(self, request):
        offering_types = plugins.manager.get_offering_types()
        payload = []
        for offering_type in offering_types:
            components = [
                dict(
                    type=component.type,
                    name=component.name,
                    measured_unit=component.measured_unit,
                    billing_type=component.billing_type,
                )
                for component in plugins.manager.get_components(offering_type)
            ]
            payload.append(
                dict(
                    offering_type=offering_type,
                    components=components,
                    available_limits=plugins.manager.get_available_limits(
                        offering_type
                    ),
                )
            )
        return Response(payload, status=status.HTTP_200_OK)


class OrderItemViewSet(BaseMarketplaceView):
    queryset = models.OrderItem.objects.all()
    filter_backends = (DjangoFilterBackend,)
    serializer_class = serializers.OrderItemDetailsSerializer
    filterset_class = filters.OrderItemFilter

    def order_items_destroy_validator(order_item):
        if not order_item:
            return
        if order_item.order.state != models.Order.States.REQUESTED_FOR_APPROVAL:
            raise rf_exceptions.PermissionDenied()

    destroy_validators = [order_items_destroy_validator]
    destroy_permissions = terminate_permissions = [
        structure_permissions.is_administrator
    ]

    def get_queryset(self):
        """
        OrderItems are available to both service provider and service consumer.
        """
        if self.request.user.is_staff or self.request.user.is_support:
            return self.queryset

        return self.queryset.filter(
            Q(
                order__project__permissions__user=self.request.user,
                order__project__permissions__is_active=True,
            )
            | Q(
                order__project__customer__permissions__user=self.request.user,
                order__project__customer__permissions__is_active=True,
            )
            | Q(
                offering__customer__permissions__user=self.request.user,
                offering__customer__permissions__is_active=True,
            )
        ).distinct()

    approve_permissions = [permissions.can_approve_order_item]

    reject_permissions = [permissions.can_reject_order_item]

    # Approve action is enabled for service provider, and
    # reject action is enabled for both provider and consumer.
    # Pending order item for remote offering is executed after it is approved by service provider.

    @action(detail=True, methods=['post'])
    def reject(self, request, uuid=None):
        order_item = self.get_object()

        if order_item.state == models.OrderItem.States.EXECUTING:
            if not order_item.resource:
                raise ValidationError('Order item does not have a resource.')
            callbacks.sync_order_item_state(
                order_item, models.OrderItem.States.TERMINATED
            )
        elif order_item.state == models.OrderItem.States.PENDING:
            order_item.reviewed_at = timezone.now()
            order_item.reviewed_by = request.user
            order_item.set_state_terminated()
            order_item.save()
            if (
                order_item.order.state == models.Order.States.REQUESTED_FOR_APPROVAL
                and order_item.order.items.filter(
                    state=models.OrderItem.States.PENDING
                ).count()
                == 0
            ):
                order_item.order.reject()
                order_item.order.save()
        else:
            raise ValidationError('Order item is not in executing or pending state.')
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def approve(self, request, uuid=None):
        order_item = self.get_object()

        if order_item.state == models.OrderItem.States.EXECUTING:
            if not order_item.resource:
                raise ValidationError('Order item does not have a resource.')
            callbacks.sync_order_item_state(order_item, models.OrderItem.States.DONE)
        elif order_item.state == models.OrderItem.States.PENDING:
            order_item.reviewed_at = timezone.now()
            order_item.reviewed_by = request.user
            order_item.set_state_executing()
            order_item.save()
            if (
                order_item.order.state == models.Order.States.REQUESTED_FOR_APPROVAL
                and order_item.order.items.filter(
                    state=models.OrderItem.States.PENDING
                ).count()
                == 0
            ):
                order_item.order.approve()
                order_item.order.save()
            transaction.on_commit(
                lambda: tasks.process_order_item.delay(
                    core_utils.serialize_instance(order_item),
                    core_utils.serialize_instance(request.user),
                )
            )
        else:
            raise ValidationError('Order item is not in executing or pending state.')
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def terminate(self, request, uuid=None):
        order_item = self.get_object()
        if not plugins.manager.can_terminate_order_item(order_item.offering.type):
            return Response(
                {
                    'details': 'Order item could not be terminated because it is not supported by plugin.'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # It is expected that plugin schedules Celery task to call backend
            # and then switches order item to terminated state.
            order_item.set_state_terminating()
            order_item.save(update_fields=['state'])
        except TransitionNotAllowed:
            return Response(
                {
                    'details': 'Order item could not be terminated because it has been already processed.'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {'details': 'Order item termination has been scheduled.'},
            status=status.HTTP_202_ACCEPTED,
        )


class CartItemViewSet(core_views.ActionsViewSet):
    queryset = models.CartItem.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.CartItemSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CartItemFilter

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    @action(detail=False, methods=['post'])
    def submit(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        order_serializer = serializers.OrderSerializer(
            instance=order, context=self.get_serializer_context()
        )
        return Response(order_serializer.data, status=status.HTTP_201_CREATED)

    submit_serializer_class = serializers.CartSubmitSerializer


class ResourceViewSet(core_views.ActionsViewSet):
    queryset = models.Resource.objects.all()
    filter_backends = (DjangoFilterBackend, filters.ResourceScopeFilterBackend)
    filterset_class = filters.ResourceFilter
    lookup_field = 'uuid'
    serializer_class = serializers.ResourceSerializer
    disabled_actions = ['create', 'destroy']
    update_serializer_class = (
        partial_update_serializer_class
    ) = serializers.ResourceUpdateSerializer

    def get_queryset(self):
        return self.queryset.filter_for_user(self.request.user)

    @action(detail=True, methods=['get'])
    def details(self, request, uuid=None):
        resource = self.get_object()
        if not resource.scope:
            return Response(status=status.HTTP_404_NOT_FOUND)
        resource_type = get_resource_type(resource.scope)
        serializer_class = get_resource_serializer_class(resource_type)
        if not serializer_class:
            return Response(status.HTTP_204_NO_CONTENT)
        serializer = serializer_class(
            instance=resource.scope, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def terminate(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attributes = serializer.validated_data.get('attributes', {})

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
                type=models.OrderItem.Types.TERMINATE,
                attributes=attributes,
            )
            utils.validate_order_item(order_item, request)
            order = serializers.create_order(
                project=resource.project,
                user=self.request.user,
                items=[order_item],
                request=request,
            )

        return Response({'order_uuid': order.uuid.hex}, status=status.HTTP_200_OK)

    terminate_serializer_class = serializers.ResourceTerminateSerializer

    terminate_permissions = [permissions.user_can_terminate_resource]

    terminate_validators = [
        core_validators.StateValidator(
            models.Resource.States.OK, models.Resource.States.ERRED
        ),
        utils.check_customer_blocked_for_terminating,
        utils.check_pending_order_item_exists,
    ]

    @action(detail=True, methods=['post'])
    def switch_plan(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data['plan']

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
                old_plan=resource.plan,
                plan=plan,
                type=models.OrderItem.Types.UPDATE,
                limits=resource.limits or {},
            )
            order = serializers.create_order(
                project=resource.project,
                user=self.request.user,
                items=[order_item],
                request=request,
            )

        return Response({'order_uuid': order.uuid.hex}, status=status.HTTP_200_OK)

    switch_plan_serializer_class = serializers.ResourceSwitchPlanSerializer

    @action(detail=True, methods=['post'])
    def update_limits(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        limits = serializer.validated_data['limits']

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
                plan=resource.plan,
                type=models.OrderItem.Types.UPDATE,
                limits=limits,
                attributes={'old_limits': resource.limits},
            )
            order = serializers.create_order(
                project=resource.project,
                user=self.request.user,
                items=[order_item],
                request=request,
            )

        return Response({'order_uuid': order.uuid.hex}, status=status.HTTP_200_OK)

    update_limits_serializer_class = serializers.ResourceUpdateLimitsSerializer

    switch_plan_permissions = update_limits_permissions = [
        structure_permissions.is_administrator
    ]

    switch_plan_validators = update_limits_validators = [
        core_validators.StateValidator(models.Resource.States.OK),
        structure_utils.check_customer_blocked,
        utils.check_pending_order_item_exists,
    ]

    @action(detail=True, methods=['get'])
    def plan_periods(self, request, uuid=None):
        resource = self.get_object()
        qs = models.ResourcePlanPeriod.objects.filter(resource=resource)
        qs = qs.filter(Q(end=None) | Q(end__gte=month_start(timezone.now())))
        serializer = serializers.ResourcePlanPeriodSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def move_resource(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data['project']
        try:
            utils.move_resource(resource, project)
        except utils.MoveResourceException as exception:
            error_message = str(exception)
            return JsonResponse({'error_message': error_message}, status=409)

        serialized_resource = serializers.ResourceSerializer(
            resource, context=self.get_serializer_context()
        )

        return Response(serialized_resource.data, status=status.HTTP_200_OK)

    move_resource_serializer_class = serializers.MoveResourceSerializer
    move_resource_permissions = [structure_permissions.is_staff]

    @action(detail=True, methods=['post'])
    def set_backend_id(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data['backend_id']
        old_backend_id = resource.backend_id
        if new_backend_id != old_backend_id:
            resource.backend_id = serializer.validated_data['backend_id']
            resource.save()
            logger.info(
                '%s has changed backend_id from %s to %s',
                request.user.full_name,
                old_backend_id,
                new_backend_id,
            )

            return Response(
                {'status': _('Resource backend_id has been changed.')},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {'status': _('Resource backend_id is not changed.')},
                status=status.HTTP_200_OK,
            )

    set_backend_id_permissions = [permissions.user_is_owner_or_service_manager]
    set_backend_id_serializer_class = serializers.ResourceBackendIDSerializer

    @action(detail=True, methods=['post'])
    def submit_report(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource.report = serializer.validated_data['report']
        resource.save(update_fields=['report'])

        return Response({'status': _('Report is submitted')}, status=status.HTTP_200_OK)

    submit_report_permissions = [
        permissions.user_is_service_provider_owner_or_service_provider_manager
    ]
    submit_report_serializer_class = serializers.ResourceReportSerializer

    def _set_end_date(self, request, is_staff_action):
        resource = self.get_object()
        serializer = serializers.ResourceEndDateByProviderSerializer(
            data=request.data, instance=resource, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        transaction.on_commit(
            lambda: tasks.notify_about_resource_termination.delay(
                resource.uuid.hex, request.user.uuid.hex, is_staff_action
            )
        )

        if not is_staff_action:
            log.log_marketplace_resource_end_date_has_been_updated_by_provider(
                resource, request.user
            )
        else:
            log.log_marketplace_resource_end_date_has_been_updated_by_staff(
                resource, request.user
            )

        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def set_end_date_by_provider(self, request, uuid=None):
        return self._set_end_date(request, False)

    set_end_date_by_provider_permissions = [
        permissions.user_can_set_end_date_by_provider
    ]

    @action(detail=True, methods=['post'])
    def set_end_date_by_staff(self, request, uuid=None):
        return self._set_end_date(request, True)

    set_end_date_by_staff_permissions = [structure_permissions.is_staff]

    # Service provider endpoint only
    @action(detail=True, methods=['get'])
    def team(self, request, uuid=None):
        resource = self.get_object()
        project = resource.project

        return Response(
            ProjectUserSerializer(
                instance=project.get_users(),
                many=True,
                context={'project': project, 'request': request},
            ).data,
            status=status.HTTP_200_OK,
        )

    team_permissions = [
        permissions.user_is_service_provider_owner_or_service_provider_manager
    ]


class ProjectChoicesViewSet(ListAPIView):
    def get_project(self):
        project_uuid = self.kwargs['project_uuid']
        if not is_uuid_like(project_uuid):
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data='Project UUID is invalid.'
            )
        return get_object_or_404(structure_models.Project, uuid=project_uuid)

    def get_category(self):
        category_uuid = self.kwargs['category_uuid']
        if not is_uuid_like(category_uuid):
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data='Category UUID is invalid.'
            )
        return get_object_or_404(models.Category, uuid=category_uuid)


class ResourceOfferingsViewSet(ProjectChoicesViewSet):
    serializer_class = serializers.ResourceOfferingSerializer

    def get_queryset(self):
        project = self.get_project()
        category = self.get_category()
        offerings = (
            models.Resource.objects.filter(project=project, offering__category=category)
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list('offering_id', flat=True)
        )
        return models.Offering.objects.filter(pk__in=offerings)


class RelatedCustomersViewSet(ListAPIView):
    serializer_class = structure_serializers.BasicCustomerSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.NameFilterSet

    def get_customer(self):
        customer_uuid = self.kwargs['customer_uuid']
        if not is_uuid_like(customer_uuid):
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data='Customer UUID is invalid.'
            )
        qs = filter_queryset_for_user(
            structure_models.Customer.objects.all(), self.request.user
        )
        return get_object_or_404(qs, uuid=customer_uuid)

    def get_queryset(self):
        customer = self.get_customer()
        customer_ids = (
            models.Resource.objects.all()
            .filter_for_user(self.request.user)
            .filter(offering__customer=customer)
            .values_list('project__customer_id', flat=True)
            .distinct()
        )
        return structure_models.Customer.objects.filter(id__in=customer_ids)


class CategoryComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.CategoryComponentUsage.objects.all().order_by(
        '-date', 'component__type'
    )
    filter_backends = (
        DjangoFilterBackend,
        filters.CategoryComponentUsageScopeFilterBackend,
    )
    filterset_class = filters.CategoryComponentUsageFilter
    serializer_class = serializers.CategoryComponentUsageSerializer


class ComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.ComponentUsage.objects.all().order_by('-date', 'component__type')
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUsageFilter
    serializer_class = serializers.ComponentUsageSerializer

    @action(detail=False, methods=['post'])
    def set_usage(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = serializer.validated_data['plan_period'].resource
        if not _has_owner_access(
            request.user, resource.offering.customer
        ) and not resource.offering.has_user(request.user):
            raise PermissionDenied(
                _(
                    'Only staff, service provider owner and service manager are allowed '
                    'to submit usage data for marketplace resource.'
                )
            )
        serializer.save()
        return Response(status=status.HTTP_201_CREATED)

    set_usage_serializer_class = serializers.ComponentUsageCreateSerializer


class MarketplaceAPIViewSet(rf_viewsets.ViewSet):
    """
    TODO: Move this viewset to  ComponentUsageViewSet.
    """

    permission_classes = ()
    serializer_class = serializers.ServiceProviderSignatureSerializer

    def get_validated_data(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data['data']
        dry_run = serializer.validated_data['dry_run']

        if self.action == 'set_usage':
            data_serializer = serializers.ComponentUsageCreateSerializer(data=data)
            data_serializer.is_valid(raise_exception=True)
            if not dry_run:
                data_serializer.save()

        return serializer.validated_data, dry_run

    @action(detail=False, methods=['post'])
    @csrf_exempt
    def check_signature(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    @csrf_exempt
    def set_usage(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_201_CREATED)


class OfferingFileViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingFile.objects.all().order_by('name')
    filterset_class = filters.OfferingFileFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.OfferingFileSerializer
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        offering = serializer.validated_data['offering']

        if user.is_staff or (
            offering.customer
            and offering.customer.has_user(user, structure_models.CustomerRole.OWNER)
        ):
            return

        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [structure_permissions.is_owner]


class OfferingUsersViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    rf_viewsets.GenericViewSet,
):
    queryset = models.OfferingUser.objects.all()
    serializer_class = serializers.OfferingUserSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserFilter

    def get_queryset(self):
        queryset = super(OfferingUsersViewSet, self).get_queryset()
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            return queryset

        project_permissions = structure_models.ProjectPermission.objects.filter(
            user=current_user, is_active=True
        )
        project_ids = project_permissions.values_list('project_id', flat=True)
        customer_permissions = structure_models.CustomerPermission.objects.filter(
            user=current_user, is_active=True
        )
        customer_ids = customer_permissions.values_list('customer_id', flat=True)
        all_customer_ids = set(customer_ids) | set(
            structure_models.Project.objects.filter(id__in=project_ids).values_list(
                'customer_id', flat=True
            )
        )
        division_ids = structure_models.Customer.objects.filter(
            id__in=all_customer_ids
        ).values_list('division_id', flat=True)

        queryset = queryset.filter(
            # user can see own remote offering user
            Q(user=current_user)
            # service provider can see all records related to managed offerings
            | Q(
                offering__customer__permissions__user=current_user,
                offering__customer__permissions__is_active=True,
            )
            # users with project permission are visible to other users in the same project
            | Q(
                user__projectpermission__project__in=project_ids,
                user__projectpermission__is_active=True,
            )
            # users with customer permission are visible to other users in the same customer
            | Q(
                user__customerpermission__customer__in=customer_ids,
                user__customerpermission__is_active=True,
            )
            # users with project permission are visible to other users in the same customer
            | Q(
                user__projectpermission__project__customer__in=customer_ids,
                user__projectpermission__is_active=True,
            )
        ).distinct()
        queryset = queryset.filter(
            # only offerings managed by customer where the current user has a role
            Q(offering__customer__id__in=all_customer_ids)
            |
            # only offerings from divisions including the current user's customers
            Q(offering__divisions__in=division_ids)
        )
        return queryset


class StatsViewSet(rf_viewsets.ViewSet):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsSupport]

    @action(detail=False, methods=['get'])
    def organization_project_count(self, request, *args, **kwargs):
        data = structure_models.Project.available_objects.values(
            'customer__abbreviation', 'customer__name', 'customer__uuid'
        ).annotate(count=Count('customer__uuid'))
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=['get'])
    def organization_resource_count(self, request, *args, **kwargs):
        data = (
            models.Resource.objects.filter(state=models.Resource.States.OK)
            .values(
                'project__customer__abbreviation',
                'project__customer__name',
                'project__customer__uuid',
            )
            .annotate(count=Count('project__customer__uuid'))
        )
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=['get'])
    def customer_member_count(self, request, *args, **kwargs):
        data = (
            structure_models.CustomerPermission.objects.filter(is_active=True)
            .values('customer__abbreviation', 'customer__name', 'customer__uuid')
            .annotate(count=Count('customer__uuid'))
        )
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=['get'])
    def project_member_count(self, request, *args, **kwargs):
        data = (
            structure_models.ProjectPermission.objects.filter(is_active=True)
            .values(
                'project__customer__abbreviation',
                'project__customer__name',
                'project__customer__uuid',
            )
            .annotate(count=Count('project__customer__uuid'))
        )
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=['get'])
    def resources_limits(self, request, *args, **kwargs):
        data = []

        for resource in (
            models.Resource.objects.filter(state=models.Resource.States.OK)
            .exclude(limits={})
            .values('offering__uuid', 'limits')
        ):
            limits = resource['limits']

            for name, value in limits.items():
                if value > 0:
                    try:
                        prev = next(
                            filter(
                                lambda x: x['offering_uuid']
                                == resource['offering__uuid']
                                and x['name'] == name,
                                data,
                            )
                        )
                    except StopIteration:
                        prev = None

                    if not prev:
                        data.append(
                            {
                                'offering_uuid': resource['offering__uuid'],
                                'name': name,
                                'value': value,
                            }
                        )
                    else:
                        prev['value'] += value

        return Response(
            self._expand_result_with_information_of_divisions(data),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def component_usages(self, request, *args, **kwargs):
        now = timezone.now()
        data = (
            models.ComponentUsage.objects.filter(
                billing_period__year=now.year, billing_period__month=now.month
            )
            .values('resource__offering__uuid', 'component__type')
            .annotate(usage=Sum('usage'))
        )
        serializer = serializers.ComponentUsagesStatsSerializer(data, many=True)
        return Response(
            self._expand_result_with_information_of_divisions(serializer.data),
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _expand_result_with_information_of_divisions(result):
        data_with_divisions = []

        for record in result:
            offering = models.Offering.objects.get(uuid=record['offering_uuid'])
            record['offering_country'] = offering.country or offering.customer.country
            divisions = offering.divisions.all()

            if not divisions:
                new_data = copy.copy(record)
                new_data['division_name'] = ''
                new_data['division_uuid'] = ''
                data_with_divisions.append(new_data)
            else:
                for division in divisions:
                    new_data = copy.copy(record)
                    new_data['division_name'] = division.name
                    new_data['division_uuid'] = division.uuid.hex
                    data_with_divisions.append(new_data)

        return data_with_divisions

    @action(detail=False, methods=['get'])
    def count_users_of_service_providers(self, request, *args, **kwargs):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            'customer', 'customer__division'
        ):
            data = {
                'count': utils.get_service_provider_user_ids(
                    self.request.user, sp
                ).count()
            }
            data.update(self._get_service_provider_info(sp))
            result.append(data)

        return Response(
            result,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def count_projects_of_service_providers(self, request, *args, **kwargs):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            'customer', 'customer__division'
        ):
            data = {'count': utils.get_service_provider_project_ids(sp).count()}
            data.update(self._get_service_provider_info(sp))
            result.append(data)

        return Response(
            result,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def count_projects_of_service_providers_grouped_by_oecd(
        self, request, *args, **kwargs
    ):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            'customer', 'customer__division'
        ):
            project_ids = utils.get_service_provider_project_ids(sp)
            projects = (
                structure_models.Project.available_objects.filter(id__in=project_ids)
                .values('oecd_fos_2007_code')
                .annotate(count=Count('id'))
            )

            for p in projects:
                data = {
                    'count': p['count'],
                    'oecd_fos_2007_code': p['oecd_fos_2007_code'],
                }
                data.update(self._get_service_provider_info(sp))
                result.append(data)

        return Response(
            self._expand_result_with_oecd_name(result), status=status.HTTP_200_OK
        )

    def _count_projects_grouped_by_field(self, field_name):
        results = (
            structure_models.Project.objects.filter()
            .values(field_name)
            .annotate(count=Count('id'))
        )

        return results

    @action(detail=False, methods=['get'])
    def count_projects_grouped_by_oecd(self, request, *args, **kwargs):
        return Response(
            self._expand_result_with_oecd_name(
                self._count_projects_grouped_by_field('oecd_fos_2007_code')
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def count_projects_grouped_by_industry_flag(self, request, *args, **kwargs):
        return Response(
            self._count_projects_grouped_by_field('is_industry'),
            status=status.HTTP_200_OK,
        )

    def _projects_usages_grouped_by_field(self, field_name):
        results = {}

        for project in structure_models.Project.objects.all():
            field_value = str(getattr(project, field_name))
            if field_value in results:
                results[field_value]['projects_ids'].append(project.id)
            else:
                results[field_value] = {
                    'projects_ids': [project.id],
                }

        now = timezone.now()

        for key, result in results.items():
            ids = result.pop('projects_ids')
            usages = (
                models.ComponentUsage.objects.filter(
                    billing_period__year=now.year,
                    billing_period__month=now.month,
                    resource__project__id__in=ids,
                )
                .values('component__type')
                .annotate(usage=Sum('usage'))
            )

            for usage in usages:
                result[usage['component__type']] = usage['usage']

        return results

    @action(detail=False, methods=['get'])
    def projects_usages_grouped_by_oecd(self, request, *args, **kwargs):
        return Response(
            self._replace_keys_from_oecd_code_to_oecd_name(
                self._projects_usages_grouped_by_field('oecd_fos_2007_code')
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def projects_usages_grouped_by_industry_flag(self, request, *args, **kwargs):
        return Response(
            self._projects_usages_grouped_by_field('is_industry'),
            status=status.HTTP_200_OK,
        )

    def _projects_limits_grouped_by_field(self, field_name):
        results = {}

        for project in structure_models.Project.objects.all():
            field_value = str(getattr(project, field_name))
            if field_value in results:
                results[field_value]['projects_ids'].append(project.id)
            else:
                results[field_value] = {
                    'projects_ids': [project.id],
                }

        for key, result in results.items():
            ids = result.pop('projects_ids')

            for resource in (
                models.Resource.objects.filter(
                    state=models.Resource.States.OK, project__id__in=ids
                )
                .exclude(limits={})
                .values('offering__uuid', 'limits')
            ):
                limits = resource['limits']

                for name, value in limits.items():
                    if value > 0:
                        if name in result:
                            result[name] += value
                        else:
                            result[name] = value

        return results

    @action(detail=False, methods=['get'])
    def projects_limits_grouped_by_oecd(self, request, *args, **kwargs):
        return Response(
            self._replace_keys_from_oecd_code_to_oecd_name(
                self._projects_limits_grouped_by_field('oecd_fos_2007_code')
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def projects_limits_grouped_by_industry_flag(self, request, *args, **kwargs):
        return Response(
            self._projects_limits_grouped_by_field('is_industry'),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def total_cost_of_active_resources_per_offering(self, request, *args, **kwargs):
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        invoice_items = (
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start,
                invoice__created__lte=end,
            )
            .values('resource__offering__uuid')
            .annotate(
                cost=Sum(
                    (Ceil(F('quantity') * F('unit_price') * 100) / 100),
                    output_field=FloatField(),
                )
            )
        )

        serializer = serializers.OfferingCostSerializer(invoice_items, many=True)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _get_service_provider_info(service_provider):
        return {
            'service_provider_uuid': service_provider.uuid.hex,
            'customer_uuid': service_provider.customer.uuid.hex,
            'customer_name': service_provider.customer.name,
            'customer_division_uuid': service_provider.customer.division.uuid.hex
            if service_provider.customer.division
            else '',
            'customer_division_name': service_provider.customer.division.name
            if service_provider.customer.division
            else '',
        }

    @staticmethod
    def _expand_result_with_oecd_name(data):
        if not hasattr(data, '__iter__'):
            return data

        for d in data:
            if not isinstance(d, dict):
                return data

            if 'oecd_fos_2007_code' in d.keys():
                name = [
                    c[1]
                    for c in structure_models.Project.OECD_FOS_2007_CODES
                    if c[0] == d['oecd_fos_2007_code']
                ]
                if name:
                    d['oecd_fos_2007_name'] = name[0]
                else:
                    d['oecd_fos_2007_name'] = ''

        return data

    @staticmethod
    def _replace_keys_from_oecd_code_to_oecd_name(data):
        if not isinstance(data, dict):
            return data

        results = {}
        for code, value in data.items():
            name = [
                c[1]
                for c in structure_models.Project.OECD_FOS_2007_CODES
                if c[0] == code
            ]
            if name:
                results['%s %s' % (code, str(name[0]))] = value
            else:
                results[code] = value

        return results


class ProviderInvoiceItemsViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = invoice_models.InvoiceItem.objects.all().order_by('-invoice__created')
    filter_backends = (
        DjangoFilterBackend,
        filters.MarketplaceInvoiceItemsFilterBackend,
    )
    filterset_class = filters.MarketplaceInvoiceItemsFilter
    serializer_class = invoice_serializers.InvoiceItemSerializer


for view in (structure_views.ProjectCountersView, structure_views.CustomerCountersView):

    def inject_resources_counter(scope):
        counters = models.AggregateResourceCount.objects.filter(scope=scope).only(
            'count', 'category'
        )
        return {
            'marketplace_category_{}'.format(counter.category.uuid): counter.count
            for counter in counters
        }

    view.register_dynamic_counter(inject_resources_counter)
