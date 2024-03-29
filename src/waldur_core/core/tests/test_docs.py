from django.contrib.auth import get_user_model
from rest_framework import status, test

User = get_user_model()


class DocsRenderTest(test.APITransactionTestCase):
    def test_swagger_docs_render(self):
        user, _ = User.objects.get_or_create(
            username="waldur_docs_tester", is_staff=True
        )
        self.client.force_authenticate(user=user)

        response = self.client.get("/docs/user")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_user_docs_render(self):
        response = self.client.get("/docs/user")
        self.assertEqual(status.HTTP_200_OK, response.status_code)
