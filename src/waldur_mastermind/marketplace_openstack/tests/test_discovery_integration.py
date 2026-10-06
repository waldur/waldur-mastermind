"""The OpenStack discovery wizard's result, applied through update_integration.

The wizard forwards the discovery preview to `update_integration` unchanged. The
preview used to put the external network, TLS and availability-zone choices
under `plugin_options`, which the offering does not declare, so they were
dropped without an error and tenants got no external network.
"""

import datetime
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_openstack.openstack_discovery import (
    OpenStackDiscoveryService,
    OpenStackTemporaryCredentials,
)

EXTERNAL_NETWORK_ID = str(uuid.uuid4())


def self_signed_certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloud.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


class DiscoveryApplyTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=OPENSTACK_TENANT_OFFERING,
            scope=structure_factories.ServiceSettingsFactory(type="OpenStack"),
        )
        self.settings = self.offering.scope
        self.url = factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        self.client.force_authenticate(self.fixture.staff)
        self.certificate = self_signed_certificate()

    def preview(self, certificate="", **choices):
        """What the wizard receives from preview_service_attributes."""
        service = OpenStackDiscoveryService(
            OpenStackTemporaryCredentials(
                auth_url="https://cloud.example.com:5000/v3",
                username="admin",
                password="secret",
                project_name="admin",
                verify_ssl=True,
                certificate=certificate,
            )
        )
        return service.build_service_attributes(**choices)

    def apply(self, preview):
        """What the wizard sends: both dicts of the preview, unchanged."""
        return self.client.post(
            self.url,
            {
                "service_attributes": preview["service_attributes"],
                "plugin_options": preview["plugin_options"],
            },
            format="json",
        )

    def test_the_choices_reach_the_service_settings(self):
        response = self.apply(
            self.preview(
                certificate=self.certificate,
                external_network_id=EXTERNAL_NETWORK_ID,
                instance_availability_zone="nova",
                volume_availability_zone="cinder-az1",
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.options["external_network_id"], EXTERNAL_NETWORK_ID
        )
        self.assertEqual(
            self.settings.options["valid_availability_zones"], {"nova": "nova"}
        )
        self.assertEqual(
            self.settings.options["volume_availability_zone_name"], "cinder-az1"
        )
        self.assertIs(self.settings.options["verify_ssl"], True)
        self.assertEqual(self.settings.options["certificate"], self.certificate)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.secret_options["openstack_api_tls_certificate"],
            self.certificate,
        )

    def test_rerunning_without_a_network_keeps_the_stored_one(self):
        self.apply(self.preview(external_network_id=EXTERNAL_NETWORK_ID))

        response = self.apply(self.preview())

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.options["external_network_id"], EXTERNAL_NETWORK_ID
        )

    def test_an_invalid_certificate_is_refused(self):
        response = self.apply(self.preview(certificate="not a certificate"))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("service_attributes", response.data)

    def test_editing_another_field_keeps_the_certificate(self):
        self.apply(self.preview(certificate=self.certificate))

        response = self.client.post(
            self.url, {"service_attributes": {"password": "new"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.options["certificate"], self.certificate)

    def test_an_empty_certificate_still_clears_it(self):
        self.apply(self.preview(certificate=self.certificate))

        response = self.client.post(
            self.url,
            {"secret_options": {"openstack_api_tls_certificate": ""}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.settings.refresh_from_db()
        self.assertNotIn("certificate", self.settings.options)

    def test_availability_zones_must_be_a_mapping(self):
        response = self.client.post(
            self.url,
            {"service_attributes": {"valid_availability_zones": "nova"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
