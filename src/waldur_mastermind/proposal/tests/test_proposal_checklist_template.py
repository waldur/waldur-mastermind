from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories


class ProposalChecklistTemplateTest(test.APITestCase):
    """Proposals have no checklist for new objects, so the inherited
    ``checklist-template`` route is disabled rather than left to fail."""

    url = "/api/proposal-proposals/checklist-template/"

    def _get(self, user, params):
        self.client.force_authenticate(user)
        return self.client.get(self.url, params)

    def test_route_is_disabled(self):
        customer = structure_factories.CustomerFactory()
        for user in (
            structure_factories.UserFactory(is_staff=True),
            structure_factories.UserFactory(),
        ):
            for params in ({}, {"parent_uuid": customer.uuid.hex}):
                with self.subTest(staff=user.is_staff, params=params):
                    response = self._get(user, params)
                    self.assertEqual(
                        response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED
                    )
