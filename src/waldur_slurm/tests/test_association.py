from rest_framework import test

from . import factories, fixtures


class AssociationGetTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.SlurmFixture()
        self.association = self.fixture.association
        self.allocation = self.fixture.allocation
        self.client.force_login(self.fixture.user)

    def test_get_association(self):
        url = factories.AssociationFactory.get_url(self.association)
        response = self.client.get(url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(self.association.uuid.hex, response.data["uuid"])

    def test_filter_associations_by_allocation_uuid(self):
        second_association = factories.AssociationFactory(allocation=self.allocation)
        url = factories.AssociationFactory.get_list_url()
        response = self.client.get(url, {"allocation_uuid": self.allocation.uuid.hex})
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            [self.association.uuid.hex, second_association.uuid.hex],
            [item["uuid"] for item in response.data],
        )
