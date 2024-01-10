from waldur_core.core.utils import get_system_robot
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_mastermind.marketplace import processors


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = TestNewInstanceViewSet
    fields = ["name"]

    def validate_order(self, request):
        pass


class TestUpdateScopedProcessor(processors.UpdateScopedResourceProcessor):
    def validate_order(self, request):
        pass

    def update_limits_process(self, user):
        return True


def create_system_robot():
    # We need create a system robot account because
    # account created in a migration does not exist when test is running
    UserFactory(
        first_name="System",
        last_name="Robot",
        username="system_robot",
        description="Special user used for performing actions on behalf of Waldur.",
        is_staff=True,
        is_active=True,
    )
    get_system_robot.cache_clear()
