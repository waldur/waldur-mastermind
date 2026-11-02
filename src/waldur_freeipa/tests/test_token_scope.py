"""Other users' FreeIPA profiles are reserved to staff. A staff user's
personal access token without `STAFF.ACCESS` acts as the user without staff
(#980).
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa.tests import factories


@override_config(PAT_ENABLED=True, FREEIPA_ENABLED=True)
@mock.patch("waldur_freeipa.backend.FreeIPABackend")
class ProfileTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = factories.ProfileFactory()
        self.url = factories.ProfileFactory.get_url(self.profile, "update_ssh_keys")

    def test_staff_session_can_sync_another_users_keys(self, backend):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.post(self.url).status_code, status.HTTP_200_OK)
        backend().update_ssh_keys.assert_called_once_with(self.profile)

    def test_staff_token_with_staff_scope_can_sync_another_users_keys(self, backend):
        use_pat(self.client, self.staff, STAFF)
        self.assertEqual(self.client.post(self.url).status_code, status.HTTP_200_OK)

    def test_staff_token_without_staff_scope_cannot_sync_another_users_keys(
        self, backend
    ):
        use_pat(self.client, self.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        backend().update_ssh_keys.assert_not_called()

    def test_other_user_cannot_sync_another_users_keys(self, backend):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        backend().update_ssh_keys.assert_not_called()
