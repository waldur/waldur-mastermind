import logging

from django.db import models as django_models
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, status
from rest_framework.reverse import reverse

from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import models, signals
from waldur_mastermind.marketplace.callbacks import (
    resource_creation_succeeded,
    resource_update_failed,
    resource_update_succeeded,
)
from waldur_mastermind.marketplace.utils import (
    parse_date,
    validate_limits,
)

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
    def __init__(self, order: models.Order):
        self.order = order

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

            if (
                not scope
                or isinstance(scope, str)
                or isinstance(scope, models.Resource)  # Managed Rancher Cluster case
            ):
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

    Therefore, this class implements template method design pattern.
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
        """Checks if the order is for updating resource limits."""
        return "old_limits" in self.order.attributes

    def is_renewal_order(self):
        """Checks if the order is for renewing a prepaid resource."""
        return self.order.attributes.get("action") == "renew"

    def is_update_options_order(self):
        """Checks if the order is for updating resource options."""
        return "new_options" in self.order.attributes

    def validate_order(self, request):
        if self.is_update_limit_order() or self.is_renewal_order():
            # For both limit updates and renewals, we must validate the final limits.
            validate_limits(
                self.order.limits,
                self.order.offering,
                self.order.resource,
            )
            return

        # Fallback for plan switch and other generic updates
        self.validate_request(request)

    def validate_request(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        if serializer_class:
            context = {"request": request, "skip_permission_check": True}
            serializer = serializer_class(data=post_data, context=context)
            serializer.is_valid(raise_exception=True)

    def process_order(self, user):
        """
        Process an UPDATE order, dispatching to the correct handler based on attributes.
        Handles:
        1. Prepaid resource renewals (end_date + optional limit changes).
        2. Standard resource limit updates.
        3. Plan switches.
        """
        # Renewal is a specific type of limit update, so check for it first.
        if self.is_renewal_order():
            self._process_renewal_or_limit_update(user, is_renewal=True)
        elif self.is_update_limit_order():
            self._process_renewal_or_limit_update(user, is_renewal=False)
        elif self.is_update_options_order():
            self._process_options_update(user)
        else:
            # Fallback to the original plan switch logic
            self._process_plan_switch(user)

    def _process_renewal_or_limit_update(self, user, is_renewal=False):
        """
        Unified handler for processing both renewals and limit updates.
        """
        # Validate renewal duration constraints from the offering component
        if is_renewal:
            raw_extension_months = self.order.attributes.get("extension_months")
            if raw_extension_months is not None:
                try:
                    extension_months = int(raw_extension_months)
                except (TypeError, ValueError):
                    extension_months = None
            else:
                extension_months = None
            if extension_months is not None:
                resource = self.order.resource
                for component in resource.offering.components.filter(is_prepaid=True):
                    min_dur = component.min_renewal_duration
                    max_dur = component.max_renewal_duration
                    step = component.renewal_duration_step or 1

                    if min_dur is not None and extension_months < min_dur:
                        signals.resource_limit_update_failed.send(
                            sender=resource.__class__,
                            order=self.order,
                            error_message=_(
                                "Renewal of {d} months is less than the minimum "
                                "of {min} months for component '{name}'."
                            ).format(
                                d=extension_months,
                                min=min_dur,
                                name=component.name,
                            ),
                        )
                        return

                    if max_dur is not None and extension_months > max_dur:
                        signals.resource_limit_update_failed.send(
                            sender=resource.__class__,
                            order=self.order,
                            error_message=_(
                                "Renewal of {d} months exceeds the maximum "
                                "of {max} months for component '{name}'."
                            ).format(
                                d=extension_months,
                                max=max_dur,
                                name=component.name,
                            ),
                        )
                        return

                    if step > 1:
                        base = min_dur or 0
                        if (extension_months - base) % step != 0:
                            signals.resource_limit_update_failed.send(
                                sender=resource.__class__,
                                order=self.order,
                                error_message=_(
                                    "Renewal of {d} months is not a valid step for "
                                    "component '{name}' (step={step}, base={base})."
                                ).format(
                                    d=extension_months,
                                    name=component.name,
                                    step=step,
                                    base=base,
                                ),
                            )
                            return

        try:
            # The underlying `update_limits_process` method in the plugin-specific
            # processor will handle the backend call. It might need to be
            # aware of the renewal context for specific plugins.
            done = self.update_limits_process(user)
        except Exception as e:
            # Use the correct signal for limit update failures
            signals.resource_limit_update_failed.send(
                sender=self.order.resource.__class__,
                order=self.order,
                error_message=str(e) or str(type(e)),
            )
            return

        if done:
            # If the backend update is synchronous and successful, we can
            # immediately apply the changes and send the success signal.

            # Commit changes to the resource
            with transaction.atomic():
                resource = self.order.resource

                # Update limits for both renewals and limit updates
                resource.limits = self.order.limits

                if is_renewal:
                    # For renewals, also update end_date and history
                    new_end_date_str = self.order.attributes.get("new_end_date")
                    if new_end_date_str:
                        resource.end_date = parse_date(new_end_date_str)
                        resource.end_date_requested_by = user

                    # Append to renewal history
                    history = resource.attributes.get("renewal_history", [])
                    history.append(
                        {
                            "date": timezone.now().isoformat(),
                            "type": "renewal",
                            "order_uuid": self.order.uuid.hex,
                            "old_limits": self.order.attributes.get("old_limits"),
                            "new_limits": resource.limits,
                            "old_end_date": self.order.attributes.get("old_end_date"),
                            "new_end_date": new_end_date_str,
                            "cost": float(self.order.cost)
                            if self.order.cost is not None
                            else None,
                        }
                    )
                    resource.attributes["renewal_history"] = history

                resource.save()

            signals.resource_limit_update_succeeded.send(
                sender=self.order.resource.__class__,
                order=self.order,
            )

    def _process_plan_switch(self, user):
        """
        Original logic for handling plan switches.
        """
        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError("Resource is not found.")

        # This method in the subclass will call the backend
        done = self.send_request(user)

        if done:
            try:
                resource_update_succeeded(self.order.resource)
            except Exception as e:
                self.order.error_message = str(e)
                self.order.save(update_fields=["error_message"])
                resource_update_failed(self.order.resource)
                logger.error(
                    f"Error switching plan for resource {self.order.resource.name}. "
                    f"Order ID: {self.order.pk}. "
                    f"Exception: {e}."
                )

    def _process_options_update(self, user):
        """
        Process an UPDATE order for resource options.

        Options handling is delegated to resource_update_succeeded which:
        - Locks the resource with select_for_update() for atomic updates
        - Updates options from order.attributes["new_options"]
        - Sets resource state to OK
        - Marks order as DONE
        """
        try:
            resource_update_succeeded(self.order.resource)
        except Exception as e:
            # Set error message on order before calling callback
            self.order.error_message = str(e)
            self.order.save(update_fields=["error_message"])

            # Use the standard callback for failed UPDATE orders
            resource_update_failed(self.order.resource)

            logger.error(
                f"Error updating options for resource {self.order.resource.name}. "
                f"Order ID: {self.order.pk}. "
                f"Exception: {str(e)}."
            )

    def send_request(self, user):
        """This method should send request to backend for plan switches."""
        raise NotImplementedError

    def get_resource(self):
        """This method should return related resource of order."""
        return self.order.resource

    def update_limits_process(self, user):
        """
        This method implements limits update processing for both standard
        limit changes and renewals. It should return True if the sync operation
        has been successfully completed and return False or None if an async
        operation has been scheduled.
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

                self.order.complete()
                self.order.save(update_fields=["state"])
        else:
            pass


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
    def send_request(self, user, resource) -> bool:
        return True


class BasicUpdateResourceProcessor(AbstractUpdateResourceProcessor):
    def send_request(self, user) -> bool:
        return True

    def validate_request(self, request):
        pass

    def update_limits_process(self, user) -> bool:
        return True
