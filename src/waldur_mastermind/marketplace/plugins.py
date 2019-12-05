import logging

from waldur_core.structure import SupportedServices


class Component:
    def __init__(self, type, name, measured_unit, billing_type, factor=1):
        self.type = type
        self.name = name
        self.measured_unit = measured_unit
        self.billing_type = billing_type
        self.factor = factor

    def _asdict(self):
        # Note that factor is not serialized to dict because it is not stored in the database.
        # Currently it is used only for cost estimation when order item is created.
        return {
            'type': self.type,
            'name': self.name,
            'measured_unit': self.measured_unit,
            'billing_type': self.billing_type,
        }


logger = logging.getLogger(__name__)


class PluginManager:
    def __init__(self):
        self.backends = {}

    def register(self, offering_type, **kwargs):
        """

        :param offering_type: string which consists of application name and model name,
                              for example Support.OfferingTemplate
        :key create_resource_processor: class which receives order item
        :key update_resource_processor: class which receives order item
        :key delete_resource_processor: class which receives order item
        :key components: tuple available plan components, for example
                           Component(type='storage', name='Storage', measured_unit='GB')
        :key service_type: optional string indicates service type to be used
        :key can_terminate_order_item: optional boolean indicates whether order item can be terminated
        :key secret_attributes: optional list of strings each of which corresponds to secret attribute key,
        for example, VPC username and password.
        :key available_limits: optional list of strings each of which corresponds to offering component type,
        which supports user-defined limits, such as VPC RAM and vCPU.
        :key resource_model: optional Django model class which corresponds to resource.
        :key get_filtered_components: optional function to filter out enabled offering components.
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
        return self.backends.get(offering_type, {}).get('service_type')

    def get_components(self, offering_type):
        """
        Return a list of components for given offering_type.
        :param offering_type: offering type name
        :return: list of components
        """
        return self.backends.get(offering_type, {}).get('components') or []

    def get_component_types(self, offering_type):
        """
        Return a components types for given offering_type.
        :param offering_type: offering type name
        :return: set of component types
        """
        return {component.type for component in self.get_components(offering_type)}

    def can_terminate_order_item(self, offering_type):
        """
        Returns true if order item can be terminated.
        """
        return self.backends.get(offering_type, {}).get('can_terminate_order_item') or False

    def get_secret_attributes(self, offering_type):
        """
        Returns list of secret attributes for given offering type.
        """
        secret_attributes = self.backends.get(offering_type, {}).get('secret_attributes')
        if callable(secret_attributes):
            secret_attributes = secret_attributes()
        return secret_attributes or []

    def get_available_limits(self, offering_type):
        """
        Returns list of offering component types which supports user-defined limits.
        """
        return self.backends.get(offering_type, {}).get('available_limits') or []

    def get_resource_model(self, offering_type):
        """
        Returns Django model class which corresponds to resource.
        """
        return self.backends.get(offering_type, {}).get('resource_model')

    def get_resource_viewset(self, offering_type):
        resource_model = self.get_resource_model(offering_type)
        return SupportedServices.get_resource_view(resource_model)

    def get_spl_model(self, offering_type):
        resource_model = self.get_resource_model(offering_type)
        return SupportedServices.get_related_models(resource_model)['service_project_link']

    def get_service_model(self, offering_type):
        resource_model = self.get_resource_model(offering_type)
        return SupportedServices.get_related_models(resource_model)['service']

    def get_importable_resources(self, offering):
        try:
            resource_viewset = self.get_resource_viewset(offering.type)
        except AttributeError:
            return []
        backend = offering.scope.get_backend()
        return getattr(backend, resource_viewset.importable_resources_backend_method)()

    def get_processor(self, offering_type, processor_type):
        """
        Return a processor class for given offering type and order item type.
        """
        return self.backends.get(offering_type, {}).get(processor_type)

    def get_filtered_components(self, offering):
        hook = self.backends.get(offering.type, {}).get('get_filtered_components')
        if hook:
            return hook(offering)
        else:
            return offering.components


manager = PluginManager()
