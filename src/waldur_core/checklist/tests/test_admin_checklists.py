from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class ChecklistAdminGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_checklists(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("owner")
    def test_user_cannot_list_checklists(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ChecklistAdminCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.ChecklistFactory.get_admin_list_url()

    def _get_payload(self):
        return {
            "name": "my_checklist",
            "checklist_type": enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        }

    @data("staff")
    def test_user_can_create_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Checklist.objects.filter(name="my_checklist").exists())

    @data("owner")
    def test_user_cannot_create_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ChecklistAdminUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_url(self.fixture.checklist)

    def _get_payload(self):
        return {
            "name": "new_checklist",
        }

    @data("staff")
    def test_user_can_update_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.Checklist.objects.filter(name="new_checklist").exists())

    @data("owner")
    def test_user_cannot_update_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Checklist.objects.filter(name="new_checklist").exists())


@ddt
class ChecklistAdminDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_url(self.fixture.checklist)

    @data("staff")
    def test_user_can_delete_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Checklist.objects.filter(name="my_checklist").exists())

    @data("owner")
    def test_user_cannot_delete_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Checklist.objects.filter(name="my_checklist").exists())
