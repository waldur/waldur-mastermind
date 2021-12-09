import logging

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.reverse import reverse

from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import models, signals
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.utils import create_local_resource, validate_limits

logger = logging.getLogger(__name__)


def get_order_item_post_data(order_item, fields):
    if not order_item.offering.scope:
        raise serializers.ValidationError(
            'Offering is invalid: it does not have a scope.'
        )
    project_url = reverse(
        'project-detail', kwargs={'uuid': order_item.order.project.uuid}
    )
    service_settings_url = reverse(
        'servicesettings-detail', kwargs={'uuid': order_item.offering.scope.uuid}
    )
    return dict(
        service_settings=service_settings_url,
        project=project_url,
        **copy_attributes(fields, order_item),
    )


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
        # scope can be a reference to a different object or a string representing
        # unique key of a scoped object, e.g. remote UUID
        scope = self.send_request(user)

        with transaction.atomic():
            resource = create_local_resource(self.order_item, scope)

            if not scope or type(scope) == str:
                resource_creation_succeeded(resource)

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
        if serializer_class:
            context = {'request': request, 'skip_permission_check': True}
            serializer = serializer_class(data=post_data, context=context)
            serializer.is_valid(raise_exception=True)

    def send_request(self, user):
        post_data = self.get_post_data()
        view = self.get_viewset().as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)
        if response.status_code != status.HTTP_201_CREATED:
            raise serializers.ValidationError(response.data)

        if response.data:
            return self.get_scope_from_response(response)

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to provision new resources.
        """
        return None

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

        self.validate_request(request)

    def validate_request(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        if serializer_class:
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
                done = self.update_limits_process(user)
            except Exception as e:
                signals.resource_limit_update_failed.send(
                    sender=self.order_item.resource.__class__,
                    order_item=self.order_item,
                    error_message=str(e) or str(type(e)),
                )
                return
            if done:
                signals.resource_limit_update_succeeded.send(
                    sender=self.order_item.resource.__class__,
                    order_item=self.order_item,
                )
            else:
                with transaction.atomic():
                    self.order_item.resource.set_state_updating()
                    self.order_item.resource.save(update_fields=['state'])
            return

        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError('Resource is not found.')
        done = self.send_request(user)

        if done:
            with transaction.atomic():
                # check if a new plan has been requested
                if resource.plan != self.order_item.plan:
                    logger.info(
                        f'Changing plan of a resource {resource.name} from {resource.plan} to {self.order_item.plan}. Order item ID: {self.order_item.id}'
                    )
                    resource.plan = self.order_item.plan
                    resource.save(update_fields=['plan'])

                self.order_item.state = models.OrderItem.States.DONE
                self.order_item.save(update_fields=['state'])
        else:
            with transaction.atomic():
                self.order_item.resource.set_state_updating()
                self.order_item.resource.save(update_fields=['state'])

    def send_request(self, user, resource):
        """
        This method should send request to backend.
        """
        raise NotImplementedError

    def get_resource(self):
        """
        This method should return related resource of order item.
        """
        return self.order_item.resource

    def update_limits_process(self, user):
        """
        This method implements limits update processing.
        It should return True if sync operation has been successfully completed
        and return False or None if async operation has been scheduled.
        """
        raise NotImplementedError


class UpdateScopedResourceProcessor(AbstractUpdateResourceProcessor):
    def get_resource(self):
        return self.order_item.resource.scope

    def send_request(self, user):
        view = self.get_view()
        payload = self.get_post_data()
        response = common_utils.create_request(view, user, payload)
        if response.status_code != status.HTTP_202_ACCEPTED:
            raise serializers.ValidationError(response.data)

        # we expect all children to implement async update process, which will set state of resource back to OK
        return False

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to update existing resource.
        """
        return None

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
        return self.order_item.resource

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


class DeleteScopedResourceProcessor(AbstractDeleteResourceProcessor):
    viewset = NotImplementedError

    def get_resource(self):
        return self.order_item.resource.scope

    def send_request(self, user, resource):
        view = self.get_viewset().as_view({'delete': 'destroy'})
        delete_attributes = self.order_item.attributes
        # Delete resource processor operates with scoped resources

        response = common_utils.delete_request(
            view, user, uuid=resource.uuid.hex, query_params=delete_attributes
        )
        if response.status_code not in (
            status.HTTP_204_NO_CONTENT,
            status.HTTP_202_ACCEPTED,
        ):
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
    """

    viewset = NotImplementedError
    fields = NotImplementedError

    @classmethod
    def get_viewset(cls):
        return cls.viewset

    def get_fields(self):
        """
        Get list of valid attribute names to be copied from order item to
        resource provisioning request.
        """
        return self.fields

    @classmethod
    def get_resource_model(cls):
        """
        Get resource model used by viewset from its queryset.
        """
        return cls.get_viewset().queryset.model

    def get_serializer_class(self):
        """
        Use create_serializer_class if it is defined. Otherwise fallback to standard serializer class.
        """
        viewset = self.get_viewset()
        return getattr(viewset, 'create_serializer_class', None) or getattr(
            viewset, 'serializer_class'
        )

    def get_post_data(self):
        return get_order_item_post_data(self.order_item, self.get_fields())

    def get_scope_from_response(self, response):
        return self.get_resource_model().objects.get(uuid=response.data['uuid'])


class BasicCreateResourceProcessor(AbstractCreateResourceProcessor):
    def process_order_item(self, user):
        with transaction.atomic():
            create_local_resource(self.order_item, None)

    def send_request(self, user):
        pass

    def validate_order_item(self, request):
        pass


class BasicDeleteResourceProcessor(AbstractDeleteResourceProcessor):
    def send_request(self, user, resource):
        return False


class BasicUpdateResourceProcessor(AbstractUpdateResourceProcessor):
    def send_request(self, user):
        return False

    def validate_request(self, request):
        pass

    def update_limits_process(self, user):
        return False
