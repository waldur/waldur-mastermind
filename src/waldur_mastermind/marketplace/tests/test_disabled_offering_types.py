from constance.test.unittest import override_config
from rest_framework import test
from rest_framework.exceptions import ValidationError

from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.serializers import OfferingCreateSerializer


class DisabledOfferingTypesTest(test.APITransactionTestCase):
    def setUp(self):
        self._original_backends = dict(plugins.manager.backends)
        plugins.manager.register("Test.Dummy")

    def tearDown(self):
        plugins.manager.backends = self._original_backends

    def test_disabled_offering_type_is_filtered_from_listing(self):
        self.assertIn("Test.Dummy", plugins.manager.get_offering_types())
        with override_config(DISABLED_OFFERING_TYPES=["Test.Dummy"]):
            self.assertNotIn("Test.Dummy", plugins.manager.get_offering_types())

    def test_validate_type_rejects_disabled_offering_type(self):
        with override_config(DISABLED_OFFERING_TYPES=["Test.Dummy"]):
            serializer = OfferingCreateSerializer()
            with self.assertRaises(ValidationError):
                serializer.validate_type("Test.Dummy")
