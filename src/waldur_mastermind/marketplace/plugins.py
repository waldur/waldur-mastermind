import logging

from rest_framework import exceptions, serializers


logger = logging.getLogger(__name__)


class PluginManager(object):
    def __init__(self):
        self.backends = {}

    def register(self, offering_type, processor, validator=None):
        self.backends[offering_type] = {
            'processor': processor,
            'validator': validator,
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

    def process(self, order_item, request):
        processor = self.get_processor(order_item.offering.type)
        order_item.set_state('executing')

        if not processor:
            order_item.set_state('erred')
            raise exceptions.ValidationError('Skipping order item processing because processor is not found.')

        try:
            processor(order_item, request)
        except serializers.ValidationError as e:
            order_item.set_state('erred')
            raise e

    def validate(self, order_item, request):
        validator = self.get_validator(order_item.offering.type)
        if validator:
            validator(order_item, request)


manager = PluginManager()
