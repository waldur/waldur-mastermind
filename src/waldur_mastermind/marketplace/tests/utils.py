from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_mastermind.marketplace import processors, utils


def process_order_item(order_item, user):
    order_item.set_state_executing()
    order_item.save(update_fields=['state'])
    utils.process_order_item(order_item, user)


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = TestNewInstanceViewSet
    fields = ['name']

    def validate_order_item(self, request):
        pass


class TestUpdateScopedProcessor(processors.UpdateScopedResourceProcessor):
    def validate_order_item(self, request):
        pass

    def update_limits_process(self, user):
        return True
