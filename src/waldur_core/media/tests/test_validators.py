from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from waldur_core.media.validators import CertificateValidator

# A minimal PEM-encoded certificate bundle. The bytes only need to be
# detected as text/plain by libmagic, which is one of the allowed types.
PEM_CONTENT = b"""-----BEGIN CERTIFICATE-----
MIIBazCCARGgAwIBAgIUABCDEF1234567890abcdefghijklmnowDQYJKoZIhvcNAQEL
-----END CERTIFICATE-----
"""


class CertificateValidatorTest(TestCase):
    def test_pem_file_is_accepted(self):
        certificate = SimpleUploadedFile(
            "certificate.pem", PEM_CONTENT, content_type="application/x-pem-file"
        )
        # Should not raise: a ".pem" extension must be accepted.
        CertificateValidator(certificate)

    def test_uppercase_pem_extension_is_accepted(self):
        certificate = SimpleUploadedFile(
            "CERTIFICATE.PEM", PEM_CONTENT, content_type="application/x-pem-file"
        )
        CertificateValidator(certificate)

    def test_wrong_extension_is_rejected(self):
        certificate = SimpleUploadedFile(
            "certificate.txt", PEM_CONTENT, content_type="text/plain"
        )
        with self.assertRaises(ValidationError) as context:
            CertificateValidator(certificate)
        self.assertEqual(context.exception.code, "invalid_extension")
