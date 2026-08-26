"""End-to-end checks for prefixes that must not become more restrictive.

`CoverageTest` proves every file field has *a* rule. These tests pin down
*which* rule, for the prefixes where getting it wrong breaks something that
does not fail loudly:

- the unauthenticated marketplace catalogue and call-for-proposals pages,
- `marketplace_remote`, which pulls offering and screenshot images over a
  bare unauthenticated ``httpx.get`` of the media URL and swallows failures
  into a log line.

The prefixes are written out rather than read back from the registry on
purpose. Deriving them would make the test agree with whatever the code
currently does, which is the opposite of what it is for.
"""

from ddt import data, ddt
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.media.utils import get_image_hash
from waldur_core.structure.tests import factories as structure_factories

PNG = b"\x89PNG\r\n"

# Reachable anonymously through a public API endpoint. Narrowing any of these
# breaks a page or an integration; see the module docstring.
PUBLIC_PREFIXES = (
    "markdown_images/",
    "marketplace_category_icons/",
    "marketplace_category_group_icons/",
    "marketplace_service_offering_thumbnails/",
    "offering/",
    "screenshot/",
    "serviceprovider/",
    "offering_files/",
    "customer/",
    "externallink/",
    "project/",
    "callmanagingorganisation/",
    "call_documents/",
)

# Require a session, but no per-object check.
AUTHENTICATED_PREFIXES = (
    "user/",
    "support_template_attachments/",
    "marketplace_offering_group_icons/",
    "rancher_icons/",
)


def make_file(name):
    return media_models.File.objects.create(
        name=name,
        content=PNG,
        size=len(PNG),
        mime_type="image/png",
        hash=get_image_hash(PNG),
    )


def media_url(file):
    return reverse("media", kwargs={"uuid": file.uuid})


@ddt
class PublicPrefixTest(test.APITestCase):
    @data(*PUBLIC_PREFIXES)
    def test_prefix_is_served_to_anonymous_users(self, prefix):
        file = make_file(f"{prefix}sample.png")

        response = self.client.get(media_url(file))

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"{prefix} must stay anonymously readable",
        )
        self.assertEqual(response.content, PNG)


@ddt
class AuthenticatedPrefixTest(test.APITestCase):
    @data(*AUTHENTICATED_PREFIXES)
    def test_prefix_is_denied_to_anonymous_users(self, prefix):
        file = make_file(f"{prefix}sample.png")

        response = self.client.get(media_url(file))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(*AUTHENTICATED_PREFIXES)
    def test_prefix_is_served_to_any_logged_in_user(self, prefix):
        file = make_file(f"{prefix}sample.png")

        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(media_url(file))

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class StaffOnlyPrefixTest(test.APITestCase):
    """Certificates are narrower than the ServiceSettings read rule on purpose."""

    def setUp(self):
        self.file = make_file("certs/server.pem")

    def test_anonymous_user_is_denied(self):
        self.assertEqual(
            self.client.get(media_url(self.file)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_regular_user_is_denied(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(
            self.client.get(media_url(self.file)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_staff_is_allowed(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        self.assertEqual(
            self.client.get(media_url(self.file)).status_code, status.HTTP_200_OK
        )

    def test_support_is_allowed(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_support=True))
        self.assertEqual(
            self.client.get(media_url(self.file)).status_code, status.HTTP_200_OK
        )
