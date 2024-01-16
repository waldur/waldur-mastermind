import logging


class Component:
    def __init__(
        self,
        type,
        name,
        measured_unit,
        billing_type,
        factor=1,
        description="",
        limit_period="",
    ):
        self.type = type
        self.name = name
        self.measured_unit = measured_unit
        self.billing_type = billing_type
        self.factor = factor
        self.description = description
        self.limit_period = limit_period

    def _asdict(self):
        # Note that factor is not serialized to dict because it is not stored in the database.
        # Currently, it is used only for cost estimation when order is created.
        return {
            "type": self.type,
            "name": self.name,
            "measured_unit": self.measured_unit,
            "billing_type": self.billing_type,
            "limit_period": self.limit_period,
        }


logger = logging.getLogger(__name__)


class PluginManager:
    def __init__(self):
        self.backends = {}

    def register(self, offering_type, **kwargs):
        """

        :param offering_type: string which consists of application name and model name,
                              for example Support.OfferingTemplate
        :key create_resource_processor: class which receives order
        :key update_resource_processor: class which receives order
        :key delete_resource_processor: class which receives order
        :key components: tuple available plan components, for example
                           Component(type='storage', name='Storage', measured_unit='GB')
        :key service_type: optional string indicates service type to be used
        :key can_terminate_order: optional boolean indicates whether order can be terminated
        :key secret_attributes: optional list of strings each of which corresponds to secret attribute key,
        for example, VPC username and password.
        :key available_limits: optional list of strings each of which corresponds to offering component type,
        which supports user-defined limits, such as VPC RAM and vCPU.
        :key limits_validator: optional function to validate limis.
        :key: can_update_limits: boolean which indicates whether plugin allows user to set limits on resource.
        :key resource_model: optional Django model class which corresponds to resource.
        :key get_filtered_components: optional function to filter out enabled offering components.
        :key change_attributes_for_view: optional function to change the display of attributes in a view. An attributes
        of offering do not change.
        :key enable_usage_notifications: optional boolean indicated whether usage notifications
        should be sent to a customer.
        :key enable_remote_support: optional boolean indicated whether offering can be imported in remote Waldur.
        :key get_importable_resources_backend_method:
        :key import_resource_backend_method:
        :key import_resource_executor:
        :key get_available_resource_actions: function which returns list of strings
        identifying available resource actions
        """
        self.backends[offering_type] = kwargs

    def get_offering_types(self):
        """
        Return list of offering types.
        """
        return self.backends.keys()

    def get_service_type(self, offering_type):
        """
        Return a service type for given offering_type.
        :param offering_type: offering type name
        :return: string or None
        """
        return self.backends.get(offering_type, {}).get("service_type")

    def get_components(self, offering_type: str) -> list[Component]:
        """
        Return a list of components for given offering_type.
        :param offering_type: offering type name
        :return: list of components
        """
        return self.backends.get(offering_type, {}).get("components") or []

    def get_component_types(self, offering_type):
        """
        Return a components types for given offering_type.
        :param offering_type: offering type name
        :return: set of component types
        """
        return {component.type for component in self.get_components(offering_type)}

    def can_cancel_order(self, offering_type):
        """
        Returns true if order can be terminated.
        """
        return self.backends.get(offering_type, {}).get("can_terminate_order") or False

    def get_secret_attributes(self, offering_type):
        """
        Returns list of secret attributes for given offering type.
        """
        secret_attributes = self.backends.get(offering_type, {}).get(
            "secret_attributes"
        )
        if callable(secret_attributes):
            secret_attributes = secret_attributes()
        return secret_attributes or []

    def get_available_limits(self, offering_type):
        """
        Returns list of offering component types which supports user-defined limits.
        """
        return self.backends.get(offering_type, {}).get("available_limits") or []

    def can_update_limits(self, offering_type):
        """
        Returns true if plugin allows user to set limits on resource.
        """
        return self.backends.get(offering_type, {}).get("can_update_limits", False)

    def get_limits_validator(self, offering_type):
        """
        Returns function to validate limis.
        """
        return self.backends.get(offering_type, {}).get("limits_validator")

    def get_resource_model(self, offering_type):
        """
        Returns Django model class which corresponds to resource.
        """
        processor = self.get_processor(offering_type, "create_resource_processor")

        if not processor:
            return

        if getattr(processor, "get_resource_model", None):
            resource_model = processor.get_resource_model()
        else:
            return

        return resource_model

    def get_importable_offering_types(self):
        return {
            offering_type
            for offering_type in self.get_offering_types()
            if self.get_importable_resources_backend_method(offering_type)
        }

    def get_importable_resources_backend_method(self, offering_type):
        return self.backends.get(offering_type, {}).get(
            "get_importable_resources_backend_method"
        )

    def import_resource_backend_method(self, offering_type):
        return self.backends.get(offering_type, {}).get(
            "import_resource_backend_method"
        )

    def get_import_resource_executor(self, offering_type):
        return self.backends.get(offering_type, {}).get("import_resource_executor")

    def get_processor(self, offering_type, processor_type):
        """
        Return a processor class for given offering type and order type.
        """
        return self.backends.get(offering_type, {}).get(processor_type)

    def get_change_attributes_for_view(self, offering_type):
        """
        Return a function for showing attributes.
        """
        return self.backends.get(offering_type, {}).get("change_attributes_for_view")

    def get_components_filter(self, offering_type):
        """
        Return a function for filtering offering components.
        This function is expected to receive offering and components queryset.
        It should return filtered components queryset as a result.
        """
        return self.backends.get(offering_type, {}).get("components_filter")

    def enable_usage_notifications(self, offering_type):
        return self.backends.get(offering_type, {}).get(
            "enable_usage_notifications", False
        )

    def enable_remote_support(self, offering_type):
        return self.backends.get(offering_type, {}).get("enable_remote_support", False)

    def can_manage_offering_components(self, offering_type):
        """
        Returns true if creating/deleting of offering components via api is available.
        """
        return self.backends.get(offering_type, {}).get(
            "can_manage_offering_components", True
        )

    def get_plan_fields_that_cannot_be_edited(self, offering_type):
        """
        Returns plan fields that cannot be edited via api.
        """
        return self.backends.get(offering_type, {}).get(
            "plan_fields_that_cannot_be_edited", []
        )

    def can_manage_plans(self, offering_type):
        """
        Returns true if creating/deleting of plans and plan components via api is available.
        """
        return self.backends.get(offering_type, {}).get("can_manage_plans", True)

    def get_available_resource_actions(self, resource):
        """
        Returns list of strings identifying available resource actions
        """
        actions = []
        for backend in self.backends.values():
            fn = backend.get("get_available_resource_actions")
            if fn:
                actions.extend(fn(resource))
        return actions


manager = PluginManager()
