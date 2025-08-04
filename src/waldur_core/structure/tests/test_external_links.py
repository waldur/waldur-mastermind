from ddt import data, ddt
from rest_framework import test

from waldur_core.structure.tests import factories, fixtures


class ExternalLinksCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.url = factories.ExternalLinkFactory.get_list_url()
        self.payload = {
            "name": "Rest Test",
            "link": "https://rest-test.nodeconductor.com/",
            "description": "This is a test external link.",
        }

    def test_staff_can_create_external_link(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "Rest Test")
        self.assertEqual(response.data["link"], "https://rest-test.nodeconductor.com/")

    def test_user_cannot_create_external_link(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, 403)


@ddt
class ExternalLinksGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.external_link = factories.ExternalLinkFactory()
        self.url = factories.ExternalLinkFactory.get_url(self.external_link)

    @data("staff", "user")
    def test_user_can_get_external_link(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], self.external_link.name)
        self.assertEqual(response.data["link"], self.external_link.link)
