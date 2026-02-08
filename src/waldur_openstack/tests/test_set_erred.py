from rest_framework import status, test

from waldur_core.core.enums import CoreStates

from . import factories, fixtures


class BaseSetErredTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


class NetworkSetErredTest(BaseSetErredTest):
    def setUp(self):
        super().setUp()
        self.network = self.fixture.network

    def test_staff_can_set_erred_on_ok_resource(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.NetworkFactory.get_url(self.network, action="set_erred")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.ERRED)

    def test_staff_can_set_erred_on_creating_resource(self):
        self.network.state = CoreStates.CREATING
        self.network.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.staff)
        url = factories.NetworkFactory.get_url(self.network, action="set_erred")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.ERRED)

    def test_staff_can_set_erred_with_error_message(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.NetworkFactory.get_url(self.network, action="set_erred")

        response = self.client.post(
            url,
            {
                "error_message": "Stuck in creating",
                "error_traceback": "Some traceback",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.ERRED)
        self.assertEqual(self.network.error_message, "Stuck in creating")
        self.assertEqual(self.network.error_traceback, "Some traceback")

    def test_staff_can_set_erred_with_empty_body(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.NetworkFactory.get_url(self.network, action="set_erred")

        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.ERRED)
        self.assertEqual(self.network.error_message, "")

    def test_non_staff_cannot_set_erred(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.NetworkFactory.get_url(self.network, action="set_erred")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_set_ok_on_erred_resource(self):
        self.network.state = CoreStates.ERRED
        self.network.error_message = "Some error"
        self.network.error_traceback = "Some traceback"
        self.network.save(update_fields=["state", "error_message", "error_traceback"])
        self.client.force_authenticate(self.fixture.staff)
        url = factories.NetworkFactory.get_url(self.network, action="set_ok")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.OK)
        self.assertEqual(self.network.error_message, "")
        self.assertEqual(self.network.error_traceback, "")

    def test_non_staff_cannot_set_ok(self):
        self.network.state = CoreStates.ERRED
        self.network.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.owner)
        url = factories.NetworkFactory.get_url(self.network, action="set_ok")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_after_set_erred_pull_is_available(self):
        """After marking as ERRED, the pull action should accept the resource
        (pull validators require OK or ERRED state)."""
        self.network.state = CoreStates.CREATING
        self.network.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.staff)
        set_erred_url = factories.NetworkFactory.get_url(
            self.network, action="set_erred"
        )

        response = self.client.post(set_erred_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.network.refresh_from_db()
        self.assertEqual(self.network.state, CoreStates.ERRED)


class RouterSetErredTest(BaseSetErredTest):
    def setUp(self):
        super().setUp()
        self.router = self.fixture.router

    def test_staff_can_set_erred_on_router(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.RouterFactory.get_url(self.router, action="set_erred")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.router.refresh_from_db()
        self.assertEqual(self.router.state, CoreStates.ERRED)

    def test_non_staff_cannot_set_erred_on_router(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.RouterFactory.get_url(self.router, action="set_erred")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_set_ok_on_router(self):
        self.router.state = CoreStates.ERRED
        self.router.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.staff)
        url = factories.RouterFactory.get_url(self.router, action="set_ok")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.router.refresh_from_db()
        self.assertEqual(self.router.state, CoreStates.OK)

    def test_non_staff_cannot_set_ok_on_router(self):
        self.router.state = CoreStates.ERRED
        self.router.save(update_fields=["state"])
        self.client.force_authenticate(self.fixture.owner)
        url = factories.RouterFactory.get_url(self.router, action="set_ok")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
