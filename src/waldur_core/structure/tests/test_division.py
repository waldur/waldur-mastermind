from ddt import data, ddt
from rest_framework import status, test

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
        rows = [
            {'name': 'name', 'valid': self.division_1.name[2:], 'invalid': 'AAA'},
            {
                'name': 'name_exact',
                'valid': self.division_1.name,
                'invalid': self.division_1.name[2:],
            },
            {
                'name': 'type',
                'valid': self.division_1.type.name,
                'invalid': self.division_1.type.name[2:],
            },
            {
                'name': 'parent',
                'valid': division_parent.uuid.hex,
                'invalid': division_parent.uuid.hex[2:],
            },
        ]
        self.client.force_authenticate(user=self.fixture.staff)

        for row in rows:
            response = self.client.get(self.url, data={row['name']: row['valid']})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)

            response = self.client.get(self.url, data={row['name']: row['invalid']})
            if row['name'] == 'parent':
                self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
            else:
                self.assertEqual(status.HTTP_200_OK, response.status_code)
                self.assertEqual(len(response.data), 0)


@ddt
class DivisionChangeTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.division_1 = factories.DivisionFactory()
        self.division_2 = factories.DivisionFactory()
        self.fixture.customer.division = self.division_1
        self.fixture.customer.save()
        self.url = factories.CustomerFactory.get_url(self.fixture.customer)

    @data('staff',)
    def test_staff_can_change_customer_division(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_division_url = factories.DivisionFactory.get_url(self.division_2)
        response = self.client.patch(self.url, {'division': new_division_url})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.division, self.division_2)

    @data('owner',)
    def test_other_can_not_change_customer_division(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        new_division_url = factories.DivisionFactory.get_url(self.division_2)
        response = self.client.patch(self.url, {'division': new_division_url})
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)
