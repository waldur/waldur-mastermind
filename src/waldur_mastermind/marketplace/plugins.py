import logging

from rest_framework import exceptions


logger = logging.getLogger(__name__)


class PluginManager(object):
    def __init__(self):
        self.backends = {}

    def register(self, offering_type, processor, validator=None, components=None):
        """

        :param offering_type: string which consists of application name and model name,
                              for example Support.OfferingTemplate
        :param processor: function which receives order item and request object,
                          and creates plugin's resource corresponding to provided order item.
                          It is called after order has been approved.
        :param validator: optional function which receives order item and request object,
                          and raises validation error if order item is invalid.
                          It is called after order has been created but before it is submitted.
        :param components: optional dictionary of available plan components, for example
        :return:
        """
        self.backends[offering_type] = {
            'processor': processor,
            'validator': validator,
            'components': components,
        }

    def get_processor(self, offering_type):
        """
        Return a processor function for given offering_type.
        :param offering_type: offering type name
        :return: processor function
        """
        return self.backends.get(offering_type, {}).get('processor')

    def get_validator(self, offering_type):
        """
        Return a validator function for given offering_type.
        :param offering_type: offering type name
        :return: validator function
        """
        return self.backends.get(offering_type, {}).get('validator')

    def get_components(self, offering_type):
        """
        Return a components dict for given offering_type.
        :param offering_type: offering type name
        :return: components dict
        """
        return self.backends.get(offering_type, {}).get('components')

    def process(self, order_item, request):
        processor = self.get_processor(order_item.offering.type)

        if not processor:
            order_item.error_message = 'Skipping order item processing because processor is not found.'
            order_item.set_state('erred')
            return

        try:
            processor(order_item, request)
        except exceptions.APIException as e:
            order_item.error_message = e
            order_item.set_state('erred')
        else:
            order_item.set_state('executing')

    def validate(self, order_item, request):
        validator = self.get_validator(order_item.offering.type)
        if validator:
            validator(order_item, request)


manager = PluginManager()
