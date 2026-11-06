from django.db.models import ProtectedError
from django.test import SimpleTestCase
from rest_framework import status

from waldur_core.core.views import exception_handler
from waldur_core.structure.models import Customer, Project


class ProtectedErrorHandlerTest(SimpleTestCase):
    def test_a_delete_blocked_by_one_kind_is_refused(self):
        error = ProtectedError("protected", [Project(name="a")])
        response = exception_handler(error, {})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("projects", str(response.data["detail"]))

    def test_a_delete_blocked_by_several_kinds_is_refused_not_a_500(self):
        error = ProtectedError(
            "protected", [Project(name="a"), Project(name="b"), Customer(name="c")]
        )
        response = exception_handler(error, {})
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("organizations, projects", str(response.data["detail"]))
