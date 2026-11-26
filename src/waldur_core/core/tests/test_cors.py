from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings

from waldur_core.core import cors
from waldur_core.server import middleware


class PublicCorsHelpersTest(SimpleTestCase):
    @override_settings(PUBLIC_CORS_PATH_PREFIXES=("/.well-known/", "/public/"))
    def test_is_public_cors_path_matches_configured_prefixes(self):
        self.assertTrue(cors.is_public_cors_path("/.well-known/oauth"))
        self.assertTrue(cors.is_public_cors_path("/public/info"))
        self.assertFalse(cors.is_public_cors_path("/api/projects/"))

    def test_add_public_cors_headers_sets_all_headers(self):
        response = cors.add_public_cors_headers(HttpResponse())
        for header, value in cors.PUBLIC_CORS_HEADERS.items():
            self.assertEqual(response[header], value)

    def test_server_middleware_reexports_cors_helpers(self):
        self.assertIs(middleware.PUBLIC_CORS_HEADERS, cors.PUBLIC_CORS_HEADERS)
        self.assertIs(middleware.is_public_cors_path, cors.is_public_cors_path)
        self.assertIs(middleware.add_public_cors_headers, cors.add_public_cors_headers)
