from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_mastermind.marketplace import processors


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    __test__ = False
    viewset = TestNewInstanceViewSet
    fields = ["name"]

    def validate_order(self, request):
        pass


class TestUpdateScopedProcessor(processors.UpdateScopedResourceProcessor):
    __test__ = False

    def validate_order(self, request):
        pass

    def update_limits_process(self, user):
        return True
