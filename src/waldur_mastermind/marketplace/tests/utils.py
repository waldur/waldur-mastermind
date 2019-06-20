from waldur_mastermind.marketplace import processors
from waldur_core.structure.tests.views import TestNewInstanceViewSet


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = TestNewInstanceViewSet
    fields = ['name']

    def validate_order_item(self, request):
        pass
