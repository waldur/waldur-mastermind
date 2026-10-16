from rest_framework import test

from waldur_vmware.serializers import VmwareServiceSerializer

VALID_BACKEND_URLS = (
    "https://vcenter.example.com/sdk",
    "http://vcenter:443/sdk",
    "http://vcenter.example.com",
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


class VmwareServiceSerializerURLTest(test.APITestCase):
    def _payload(self, backend_url):
        return {
            "backend_url": backend_url,
            "username": "administrator",
            "password": "secret",
        }

    def test_accepts_supported_backend_urls(self):
        for backend_url in VALID_BACKEND_URLS:
            with self.subTest(backend_url=backend_url):
                serializer = VmwareServiceSerializer(data=self._payload(backend_url))
                self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_malformed_backend_urls(self):
        for backend_url in INVALID_BACKEND_URLS:
            with self.subTest(backend_url=backend_url):
                serializer = VmwareServiceSerializer(data=self._payload(backend_url))
                self.assertFalse(serializer.is_valid())
                self.assertIn(
                    "Enter a valid URL", str(serializer.errors["backend_url"])
                )
