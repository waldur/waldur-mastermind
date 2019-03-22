from ddt import data, ddt
from rest_framework import test, status

from waldur_core.structure.tests import factories, fixtures


@ddt
class DivisionListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.division_1 = factories.DivisionFactory()
        self.division_2 = factories.DivisionFactory()
        self.url = factories.DivisionFactory.get_list_url()

    @data('staff', 'user')
    def test_authorized_user_can_list_divisions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_anonymous_user_cannot_list_divisions(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_filters(self):
        """Test of divisions' list filter by name, type and parent UUID."""
        division_parent = factories.DivisionFactory()
        self.division_1.parent = division_parent
        self.division_1.save()
        filters = [
            {'name': 'name', 'correct': self.division_1.name[2:], 'uncorrect': 'AAA'},
            {'name': 'name_exact', 'correct': self.division_1.name, 'uncorrect': self.division_1.name[2:]},
            {'name': 'type', 'correct': self.division_1.type.name, 'uncorrect': self.division_1.type.name[2:]},
            {'name': 'parent', 'correct': division_parent.uuid.hex, 'uncorrect': division_parent.uuid.hex[2:]},
        ]
        self.client.force_authenticate(user=self.fixture.staff)

        for f in filters:
            response = self.client.get(self.url, data={f['name']: f['correct']})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)
            response = self.client.get(self.url, data={f['name']: f['uncorrect']})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 0)
