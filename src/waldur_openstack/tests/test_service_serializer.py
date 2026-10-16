from rest_framework import test

from waldur_openstack.serializers import OpenStackServiceSerializer

VALID_BACKEND_URLS = (
    "http://keystone.example.com:5000/v3",
    "http://controller:5000/v3",
    "https://example.com:5000/v3",
    "ssh://bastion.example.com",
    "ssh://login01",
    "ldap://directory.example.com",
    "ldaps://directory.example.com",
    "rdp://desktop.example.com",
)
INVALID_BACKEND_URLS = (
    "not a url",
    "localhost",
    "example.com",
    "ftp://files.example.com",
    "ftps://files.example.com",
    "http://keystone_1.example.com",
    "http://999.999.999.999",
    "http://256.0.0.1:5000",
    "http://1.2.3",
    "http://1234",
)


class OpenStackServiceSerializerURLTest(test.APITestCase):
    def test_default_backend_url_is_accepted(self):
        serializer = OpenStackServiceSerializer(data={"password": "secret"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["backend_url"],
            "http://keystone.example.com:5000/v3",
        )

    def test_accepts_supported_backend_urls(self):
        for backend_url in VALID_BACKEND_URLS:
            with self.subTest(backend_url=backend_url):
                serializer = OpenStackServiceSerializer(
                    data={"backend_url": backend_url, "password": "secret"}
                )
                self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_malformed_backend_urls(self):
        for backend_url in INVALID_BACKEND_URLS:
            with self.subTest(backend_url=backend_url):
                serializer = OpenStackServiceSerializer(
                    data={"backend_url": backend_url, "password": "secret"}
                )
                self.assertFalse(serializer.is_valid())
                self.assertIn(
                    "Enter a valid URL", str(serializer.errors["backend_url"])
                )
