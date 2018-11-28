import collections
import logging

from django.utils import six
from rest_framework import exceptions

from . import models


Component = collections.namedtuple('Component', ('type', 'name', 'measured_unit', 'billing_type'))
logger = logging.getLogger(__name__)


class PluginManager(object):
    def __init__(self):
        self.backends = {}

    def register(self, offering_type,
                 create_resource_processor,
                 update_resource_processor=None,
                 delete_resource_processor=None,
                 components=None,
                 scope_model=None):
        """

        :param offering_type: string which consists of application name and model name,
                              for example Support.OfferingTemplate
        :param create_resource_processor: class which receives order item
        :param update_resource_processor: class which receives order item
        :param delete_resource_processor: class which receives order item
        :param components: tuple available plan components, for example
                           Component(type='storage', name='Storage', measured_unit='GB')
        :param scope_model: available model for an offering scope field
        :return:
        """
        self.backends[offering_type] = {
            'create_resource_processor': create_resource_processor,
            'update_resource_processor': update_resource_processor,
            'delete_resource_processor': delete_resource_processor,
            'components': components,
            'scope_model': scope_model,
        }

    def get_offering_types(self):
        """
        Return list of offering types.
        """
        return self.backends.keys()

    def get_processor(self, order_item):
        """
        Return a processor class for given order item.
        """
        if order_item.resource:
            offering = order_item.resource.offering
        else:
            offering = order_item.offering
        backend = self.backends.get(offering.type, {})

        if order_item.type == models.RequestTypeMixin.Types.CREATE:
            return backend.get('create_resource_processor')

        elif order_item.type == models.RequestTypeMixin.Types.UPDATE:
            return backend.get('update_resource_processor')

        elif order_item.type == models.RequestTypeMixin.Types.TERMINATE:
            return backend.get('delete_resource_processor')

    def get_scope_model(self, offering_type):
        """
        Return a scope model class for given offering_type.
        :param offering_type: offering type name
        :return: scope model class
        """
        return self.backends.get(offering_type, {}).get('scope_model')

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

    def get_scope_models(self):
        return {b['scope_model'] for b in self.backends.values() if b['scope_model']}

    def process(self, order_item, user):
        processor = self.get_processor(order_item)
        if not processor:
            order_item.error_message = 'Skipping order item processing because processor is not found.'
            order_item.set_state_erred()
            order_item.save(update_fields=['state', 'error_message'])
            return

        try:
            processor(order_item).process_order_item(user)
        except exceptions.APIException as e:
            order_item.error_message = six.text_type(e)
            order_item.set_state_erred()
            order_item.save(update_fields=['state', 'error_message'])
        else:
            if order_item.state != models.OrderItem.States.DONE:
                order_item.set_state_executing()
                order_item.save(update_fields=['state'])

    def validate(self, order_item, request):
        processor = self.get_processor(order_item)
        if processor:
            processor(order_item).validate_order_item(request)


manager = PluginManager()
