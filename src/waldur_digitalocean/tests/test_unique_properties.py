from django.db import IntegrityError
from rest_framework import test

from . import factories


class UniquePropertiesTest(test.APITransactionTestCase):
    def test_unable_to_create_properties_with_duplicate_backend_id(self):
        property_factories = (
            factories.ImageFactory,
            factories.RegionFactory,
            factories.SizeFactory,
        )

        for factory in property_factories:
            factory(backend_id="id-1")
            with self.assertRaises(IntegrityError):
                factory(backend_id="id-1")
