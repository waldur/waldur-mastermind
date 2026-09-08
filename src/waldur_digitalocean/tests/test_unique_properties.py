from django.db import IntegrityError, transaction
from rest_framework import test

from . import factories


class UniquePropertiesTest(test.APITestCase):
    def test_unable_to_create_properties_with_duplicate_backend_id(self):
        property_factories = (
            factories.ImageFactory,
            factories.RegionFactory,
            factories.SizeFactory,
        )

        for factory in property_factories:
            factory(backend_id="id-1")
            # The savepoint keeps the failure from poisoning the test's
            # wrapping transaction, so APITestCase is enough here.
            with self.assertRaises(IntegrityError), transaction.atomic():
                factory(backend_id="id-1")
