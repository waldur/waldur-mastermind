import logging

from django.db import models as django_models
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.reverse import reverse

from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import models, signals
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.utils import validate_limits

logger = logging.getLogger(__name__)


def get_order_post_data(order, fields):
    if not order.offering.scope:
        raise serializers.ValidationError(
            "Offering is invalid: it does not have a scope."
        )
    project_url = reverse("project-detail", kwargs={"uuid": order.project.uuid})
    service_settings_url = reverse(
        "servicesettings-detail", kwargs={"uuid": order.offering.scope.uuid}
    )
    return dict(
        service_settings=service_settings_url,
        project=project_url,
        **copy_attributes(fields, order),
    )


def copy_attributes(fields, order):
    """
    Copy valid attributes from order.
    """
    payload = dict()
    for field in fields:
        if field in order.attributes:
            payload[field] = order.attributes.get(field)
    return payload


class BaseOrderProcessor:
    def __init__(self, order):
        self.order: models.Order = order

    def process_order(self, user):
        """
        This method is called after order has been approved.
        """
        raise NotImplementedError()

    def validate_order(self, request):
        """
        This method receives request object, and raises
        validation error if provided order is invalid.
        It is called after order has been created but before it is submitted.
        """
        raise NotImplementedError()


class AbstractCreateResourceProcessor(BaseOrderProcessor):
    def process_order(self, user):
        # scope can be a reference to a different object or a string representing
        # unique key of a scoped object, e.g. remote UUID
        resource = self.order.resource
        resource.options = {}

        for resource_option in self.order.offering.resource_options.get(
            "options", {}
        ).keys():
            if resource_option in self.order.attributes:
                resource.options[resource_option] = self.order.attributes[
                    resource_option
                ]

        resource.save(update_fields=["options"])

        scope = self.send_request(user)
        backend_metadata = {}
        endpoints = []
        if isinstance(scope, dict) and scope["response_type"] == "metadata":
            backend_metadata = scope.get("backend_metadata") or backend_metadata
            endpoints = scope.get("endpoints") or endpoints
            scope = scope["backend_id"]

        with transaction.atomic():
            resource.backend_metadata = backend_metadata
            if isinstance(scope, str):
                resource.backend_id = scope
            elif isinstance(scope, django_models.Model):
                resource.scope = scope
            resource.save()
            for endpoint in endpoints:
                name = endpoint.get("name")
                url = endpoint.get("url")
                if name is not None and url is not None:
                    models.ResourceAccessEndpoint.objects.create(
                        name=name, url=url, resource=resource
                    )

            if not scope or isinstance(scope, str):
                resource_creation_succeeded(resource)

    def send_request(self, user):
        """
        This method should send request to backend.
        """
        raise NotImplementedError


class CreateResourceProcessor(AbstractCreateResourceProcessor):
    """
    This class implements order processing using internal API requests.

    Order validation flow looks as following:
    1) Convert order to HTTP POST request data expected by DRF serializer.
    2) Pass request data to serializer and check if data is valid.

    Order processing flow looks as following:
    1) Convert order to HTTP POST request data expected by DRF serializer.
    2) Issue internal API request to DRF viewset.
    3) Extract Django model for created resource from HTTP response.
    4) Create marketplace resource object from order and plugin resource.
    5) Store link from order to the resource.

    Therefore this class implements template method design pattern.
    """

    def validate_order(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        if serializer_class:
            context = {"request": request, "skip_permission_check": True}
            serializer = serializer_class(data=post_data, context=context)
            serializer.is_valid(raise_exception=True)

    def send_request(self, user):
        post_data = self.get_post_data()
        view = self.get_viewset().as_view({"post": "create"})
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
        This method converts order to request data expected by DRF serializer.
        """
        raise NotImplementedError

    def get_scope_from_response(self, response):
        """
        This method extracts Django model from response returned by DRF viewset.
        """
        raise NotImplementedError


class AbstractUpdateResourceProcessor(BaseOrderProcessor):
    def is_update_limit_order(self):
        if "old_limits" in self.order.attributes.keys():
            return True

    def validate_order(self, request):
        if self.is_update_limit_order():
            validate_limits(
                self.order.limits,
                self.order.offering,
                self.order.resource,
            )
            return

        self.validate_request(request)

    def validate_request(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        if serializer_class:
            context = {"request": request, "skip_permission_check": True}
            serializer = serializer_class(data=post_data, context=context)
            serializer.is_valid(raise_exception=True)

    def process_order(self, user):
        """We need to overwrite process order because two cases exist:
        a switch of a plan and a change of limits."""
        if self.is_update_limit_order():
            try:
                # self.update_limits_process method can execute not is_async
                # because in this case an order has got only one order.
                done = self.update_limits_process(user)
            except Exception as e:
                signals.resource_limit_update_failed.send(
                    sender=self.order.resource.__class__,
                    order=self.order,
                    error_message=str(e) or str(type(e)),
                )
                return
            if done:
                signals.resource_limit_update_succeeded.send(
                    sender=self.order.resource.__class__,
                    order=self.order,
                )
            else:
                with transaction.atomic():
                    self.order.resource.set_state_updating()
                    self.order.resource.save(update_fields=["state"])
            return

        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError("Resource is not found.")
        done = self.send_request(user)

        if done:
            with transaction.atomic():
                # check if a new plan has been requested
                if self.order.resource.plan != self.order.plan:
                    logger.info(
                        f"Changing plan of a resource {self.order.resource.name} "
                        "from {self.order.resource.plan} to {self.order.plan}. "
                        "Order ID: {self.order.id}"
                    )
                    self.order.resource.plan = self.order.plan
                    self.order.resource.save(update_fields=["plan"])

                self.order.state = models.Order.States.DONE
                self.order.save(update_fields=["state"])
        else:
            with transaction.atomic():
                self.order.resource.set_state_updating()
                self.order.resource.save(update_fields=["state"])

    def send_request(self, user, resource):
        """
        This method should send request to backend.
        """
        raise NotImplementedError

    def get_resource(self):
        """
        This method should return related resource of order.
        """
        return self.order.resource

    def update_limits_process(self, user):
        """
        This method implements limits update processing.
        It should return True if sync operation has been successfully completed
        and return False or None if async operation has been scheduled.
        """
        raise NotImplementedError


class UpdateScopedResourceProcessor(AbstractUpdateResourceProcessor):
    def get_resource(self):
        return self.order.resource.scope

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
        This method converts order to request data expected by DRF serializer.
        """
        raise NotImplementedError


class AbstractDeleteResourceProcessor(BaseOrderProcessor):
    def validate_order(self, request):
        pass

    def get_resource(self):
        """
        This method should return related resource of order.
        """
        return self.order.resource

    def send_request(self, user, resource):
        """
        This method should send request to backend.
        """
        raise NotImplementedError

    def process_order(self, user):
        resource = self.get_resource()
        if not resource:
            done = True
        else:
            done = self.send_request(user, resource)

        if done:
            with transaction.atomic():
                self.order.resource.set_state_terminated()
                self.order.resource.save(update_fields=["state"])

                self.order.state = models.Order.States.DONE
                self.order.save(update_fields=["state"])
        else:
            with transaction.atomic():
                self.order.resource.set_state_terminating()
                self.order.resource.save(update_fields=["state"])


class DeleteScopedResourceProcessor(AbstractDeleteResourceProcessor):
    viewset = NotImplementedError

    def get_resource(self):
        return self.order.resource.scope

    def validate_order(self, request):
        action = self._get_action()
        resource = self.get_resource()
        if resource:
            self.get_viewset()().validate_object_action(action, resource)

    def send_request(self, user, resource):
        delete_attributes = self.order.attributes
        action = self._get_action()
        view = self.get_viewset().as_view({"delete": action})
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

    def _get_action(self):
        delete_attributes = self.order.attributes
        action = delete_attributes.get("action", "destroy")
        return action if hasattr(self.get_viewset(), action) else "destroy"


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
        Get list of valid attribute names to be copied from order to
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
        return getattr(viewset, "create_serializer_class", None) or getattr(
            viewset, "serializer_class"
        )

    def get_post_data(self):
        return get_order_post_data(self.order, self.get_fields())

    def get_scope_from_response(self, response):
        return self.get_resource_model().objects.get(uuid=response.data["uuid"])


class BasicCreateResourceProcessor(AbstractCreateResourceProcessor):
    def send_request(self, user):
        pass

    def validate_order(self, request):
        pass


class BasicDeleteResourceProcessor(AbstractDeleteResourceProcessor):
    def send_request(self, user, resource):
        return True


class BasicUpdateResourceProcessor(AbstractUpdateResourceProcessor):
    def send_request(self, user):
        return True

    def validate_request(self, request):
        pass

    def update_limits_process(self, user):
        return True
