from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase


class OpenAPITestCase(SimpleTestCase):
    def test_openapi_is_exported(self):
        result = StringIO()
        call_command(
            "spectacular",
            file="waldur-openapi-schema.yaml",
            fail_on_warn=True,
            stderr=result,
        )
