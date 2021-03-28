from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_mastermind.marketplace import processors


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = TestNewInstanceViewSet
    fields = ['name']

    def validate_order_item(self, request):
        pass


class TestUpdateScopedProcessor(processors.UpdateScopedResourceProcessor):
    def validate_order_item(self, request):
        pass

    def update_limits_process(self, user):
        pass
