from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from waldur_client import WaldurClient, WaldurClientException

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_COMPONENT_FIELDS,
    OFFERING_FIELDS,
    PLAN_FIELDS,
)

from . import serializers


class RemoteView(APIView):
    def get_client(self, request):
        serializer = serializers.CredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        api_url = serializer.validated_data['api_url']
        token = serializer.validated_data['token']
        return WaldurClient(api_url, token)


class CustomersView(RemoteView):
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        params = {
            'owned_by_current_user': True,
            'field': ['uuid', 'name', 'abbreviation', 'phone_number', 'email'],
        }
        try:
            customers = client.list_customers(params)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        return Response(customers)


class OfferingsListView(RemoteView):
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        if 'customer_uuid' not in request.query_params:
            raise ValidationError(
                {'url': _('customer_uuid field must be present in query parameters')}
            )

        # TODO: update after https://opennode.atlassian.net/browse/WAL-4093
        # remote_customer_uuid = request.query_params['customer_uuid']
        whitelist_types = [
            offering_type
            for offering_type in plugins.manager.get_offering_types()
            if plugins.manager.enable_remote_support(offering_type)
        ]

        params = {
            'shared': True,
            # TODO: update after https://opennode.atlassian.net/browse/WAL-4093
            # 'customer_uuid': remote_customer_uuid,
            'type': whitelist_types,
            'field': ['uuid', 'name', 'type', 'state', 'category_title'],
        }
        try:
            remote_offerings = client.list_marketplace_offerings(params)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        local_offerings = list(
            models.Offering.objects.filter(type=PLUGIN_NAME)
            .exclude(state=models.Offering.States.ARCHIVED)
            .values_list('backend_id', flat=True)
        )

        importable_offerings = [
            offering
            for offering in remote_offerings
            if offering['uuid'] not in local_offerings
        ]
        return Response(importable_offerings)


class OfferingCreateView(RemoteView):
    def post(self, request, *args, **kwargs):
        serializer = serializers.OfferingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client = self.get_client(request)

        api_url = serializer.validated_data['api_url']
        token = serializer.validated_data['token']
        remote_offering_uuid = serializer.validated_data['remote_offering_uuid']
        remote_customer_uuid = serializer.validated_data['remote_customer_uuid']
        local_customer_uuid = serializer.validated_data['local_customer_uuid']
        local_category_uuid = serializer.validated_data['local_category_uuid']

        local_customer = Customer.objects.get(uuid=local_customer_uuid)
        local_category = models.Category.objects.get(uuid=local_category_uuid)

        try:
            remote_offering = client.get_marketplace_offering(remote_offering_uuid)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        secret_options = {
            'api_url': api_url,
            'token': token,
            'customer_uuid': remote_customer_uuid,
        }
        local_offering = self.import_offering(
            remote_offering, local_customer, local_category, secret_options
        )

        return Response({'uuid': local_offering.uuid.hex})

    def import_offering(
        self, remote_offering, local_customer, local_category, secret_options
    ):
        local_offering = models.Offering.objects.create(
            type=PLUGIN_NAME,
            billable=True,
            backend_id=remote_offering['uuid'],
            customer=local_customer,
            category=local_category,
            secret_options=secret_options,
            **{key: remote_offering[key] for key in OFFERING_FIELDS}
        )
        local_components_map = self.import_offering_components(
            local_offering, remote_offering
        )
        self.import_plans(local_offering, remote_offering, local_components_map)
        return local_offering

    def import_offering_components(self, local_offering, remote_offering):
        local_components_map = {}
        for remote_component in remote_offering['components']:
            local_component = models.OfferingComponent.objects.create(
                offering=local_offering,
                **{key: remote_component[key] for key in OFFERING_COMPONENT_FIELDS}
            )
            local_components_map[local_component.type] = local_component
        return local_components_map

    def import_plans(self, local_offering, remote_offering, local_components_map):
        for remote_plan in remote_offering['plans']:
            local_plan = models.Plan.objects.create(
                offering=local_offering,
                backend_id=remote_plan['uuid'],
                **{key: remote_plan[key] for key in PLAN_FIELDS}
            )
            remote_prices = remote_plan['prices']
            remote_quotas = remote_plan['quotas']
            components = set(remote_prices.keys()) | set(remote_quotas.keys())
            for component_type in components:
                models.PlanComponent.objects.create(
                    plan=local_plan,
                    component=local_components_map[component_type],
                    price=remote_prices[component_type],
                    amount=remote_quotas[component_type],
                )
