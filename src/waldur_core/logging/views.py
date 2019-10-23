from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import response, viewsets, permissions, status, decorators, mixins

from waldur_core.core import filters as core_filters, permissions as core_permissions
from waldur_core.core.managers import SummaryQuerySet
from waldur_core.logging import models, serializers, filters, utils
from waldur_core.logging.loggers import get_event_groups


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Event.objects.all()
    permission_classes = (permissions.IsAuthenticated, core_permissions.IsAdminOrReadOnly)
    serializer_class = serializers.EventSerializer
    filter_backends = (DjangoFilterBackend, filters.EventFilterBackend)
    filterset_class = filters.EventFilter

    @decorators.action(detail=False)
    def count(self, request, *args, **kwargs):
        """
        To get a count of events - run **GET** against */api/events/count/* as authenticated user.
        Endpoint support same filters as events list.

        Response example:

        .. code-block:: javascript

            {"count": 12321}
        """

        self.queryset = self.filter_queryset(self.get_queryset())
        return response.Response({'count': self.queryset.count()}, status=status.HTTP_200_OK)

    @decorators.action(detail=False)
    def scope_types(self, request, *args, **kwargs):
        """ Returns a list of scope types acceptable by events filter. """
        return response.Response(utils.get_scope_types_mapping().keys())

    @decorators.action(detail=False)
    def event_groups(self, request, *args, **kwargs):
        """
        Returns a list of groups with event types.
        Group is used in exclude_features query param.
        """
        return response.Response(get_event_groups())


class BaseHookViewSet(viewsets.ModelViewSet):
    """
    Hooks API allows user to receive event notifications via different channel, like email or webhook.
    To get a list of all your hooks, run **GET** against */api/hooks/* as an authenticated user.
    """
    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)
    lookup_field = 'uuid'


class WebHookViewSet(BaseHookViewSet):
    queryset = models.WebHook.objects.all()
    filterset_class = filters.WebHookFilter
    serializer_class = serializers.WebHookSerializer

    def create(self, request, *args, **kwargs):
        """
        To create new web hook issue **POST** against */api/hooks-web/* as an authenticated user.
        You should specify list of event_types or event_groups.

        Example of a request:

        .. code-block:: http

            POST /api/hooks-web/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "event_types": ["resource_start_succeeded"],
                "event_groups": ["users"],
                "destination_url": "http://example.com/"
            }

        When hook is activated, **POST** request is issued against destination URL with the following data:

        .. code-block:: javascript

            {
                "timestamp": "2015-07-14T12:12:56.000000",
                "message": "Customer ABC LLC has been updated.",
                "type": "customer_update_succeeded",
                "context": {
                    "user_native_name": "Walter Lebrowski",
                    "customer_contact_details": "",
                    "user_username": "Walter",
                    "user_uuid": "1c3323fc4ae44120b57ec40dea1be6e6",
                    "customer_uuid": "4633bbbb0b3a4b91bffc0e18f853de85",
                    "ip_address": "8.8.8.8",
                    "user_full_name": "Walter Lebrowski",
                    "customer_abbreviation": "ABC LLC",
                    "customer_name": "ABC LLC"
                },
                "levelname": "INFO"
            }

        Note that context depends on event type.
        """
        return super(WebHookViewSet, self).create(request, *args, **kwargs)


class EmailHookViewSet(BaseHookViewSet):
    queryset = models.EmailHook.objects.all()
    filterset_class = filters.EmailHookFilter
    serializer_class = serializers.EmailHookSerializer

    def create(self, request, *args, **kwargs):
        """
        To create new email hook issue **POST** against */api/hooks-email/* as an authenticated user.
        You should specify list of event_types or event_groups.

        Example of a request:

        .. code-block:: http

            POST /api/hooks-email/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "event_types": ["openstack_instance_start_succeeded"],
                "event_groups": ["users"],
                "email": "test@example.com"
            }

        You may temporarily disable hook without deleting it by issuing following **PATCH** request against hook URL:

        .. code-block:: javascript

            {
                "is_active": "false"
            }
        """
        return super(EmailHookViewSet, self).create(request, *args, **kwargs)


class PushHookViewSet(BaseHookViewSet):
    queryset = models.PushHook.objects.all()
    filterset_class = filters.PushHookFilter
    serializer_class = serializers.PushHookSerializer

    def create(self, request, *args, **kwargs):
        """
        To create new push hook issue **POST** against */api/hooks-push/* as an authenticated user.
        You should specify list of event_types or event_groups.

        Example of a request:

        .. code-block:: http

            POST /api/hooks-push/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "event_types": ["resource_start_succeeded"],
                "event_groups": ["users"],
                "type": "Android"
            }

        You may temporarily disable hook without deleting it by issuing following **PATCH** request against hook URL:

        .. code-block:: javascript

            {
                "is_active": "false"
            }
        """
        return super(PushHookViewSet, self).create(request, *args, **kwargs)


class HookSummary(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Use */api/hooks/* to get a list of all the hooks of any type that a user can see.
    """
    serializer_class = serializers.SummaryHookSerializer
    filter_backends = (core_filters.StaffOrUserFilter, filters.HookSummaryFilterBackend)

    def get_queryset(self):
        return SummaryQuerySet(models.BaseHook.get_all_models())
