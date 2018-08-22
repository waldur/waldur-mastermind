import logging

from rest_framework import exceptions


logger = logging.getLogger(__name__)


class PluginManager(object):
    def __init__(self):
        self.backends = {}

    def register(self, offering_type, processor):
        self.backends[offering_type] = processor

    def get_processor(self, offering_type):
        return self.backends.get(offering_type)

    def process(self, order_item, request):
        processor = self.get_processor(order_item.offering.type)
        if not processor:
            raise exceptions.ValidationError('Skipping order item processing because processor is not found.')

        processor(order_item, request)


manager = PluginManager()
