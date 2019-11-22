import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework import status, serializers
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models, SupportedServices
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import models, signals
from waldur_mastermind.marketplace.utils import validate_limits

logger = logging.getLogger(__name__)


def get_spl_url(spl_model_class, order_item):
    """
    Find service project link URL for specific service settings and marketplace order.
    """
    service_settings = order_item.offering.scope

    service_settings_type = spl_model_class._meta.app_config.service_name

    if not isinstance(service_settings, structure_models.ServiceSettings) or \
            service_settings.type != service_settings_type:
        raise serializers.ValidationError('Offering has invalid scope. Service settings object is expected.')

    project = order_item.order.project

    try:
        spl = spl_model_class.objects.get(
            project=project,
            service__settings=service_settings,
            service__customer=project.customer,
        )
        return reverse('{}-detail'.format(spl.get_url_name()), kwargs={'pk': spl.pk})
    except ObjectDoesNotExist:
        raise serializers.ValidationError('Project does not have access to the service.')


def copy_attributes(fields, order_item):
    """
    Copy valid attributes from order item.
    """
    payload = dict()
    for field in fields:
        if field in order_item.attributes:
            payload[field] = order_item.attributes.get(field)
    return payload


class BaseOrderItemProcessor:
    def __init__(self, order_item):
        self.order_item = order_item

    def process_order_item(self, user):
        """
        This method receives user object and creates plugin's resource corresponding
        to provided order item. It is called after order has been approved.
        """
        raise NotImplementedError()

    def validate_order_item(self, request):
        """
        This method receives request object, and raises
        validation error if provided order item is invalid.
        It is called after order has been created but before it is submitted.
        """
        raise NotImplementedError()


class AbstractCreateResourceProcessor(BaseOrderItemProcessor):
    def process_order_item(self, user):
        scope = self.send_request(user)

        with transaction.atomic():
            resource = models.Resource(
                project=self.order_item.order.project,
                offering=self.order_item.offering,
                plan=self.order_item.plan,
                limits=self.order_item.limits,
                attributes=self.order_item.attributes,
                name=self.order_item.attributes.get('name') or '',
                scope=scope,
            )
            resource.init_cost()
            resource.save()
            resource.init_quotas()
            self.order_item.resource = resource
            self.order_item.save(update_fields=['resource'])

    def send_request(self, user):
        """
        This method should send request to backend.
        """
        raise NotImplementedError


class CreateResourceProcessor(AbstractCreateResourceProcessor):
    """
    This class implements order processing using internal API requests.

    Order item validation flow looks as following:
    1) Convert order item to HTTP POST request data expected by DRF serializer.
    2) Pass request data to serializer and check if data is valid.

    Order item processing flow looks as following:
    1) Convert order item to HTTP POST request data expected by DRF serializer.
    2) Issue internal API request to DRF viewset.
    3) Extract Django model for created resource from HTTP response.
    4) Create marketplace resource object from order item and plugin resource.
    5) Store link from order item to the resource.

    Therefore this class implements template method design pattern.
    """

    def validate_order_item(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        context = {'request': request, 'skip_permission_check': True}
        serializer = serializer_class(data=post_data, context=context)
        serializer.is_valid(raise_exception=True)

    def send_request(self, user):
        post_data = self.get_post_data()
        view = self.get_viewset().as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)
        if response.status_code != status.HTTP_201_CREATED:
            raise serializers.ValidationError(response.data)

        return self.get_scope_from_response(response)

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to provision new resources.
        """
        raise NotImplementedError

    def get_viewset(self):
        """
        This method should return DRF viewset class which
        processes request to provision new resources.
        """
        raise NotImplementedError

    def get_post_data(self):
        """
        This method converts order item to request data expected by DRF serializer.
        """
        raise NotImplementedError

    def get_scope_from_response(self, response):
        """
        This method extracts Django model from response returned by DRF viewset.
        """
        raise NotImplementedError


class AbstractUpdateResourceProcessor(BaseOrderItemProcessor):
    def is_update_limit_order_item(self):
        if 'old_limits' in self.order_item.attributes.keys():
            return True

    def validate_order_item(self, request):
        if self.is_update_limit_order_item():
            validate_limits(self.order_item.limits, self.order_item.offering)
            return

        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        context = {'request': request, 'skip_permission_check': True}
        serializer = serializer_class(data=post_data, context=context)
        serializer.is_valid(raise_exception=True)

    def process_order_item(self, user):
        """We need to overwrite process order item because two cases exist:
        a switch of a plan and a change of limits."""
        if self.is_update_limit_order_item():
            try:
                # self.update_limits_process method can execute not is_async
                # because in this case an order has got only one order item.
                self.update_limits_process(user)
            except NotImplementedError:
                self.order_item.set_state_erred()
                self.order_item.save(update_fields=['state'])
                logger.warning('An update of limits has been called. '
                               'But update limits process for the plugin has not been implemented. '
                               'Order item ID: %s, Plugin: %s.',
                               self.order_item.id, self.order_item.offering.type)
            except Exception as e:
                signals.limit_update_failed.send(
                    sender=self.order_item.resource.__class__,
                    order_item=self.order_item,
                    error_message=str(e),
                )
            else:
                signals.limit_update_succeeded.send(
                    sender=self.order_item.resource.__class__,
                    order_item=self.order_item,
                )
            return

        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError('Resource is not found.')

        self.send_request(user)
        self.order_item.resource.set_state_updating()
        self.order_item.resource.save(update_fields=['state'])

    def send_request(self, user):
        """
        This method should send request to backend.
        """
        raise NotImplementedError

    def get_resource(self):
        """
        This method should return related resource of order item.
        """
        return self.order_item.resource.scope

    def update_limits_process(self, user):
        """
        This method implements limits update processing.
        """
        raise NotImplementedError


class UpdateResourceProcessor(AbstractUpdateResourceProcessor):
    def send_request(self, user):
        view = self.get_view()
        payload = self.get_post_data()
        response = common_utils.create_request(view, user, payload)
        if response.status_code != status.HTTP_202_ACCEPTED:
            raise serializers.ValidationError(response.data)

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to update existing resource.
        """
        raise NotImplementedError

    def get_view(self):
        """
        This method should return DRF viewset class which
        processes request to change existing resource.
        """
        raise NotImplementedError

    def get_post_data(self):
        """
        This method converts order item to request data expected by DRF serializer.
        """
        raise NotImplementedError


class AbstractDeleteResourceProcessor(BaseOrderItemProcessor):
    def validate_order_item(self, request):
        pass

    def get_resource(self):
        """
        This method should return related resource of order item.
        """
        return self.order_item.resource.scope

    def send_request(self, user, resource):
        """
        This method should send request to backend.
        """
        raise NotImplementedError

    def process_order_item(self, user):
        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError('Resource is not found.')

        done = self.send_request(user, resource)

        if done:
            with transaction.atomic():
                self.order_item.resource.set_state_terminated()
                self.order_item.resource.save(update_fields=['state'])

                self.order_item.state = models.OrderItem.States.DONE
                self.order_item.save(update_fields=['state'])
        else:
            with transaction.atomic():
                self.order_item.resource.set_state_terminating()
                self.order_item.resource.save(update_fields=['state'])


class DeleteResourceProcessor(AbstractDeleteResourceProcessor):
    viewset = NotImplementedError

    def send_request(self, user, resource):
        view = self.get_viewset().as_view({'delete': 'destroy'})
        delete_attributes = self.order_item.attributes
        response = common_utils.delete_request(view, user, uuid=resource.uuid.hex, query_params=delete_attributes)
        if response.status_code not in (status.HTTP_204_NO_CONTENT, status.HTTP_202_ACCEPTED):
            raise serializers.ValidationError(response.data)
        return response.status_code == status.HTTP_204_NO_CONTENT

    def get_viewset(self):
        """
        This method should return DRF viewset class which
        processes request to delete existing resource.
        """
        return self.viewset


class BaseCreateResourceProcessor(CreateResourceProcessor):
    """
    Abstract base class to adapt resource provisioning endpoints to marketplace API.
    It is assumed that resource model and serializer uses service project link.
    """
    viewset = NotImplementedError
    fields = NotImplementedError

    def get_viewset(self):
        return self.viewset

    def get_fields(self):
        """
        Get list of valid attribute names to be copied from order item to
        resource provisioning request.
        """
        return self.fields

    def get_resource_model(self):
        """
        Get resource model used by viewset from its queryset.
        """
        return self.get_viewset().queryset.model

    def get_spl_model(self):
        """
        Get service project link model used by resource model using service registry.
        """
        return SupportedServices.get_related_models(self.get_resource_model())['service_project_link']

    def get_serializer_class(self):
        """
        Use create_serializer_class if it is defined. Otherwise fallback to standard serializer class.
        """
        viewset = self.get_viewset()
        return getattr(viewset, 'create_serializer_class', None) or getattr(viewset, 'serializer_class')

    def get_post_data(self):
        order_item = self.order_item
        return dict(
            service_project_link=get_spl_url(self.get_spl_model(), order_item),
            **copy_attributes(self.get_fields(), order_item)
        )

    def get_scope_from_response(self, response):
        return self.get_resource_model().objects.get(uuid=response.data['uuid'])
