import logging

from rest_framework import exceptions


logger = logging.getLogger(__name__)


class PluginManager(object):
    def __init__(self):
        self.backends = {}

    def register(self, model_class, processor):
        self.backends[model_class] = processor

    def get_processor(self, model_class):
        return self.backends.get(model_class)

    def process(self, order_item, request):
        processor = self.get_processor(order_item.offering.scope._meta.model)
        if not processor:
            raise exceptions.ValidationError('Skipping order item processing because processor is not found.')

        processor(order_item, request)


manager = PluginManager()
