from collections import defaultdict

from rest_framework import status, exceptions, response
from rest_framework.decorators import detail_route, list_route
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.serializers import HistorySerializer
from waldur_core.core.utils import datetime_to_timestamp, pwgen
from waldur_core.monitoring.utils import get_period
from waldur_core.structure import views as structure_views

from . import models, serializers, filters, executors
from .managers import filter_active


class ZabbixServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.ZabbixService.objects.all()
    serializer_class = serializers.ServiceSerializer

    def get_serializer_class(self):
        if self.action == 'credentials':
            return serializers.UserSerializer
        elif self.action == 'trigger_status':
            return serializers.TriggerRequestSerializer
        return super(ZabbixServiceViewSet, self).get_serializer_class()

    @detail_route(methods=['GET', 'POST'])
    def credentials(self, request, uuid):
        """ On GET request - return superadmin user data.
            On POST - reset superuser password and return new one.
        """
        service = self.get_object()
        if request.method == 'GET':
            user = models.User.objects.get(settings=service.settings, alias=service.settings.username)
            serializer_class = self.get_serializer_class()
            serializer = serializer_class(user, context=self.get_serializer_context())
            return Response(serializer.data)
        else:
            password = pwgen()
            executors.ServiceSettingsPasswordResetExecutor.execute(service.settings, password=password)
            return Response({'password': password})

    @detail_route(methods=['GET', 'HEAD'])
    def trigger_status(self, request, uuid):
        serializer_class = self.get_serializer_class()
        serializer = serializer_class(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data

        service = self.get_object()
        backend = service.get_backend()
        headers = {
            'X-Result-Count': backend.get_trigger_count(query),
        }

        if request.method == 'HEAD':
            return Response(headers=headers)

        backend_triggers = backend.get_trigger_status(query)
        response_serializer = serializers.TriggerResponseSerializer(
            instance=backend_triggers, many=True)

        page = self.paginate_queryset(response_serializer.data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(response_serializer.data, headers=headers)


class ZabbixServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.ZabbixServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer


class BaseZabbixResourceViewSet(structure_views.ResourceViewSet):
    def perform_provision(self, serializer):
        resource = serializer.save()
        backend = resource.get_backend()
        backend.provision(resource)


class NoHostsException(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'There are no OK hosts that match given query.'


class NoItemsException(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'There are no items that match given query.'


class HostViewSet(structure_views.ResourceViewSet):
    """ Representation of Zabbix hosts and related actions. """
    queryset = models.Host.objects.all()
    serializer_class = serializers.HostSerializer
    filter_backends = BaseZabbixResourceViewSet.filter_backends + (
        filters.HostScopeFilterBackend,
    )
    create_executor = executors.HostCreateExecutor
    update_executor = executors.HostUpdateExecutor
    delete_executor = executors.HostDeleteExecutor

    @detail_route()
    def items_history(self, request, uuid):
        """ Get host items historical values.

        Request should specify datetime points and items.
        There are two ways to define datetime points for historical data.

        1. Send *?point=<timestamp>* parameter that can list.
           Response will contain historical data for each given point in the same order.
        2. Send *?start=<timestamp>*, *?end=<timestamp>*, *?points_count=<integer>* parameters.
           Result will contain <points_count> points from <start> to <end>.

        Also you should specify one or more name of host template items, for example 'openstack.instance.cpu_util'

        Response is list of datapoints, each of which is dictionary with following fields:
         - 'point' - timestamp;
         - 'value' - values are converted from bytes to megabytes, if possible;
         - 'item' - key of host template item;
         - 'item_name' - name of host template item.
        """
        host = self.get_object()
        if host.state != models.Host.States.OK:
            raise IncorrectStateException('Host has to be OK to get items history.')
        stats = self._get_stats(request, [host])
        return Response(stats, status=status.HTTP_200_OK)

    @list_route()
    def aggregated_items_history(self, request):
        """ Get sum of hosts historical values.

        Request should specify host filtering parameters, datetime points, and items.
        Host filtering parameters are the same as for */api/zabbix-hosts/* endpoint.
        Input/output format is the same as for **/api/zabbix-hosts/<host_uuid>/items_history/** endpoint.
        """
        stats = self._get_stats(request, self._get_hosts())
        return Response(stats, status=status.HTTP_200_OK)

    @list_route()
    def items_aggregated_values(self, request):
        """ Get sum of aggregated hosts values.

        Request parameters:
         - ?start - start of aggregation period as timestamp. Default: 1 hour ago.
         - ?end - end of aggregation period as timestamp. Default: now.
         - ?method - aggregation method. Default: MAX. Choices: MIN, MAX.
         - ?item - item key. Can be list. Required.

        Response format: {<item key>: <aggregated value>, ...}

        Endpoint will return status 400 if there are no hosts or items that match request parameters.
        """
        hosts = self._get_hosts()
        serializer = serializers.ItemsAggregatedValuesSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filter_data = serializer.validated_data
        items = self._get_items(request, hosts)

        aggregated_data = defaultdict(lambda: 0)
        for host in hosts:
            backend = host.get_backend()
            host_aggregated_values = backend.get_items_aggregated_values(
                host, items, filter_data['start'], filter_data['end'], filter_data['method'])
            for key, value in host_aggregated_values.items():
                aggregated_data[key] += value
        return Response(aggregated_data, status=status.HTTP_200_OK)

    # TODO: make methods items_aggregated_values and items_values DRY.
    @detail_route()
    def items_values(self, request, uuid):
        """ The same as items_aggregated_values, only for one host """
        host = self.get_object()
        serializer = serializers.ItemsAggregatedValuesSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filter_data = serializer.validated_data
        items = self._get_items(request, [host])

        backend = host.get_backend()
        host_aggregated_values = backend.get_items_aggregated_values(
            host, items, filter_data['start'], filter_data['end'], filter_data['method'])
        return Response(host_aggregated_values, status=status.HTTP_200_OK)

    def _get_hosts(self):
        hosts = filter_active(self.filter_queryset(self.get_queryset()))
        if not hosts:
            raise NoHostsException()
        return hosts

    def _get_items(self, request, hosts):
        items = request.query_params.getlist('item')
        items = models.Item.objects.filter(template__hosts__in=hosts, key__in=items).distinct()
        if not items:
            raise NoItemsException()
        return items

    def _get_stats(self, request, hosts):
        """
        If item list contains several elements, result is ordered by item
        (in the same order as it has been provided in request) and then by time.
        """
        items = self._get_items(request, hosts)
        numeric_types = (models.Item.ValueTypes.FLOAT, models.Item.ValueTypes.INTEGER)
        non_numeric_items = [item.name for item in items if item.value_type not in numeric_types]
        if non_numeric_items:
            raise exceptions.ValidationError(
                'Cannot show historical data for non-numeric items: %s' % ', '.join(non_numeric_items))
        points = self._get_points(request)

        stats = []
        for item in items:
            values = self._sum_rows([
                host.get_backend().get_item_stats(host.backend_id, item, points)
                for host in hosts
            ])

            for point, value in zip(points, values):
                stats.append({
                    'point': point,
                    'item': item.key,
                    'item_name': item.name,
                    'value': value,
                })
        return stats

    def _get_points(self, request):
        mapped = {
            'start': request.query_params.get('start'),
            'end': request.query_params.get('end'),
            'points_count': request.query_params.get('points_count'),
            'point_list': request.query_params.getlist('point')
        }
        serializer = HistorySerializer(data={k: v for k, v in mapped.items() if v})
        serializer.is_valid(raise_exception=True)
        points = map(datetime_to_timestamp, serializer.get_filter_data())
        return points

    def _sum_rows(self, rows):
        """
        Input: [[1, 2], [10, 20], [None, None]]
        Output: [11, 22]
        """
        def sum_without_none(xs):
            return sum(x for x in xs if x)
        return map(sum_without_none, zip(*rows))


class ITServiceViewSet(structure_views.ResourceViewSet):
    queryset = models.ITService.objects.all().select_related('trigger')
    serializer_class = serializers.ITServiceSerializer
    lookup_field = 'uuid'
    create_executor = executors.ITServiceCreateExecutor
    delete_executor = executors.ITServiceDeleteExecutor
    # TODO: add update operation

    @detail_route()
    def events(self, request, uuid):
        itservice = self.get_object()
        period = get_period(request)

        history = get_object_or_404(models.SlaHistory, itservice=itservice, period=period)
        events = list(history.events.all().order_by('-timestamp').values('timestamp', 'state'))

        serializer = serializers.SlaHistoryEventSerializer(data=events, many=True)
        serializer.is_valid(raise_exception=True)

        return Response(serializer.data, status=status.HTTP_200_OK)


class TemplateViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Template.objects.all().prefetch_related('items')
    serializer_class = serializers.TemplateSerializer
    lookup_field = 'uuid'
    filter_class = filters.TemplateFilter


class TriggerViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Trigger.objects.all()
    serializer_class = serializers.TriggerSerializer
    lookup_field = 'uuid'
    filter_class = filters.TriggerFilter


class UserGroupViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.UserGroup.objects.all()
    serializer_class = serializers.UserGroupSerializer
    lookup_field = 'uuid'
    filter_class = filters.UserGroupFilter


class UserViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.User.objects.all()
    serializer_class = serializers.UserSerializer
    lookup_field = 'uuid'
    filter_class = filters.UserFilter
    create_executor = executors.UserCreateExecutor
    update_executor = executors.UserUpdateExecutor
    delete_executor = executors.UserDeleteExecutor

    @detail_route(methods=['post'])
    def password(self, request, uuid):
        user = self.get_object()
        user.password = pwgen()
        user.save()
        executors.UserUpdateExecutor.execute(user, updated_fields=['password'])
        return response.Response(
            {'detail': 'password update was scheduled successfully', 'password': user.password},
            status=status.HTTP_200_OK
        )
