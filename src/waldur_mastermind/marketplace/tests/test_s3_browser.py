"""The S3 bucket browser: who may use a key to browse, where, and what it does."""

import datetime
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.core.tests.helpers import create_pat
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, s3_browser
from waldur_mastermind.marketplace.s3client import (
    S3ClientError,
    S3ConnectionError,
    S3TimeoutError,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture

States = models.ResourceApiKey.States
REVEAL = PermissionEnum.REVEAL_RESOURCE_API_KEY
ENDPOINT = "https://rgw.example.org"
ALLOWED = override_config(S3_BROWSER_ALLOWED_ENDPOINTS="rgw.example.org")


def url(api_key, action):
    return reverse(
        f"marketplace-resource-api-key-{action}", kwargs={"uuid": api_key.uuid.hex}
    )


def client_error(code, message="", status=400):
    return S3ClientError(code, message or code, status)


@ALLOWED
class S3BrowserTestBase(test.APITestCase):
    flavor = "ceph"

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.backend_metadata = {
            "s3_endpoint": ENDPOINT,
            "s3_region": "default",
        }
        self.resource.save(update_fields=["backend_metadata"])
        offering = self.resource.offering
        offering.plugin_options = {
            **(offering.plugin_options or {}),
            "enable_s3_browser": True,
            "s3_browser_flavor": self.flavor,
        }
        offering.save(update_fields=["plugin_options"])
        ProjectRole.MEMBER.add_permission(REVEAL)
        self.member = structure_factories.UserFactory()
        self.fixture.project.add_user(self.member, ProjectRole.MEMBER)
        self.key = models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id="AKIAEXAMPLE000000001",
            key_ciphertext=encryption.encrypt_value("secret-1"),
            state=States.OK,
        )
        self.s3 = mock.Mock()
        # A bucket row reads stats, versioning, ACL and policy status.
        self.s3.head_bucket.return_value = {}
        self.s3.get_bucket_versioning.return_value = {}
        self.s3.get_bucket_acl.return_value = {"Grants": []}
        self.s3.get_bucket_policy_status.return_value = {}
        patcher = mock.patch.object(s3_browser, "S3Client", return_value=self.s3)
        self.boto = patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_authenticate(self.member)

    def get(self, action, **params):
        return self.client.get(url(self.key, action), params)

    def post(self, action, data, **kwargs):
        return self.client.post(url(self.key, action), data, **kwargs)


class AccessTest(S3BrowserTestBase):
    def test_a_member_who_may_reveal_the_key_lists_buckets(self):
        self.s3.list_buckets.return_value = {
            "Buckets": [
                {"Name": "data", "CreationDate": datetime.datetime(2026, 11, 1)}
            ]
        }

        response = self.get("s3-buckets")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["name"], "data")

    def test_the_call_is_signed_with_the_keys_own_secret_at_the_resource_endpoint(self):
        self.s3.list_buckets.return_value = {"Buckets": []}

        self.get("s3-buckets")

        kwargs = self.boto.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], ENDPOINT)
        self.assertEqual(kwargs["aws_access_key_id"], "AKIAEXAMPLE000000001")
        self.assertEqual(kwargs["aws_secret_access_key"], "secret-1")

    def test_a_member_without_the_reveal_permission_is_refused(self):
        ProjectRole.MEMBER.delete_permission(REVEAL)

        response = self.get("s3-buckets")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.s3.list_buckets.assert_not_called()

    def test_another_users_assigned_key_is_refused(self):
        self.key.user = structure_factories.UserFactory()
        self.key.save(update_fields=["user"])

        self.assertEqual(self.get("s3-buckets").status_code, status.HTTP_403_FORBIDDEN)

    @override_config(PAT_ENABLED=True)
    def test_a_personal_access_token_is_refused(self):
        self.member.can_use_personal_access_tokens = True
        self.member.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(self.member, scopes=[REVEAL.value])
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertEqual(self.get("s3-buckets").status_code, status.HTTP_403_FORBIDDEN)

    def test_an_offering_without_the_browser_answers_404(self):
        offering = self.resource.offering
        offering.plugin_options = {}
        offering.save(update_fields=["plugin_options"])

        self.assertEqual(self.get("s3-buckets").status_code, status.HTTP_404_NOT_FOUND)

    def test_a_key_that_is_not_ok_is_refused(self):
        self.key.state = States.PAUSED
        self.key.save(update_fields=["state"])

        self.assertEqual(self.get("s3-buckets").status_code, status.HTTP_409_CONFLICT)


class EndpointTest(S3BrowserTestBase):
    @override_config(S3_BROWSER_ALLOWED_ENDPOINTS="")
    def test_nothing_is_contacted_until_staff_allow_the_host(self):
        response = self.get("s3-buckets")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("S3_BROWSER_ALLOWED_ENDPOINTS", str(response.data))
        self.boto.assert_not_called()

    @override_config(S3_BROWSER_ALLOWED_ENDPOINTS="rgw.example.org:8443")
    def test_an_entry_with_a_port_admits_only_that_port(self):
        self.assertEqual(
            self.get("s3-buckets").status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_credentials_in_the_endpoint_are_refused(self):
        self.resource.backend_metadata = {"s3_endpoint": "https://u:p@rgw.example.org"}
        self.resource.save(update_fields=["backend_metadata"])

        self.assertEqual(
            self.get("s3-buckets").status_code, status.HTTP_400_BAD_REQUEST
        )
        self.boto.assert_not_called()

    @override_config(S3_BROWSER_ALLOWED_ENDPOINTS="rgw.example.org")
    def test_plain_http_to_a_public_host_is_refused(self):
        self.resource.backend_metadata = {"s3_endpoint": "http://rgw.example.org"}
        self.resource.save(update_fields=["backend_metadata"])

        self.assertEqual(
            self.get("s3-buckets").status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_info_names_the_access_key_but_never_the_secret(self):
        response = self.get("s3-info")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["access_key"], "AKIAEXAMPLE000000001")
        self.assertEqual(response.data["endpoint"], ENDPOINT)
        self.assertNotIn("secret-1", str(response.data))


class OperationsTest(S3BrowserTestBase):
    def test_objects_are_listed_with_prefixes_as_folders(self):
        self.s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "logs/2026/"}],
            "Contents": [
                {"Key": "logs/", "Size": 0},
                {"Key": "logs/a.txt", "Size": 3, "ETag": '"abc"'},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "tok",
        }

        response = self.get("s3-objects", bucket="data", prefix="logs/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["folders"], ["logs/2026/"])
        # The prefix's own placeholder object is not a file in it.
        self.assertEqual([o["key"] for o in response.data["objects"]], ["logs/a.txt"])
        self.assertEqual(response.data["next_continuation_token"], "tok")
        self.assertEqual(self.s3.list_objects_v2.call_args.kwargs["Delimiter"], "/")

    def test_a_download_link_is_presigned_audited_and_not_cached(self):
        self.s3.head_object.return_value = {"ContentLength": 3}
        self.s3.presign_get_object.return_value = "https://rgw.example.org/data/a?sig"

        response = self.post("s3-download-url", {"bucket": "data", "key": "dir/a.txt"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], "https://rgw.example.org/data/a?sig")
        self.assertEqual(response["Cache-Control"], "no-store")
        kwargs = self.s3.presign_get_object.call_args.kwargs
        self.assertEqual(kwargs["ExpiresIn"], 300)
        self.assertIn('filename="a.txt"', kwargs["ResponseContentDisposition"])

    def test_a_non_ascii_name_downloads_under_its_real_name(self):
        self.s3.head_object.return_value = {"ContentLength": 3}
        self.s3.presign_get_object.return_value = "https://rgw.example.org/x"

        self.post("s3-download-url", {"bucket": "data", "key": 'Shot\u202fAM "ä".png'})

        disposition = self.s3.presign_get_object.call_args.kwargs[
            "ResponseContentDisposition"
        ]
        self.assertEqual(
            disposition,
            'attachment; filename="Shot_AM ___.png"; '
            "filename*=UTF-8''Shot%E2%80%AFAM%20%22%C3%A4%22.png",
        )
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_S3_OBJECT_DOWNLOADED
            ).exists()
        )

    def test_a_missing_object_fails_before_a_link_is_issued(self):
        self.s3.head_object.side_effect = client_error("NoSuchKey")

        response = self.post("s3-download-url", {"bucket": "data", "key": "gone"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.s3.presign_get_object.assert_not_called()

    def test_buckets_are_created_and_deleted_with_an_audit_event(self):
        self.assertEqual(
            self.post("s3-create-bucket", {"bucket": "new-bucket"}).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.post("s3-delete-bucket", {"bucket": "new-bucket"}).status_code,
            status.HTTP_204_NO_CONTENT,
        )
        self.assertEqual(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_S3_STORAGE_CHANGED
            ).count(),
            2,
        )

    def test_an_invalid_bucket_name_never_reaches_the_gateway(self):
        for name in ("A", "x", "../etc", "has space"):
            with self.subTest(name):
                response = self.post("s3-create-bucket", {"bucket": name})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.s3.create_bucket.assert_not_called()

    def test_a_prefix_delete_reports_what_remains(self):
        self.s3.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": f"p/{i}"} for i in range(1000)],
                "IsTruncated": True,
                "NextContinuationToken": f"t{n}",
            }
            for n in range(11)
        ]
        self.s3.delete_objects.return_value = {"Errors": []}

        response = self.post(
            "s3-delete-objects", {"bucket": "data", "prefix": "p/"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["deleted"], 10000)
        self.assertTrue(response.data["remaining"])
        self.assertEqual(self.s3.delete_objects.call_count, 10)

    def test_keys_or_prefix_but_not_both(self):
        response = self.post(
            "s3-delete-objects",
            {"bucket": "data", "keys": ["a"], "prefix": "p/"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ErrorMappingTest(S3BrowserTestBase):
    def test_gateway_codes_keep_their_meaning(self):
        cases = {
            "NoSuchBucket": status.HTTP_404_NOT_FOUND,
            "AccessDenied": status.HTTP_403_FORBIDDEN,
            "BucketNotEmpty": status.HTTP_409_CONFLICT,
            "QuotaExceeded": status.HTTP_409_CONFLICT,
            "Weird": status.HTTP_400_BAD_REQUEST,
        }
        for code, expected in cases.items():
            with self.subTest(code):
                self.s3.delete_bucket.side_effect = client_error(code, "msg")
                response = self.post("s3-delete-bucket", {"bucket": "data"})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.data["code"], code)

    def test_an_unreachable_or_slow_gateway_is_502_or_504(self):
        self.s3.list_buckets.side_effect = S3ConnectionError("refused")
        self.assertEqual(
            self.get("s3-buckets").status_code, status.HTTP_502_BAD_GATEWAY
        )
        self.s3.list_buckets.side_effect = S3TimeoutError("slow")
        self.assertEqual(
            self.get("s3-buckets").status_code, status.HTTP_504_GATEWAY_TIMEOUT
        )


@override_config(S3_BROWSER_MAX_UPLOAD_SIZE_MB=1)
class UploadTest(S3BrowserTestBase):
    def setUp(self):
        super().setUp()
        self.s3.head_object.side_effect = client_error("NoSuchKey", status=404)

    def upload(self, size):
        return self.post(
            "s3-upload",
            {
                "bucket": "data",
                "key": "in/f.bin",
                "file": SimpleUploadedFile("f.bin", b"x" * size),
            },
            format="multipart",
        )

    def test_a_small_file_is_put_and_audited(self):
        response = self.upload(10)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        kwargs = self.s3.put_object.call_args.kwargs
        self.assertEqual(
            (kwargs["Bucket"], kwargs["Key"], kwargs["ContentLength"]),
            ("data", "in/f.bin", 10),
        )
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_S3_STORAGE_CHANGED
            ).exists()
        )

    def test_a_file_over_the_limit_is_refused_with_413(self):
        response = self.upload(1024 * 1024 + 1)

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.s3.put_object.assert_not_called()


def head_with_stats(objects, size):
    return {
        "ResponseMetadata": {
            "HTTPHeaders": {
                "x-rgw-object-count": str(objects),
                "x-rgw-bytes-used": str(size),
            }
        }
    }


class BucketStatsTest(S3BrowserTestBase):
    def test_buckets_carry_object_count_and_size_from_rgw_read_stats(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}
        self.s3.head_bucket.return_value = head_with_stats(3, 4096)
        self.s3.get_bucket_versioning.return_value = {}
        self.s3.get_bucket_acl.return_value = {"Grants": []}
        self.s3.get_bucket_policy_status.return_value = {}

        response = self.get("s3-buckets")

        self.assertEqual(response.data[0]["objects"], 3)
        self.assertEqual(response.data[0]["size_bytes"], 4096)
        self.assertEqual(response.data[0]["versioning"], "Disabled")
        self.assertFalse(response.data[0]["is_public"])
        self.s3.head_bucket.assert_called_with(Bucket="data", ReadStats=True)

    def test_a_bucket_without_stats_still_lists(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}
        self.s3.head_bucket.side_effect = client_error("AccessDenied")

        self.s3.get_bucket_acl.side_effect = client_error("AccessDenied")

        response = self.get("s3-buckets")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data[0]["size_bytes"])
        self.assertIsNone(response.data[0]["is_public"])

    def test_a_bucket_shared_with_all_users_is_public(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}
        self.s3.get_bucket_acl.return_value = {
            "Grants": [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                    },
                    "Permission": "READ",
                }
            ]
        }
        self.s3.get_bucket_policy_status.side_effect = client_error(
            "NoSuchBucketPolicy"
        )
        self.s3.get_bucket_versioning.return_value = {"Status": "Enabled"}

        row = self.get("s3-buckets").data[0]

        self.assertTrue(row["is_public"])
        self.assertEqual(row["versioning"], "Enabled")


class QuotasTest(S3BrowserTestBase):
    def set_quotas(self, quotas):
        self.resource.backend_metadata = {
            **self.resource.backend_metadata,
            "quotas": quotas,
        }
        self.resource.save(update_fields=["backend_metadata"])

    def test_account_mode_reports_the_account_and_bucket_quota(self):
        self.set_quotas(
            {
                "account_quota": {
                    "enabled": True,
                    "max_size_bytes": 5000,
                    "max_objects": -1,
                },
                "bucket_quota": {
                    "enabled": True,
                    "max_size_bytes": 1000,
                    "max_objects": 10,
                },
            }
        )

        quotas = self.get("s3-info").data["quotas"]

        self.assertEqual(quotas["scope"], "account")
        self.assertEqual(quotas["total"], {"max_size_bytes": 5000, "max_objects": None})
        self.assertEqual(
            quotas["per_bucket"], {"max_size_bytes": 1000, "max_objects": 10}
        )

    def test_a_disabled_or_unlimited_quota_reads_as_none(self):
        self.set_quotas(
            {
                "user_quota": {"enabled": "False", "max_size_bytes": 5000},
                "bucket_quota": {
                    "enabled": True,
                    "max_size_bytes": -1,
                    "max_objects": -1,
                },
            }
        )

        quotas = self.get("s3-info").data["quotas"]

        self.assertEqual(quotas["scope"], "user")
        self.assertIsNone(quotas["total"])
        self.assertIsNone(quotas["per_bucket"])


class BucketDetailsTest(S3BrowserTestBase):
    def setUp(self):
        super().setUp()
        s3 = self.s3
        s3.head_bucket.return_value = head_with_stats(1, 10)
        s3.get_bucket_location.return_value = {"LocationConstraint": "zg"}
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.get_object_lock_configuration.side_effect = client_error(
            "ObjectLockConfigurationNotFoundError"
        )
        s3.get_bucket_acl.return_value = {
            "Owner": {"ID": "RGW1", "DisplayName": "owner"},
            "Grants": [
                {
                    "Grantee": {"Type": "CanonicalUser", "ID": "RGW1"},
                    "Permission": "FULL_CONTROL",
                },
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                    },
                    "Permission": "READ",
                },
            ],
        }
        s3.get_bucket_policy.side_effect = client_error("NoSuchBucketPolicy")
        s3.get_bucket_policy_status.return_value = {"PolicyStatus": {"IsPublic": False}}
        s3.get_public_access_block.side_effect = client_error("AccessDenied")
        s3.get_bucket_lifecycle_configuration.return_value = {
            "Rules": [
                {
                    "ID": "expire-logs",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "logs/"},
                    "Expiration": {"Days": 30},
                }
            ]
        }
        s3.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            }
        }
        s3.get_bucket_cors.side_effect = client_error("NoSuchCORSConfiguration")
        s3.get_bucket_tagging.return_value = {"TagSet": [{"Key": "team", "Value": "a"}]}

    def test_details_read_every_part_and_flag_a_public_acl(self):
        response = self.get("s3-bucket-details", bucket="data")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["size_bytes"], 10)
        self.assertEqual(data["versioning"]["status"], "Enabled")
        self.assertIsNone(data["object_lock"])
        self.assertEqual(data["acl"]["grants"][1]["group"], "all_users")
        self.assertTrue(data["is_public"])
        self.assertEqual(data["public_acl_groups"], ["all_users"])
        self.assertEqual(data["policy"], None)
        self.assertEqual(data["lifecycle_rules"][0]["prefix"], "logs/")
        self.assertEqual(data["lifecycle_rules"][0]["expiration_days"], 30)
        self.assertEqual(data["encryption"], "AES256")
        self.assertEqual(data["tags"], [{"key": "team", "value": "a"}])

    def test_a_refused_part_is_listed_as_unavailable_and_the_rest_answers(self):
        data = self.get("s3-bucket-details", bucket="data").data

        self.assertEqual(data["unavailable"], ["public_access_block"])
        self.assertIsNone(data["public_access_block"])
        self.assertEqual(data["location"], "zg")

    def test_a_public_policy_marks_the_bucket_public(self):
        self.s3.get_bucket_acl.return_value = {"Owner": {}, "Grants": []}
        self.s3.get_bucket_policy.side_effect = None
        self.s3.get_bucket_policy.return_value = {"Policy": '{"Statement": []}'}
        self.s3.get_bucket_policy_status.return_value = {
            "PolicyStatus": {"IsPublic": True}
        }

        data = self.get("s3-bucket-details", bucket="data").data

        self.assertTrue(data["is_public"])
        self.assertIn('"Statement"', data["policy"])

    def test_details_need_the_reveal_permission(self):
        ProjectRole.MEMBER.delete_permission(REVEAL)

        response = self.get("s3-bucket-details", bucket="data")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.s3.get_bucket_acl.assert_not_called()

    def test_an_unreachable_gateway_fails_the_request(self):
        self.s3.head_bucket.side_effect = None
        self.s3.get_bucket_location.side_effect = S3ConnectionError("refused")

        response = self.get("s3-bucket-details", bucket="data")

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class ObjectDetailsTest(S3BrowserTestBase):
    def setUp(self):
        super().setUp()
        self.s3.head_object.return_value = {
            "ContentLength": 42,
            "ETag": '"abc-3"',
            "ContentType": "text/csv",
            "StorageClass": "COLD",
            "Metadata": {"source": "lab"},
            "ServerSideEncryption": "AES256",
        }
        self.s3.get_object_acl.return_value = {
            "Owner": {"DisplayName": "owner"},
            "Grants": [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
                    },
                    "Permission": "READ",
                }
            ],
        }
        self.s3.get_object_tagging.return_value = {
            "TagSet": [{"Key": "k", "Value": "v"}]
        }
        self.s3.list_object_versions.return_value = {
            "Versions": [{"Key": "a.csv"}, {"Key": "a.csv"}, {"Key": "a.csv.bak"}],
            "DeleteMarkers": [{"Key": "a.csv"}],
        }

    def test_details_read_headers_acl_tags_and_versions_of_this_key_only(self):
        response = self.get("s3-object-details", bucket="data", key="a.csv")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["content_type"], "text/csv")
        self.assertEqual(data["storage_class"], "COLD")
        self.assertEqual(data["multipart_parts"], 3)
        self.assertEqual(data["metadata"], [{"key": "source", "value": "lab"}])
        self.assertTrue(data["is_public"])
        self.assertEqual(data["tags"], [{"key": "k", "value": "v"}])
        self.assertEqual(data["versions"], 2)
        self.assertEqual(data["delete_markers"], 1)

    def test_a_refused_part_is_listed_and_the_rest_answers(self):
        self.s3.get_object_tagging.side_effect = client_error("AccessDenied")

        data = self.get("s3-object-details", bucket="data", key="a.csv").data

        self.assertEqual(data["unavailable"], ["tags"])
        self.assertEqual(data["content_type"], "text/csv")

    def test_a_missing_object_is_404(self):
        self.s3.head_object.side_effect = client_error("NoSuchKey")

        response = self.get("s3-object-details", bucket="data", key="gone")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_object_details_need_the_reveal_permission(self):
        ProjectRole.MEMBER.delete_permission(REVEAL)

        response = self.get("s3-object-details", bucket="data", key="a.csv")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.s3.head_object.assert_not_called()


class ReviewFixesTest(S3BrowserTestBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def test_a_relaxed_bucket_name_the_gateway_lists_can_be_opened(self):
        self.s3.list_objects_v2.return_value = {"Contents": []}

        response = self.get("s3-objects", bucket="My_Data")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_new_bucket_still_needs_a_strict_name(self):
        response = self.post("s3-create-bucket", {"bucket": "My_Data"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.s3.create_bucket.assert_not_called()

    def test_a_prefix_delete_that_fails_part_way_reports_and_logs_what_went(self):
        self.s3.list_objects_v2.return_value = {
            "Contents": [{"Key": f"logs/{n}"} for n in range(2500)],
            "IsTruncated": False,
        }
        self.s3.delete_objects.side_effect = [
            {"Errors": []},
            {"Errors": []},
            S3TimeoutError("slow"),
        ]

        response = self.post("s3-delete-objects", {"bucket": "data", "prefix": "logs/"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["deleted"], 2000)
        self.assertTrue(response.data["remaining"])
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_S3_STORAGE_CHANGED,
                message__contains="deleted 2000",
            ).exists()
        )

    def test_a_delete_whose_first_batch_fails_is_an_error(self):
        self.s3.delete_objects.side_effect = S3TimeoutError("slow")

        response = self.post("s3-delete-objects", {"bucket": "data", "keys": ["a"]})

        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)

    def test_the_bucket_list_is_cached_briefly_and_dropped_on_change(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}

        self.get("s3-buckets")
        self.get("s3-buckets")
        self.assertEqual(self.s3.list_buckets.call_count, 1)

        self.post("s3-create-bucket", {"bucket": "more-data"})
        self.get("s3-buckets")
        self.assertEqual(self.s3.list_buckets.call_count, 2)

    def test_a_key_that_cannot_be_decrypted_is_409_not_500(self):
        self.key.key_ciphertext = "not-a-fernet-token"
        self.key.save(update_fields=["key_ciphertext"])

        response = self.get("s3-buckets")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_ipv6_allowlist_entries_match(self):
        for entry, endpoint in (
            ("[fd00::10]:7480", "http://[fd00::10]:7480"),
            ("[fd00::10]", "http://[fd00::10]:7480"),
            ("fd00::10", "http://[fd00::10]:7480"),
        ):
            with (
                self.subTest(entry=entry),
                override_config(S3_BROWSER_ALLOWED_ENDPOINTS=entry),
            ):
                self.resource.backend_metadata = {"s3_endpoint": endpoint}
                self.resource.save(update_fields=["backend_metadata"])
                self.assertEqual(
                    s3_browser.gateway_for(self.resource).endpoint, endpoint
                )

    def test_an_ipv6_entry_with_a_port_admits_only_that_port(self):
        with override_config(S3_BROWSER_ALLOWED_ENDPOINTS="[fd00::10]:7480"):
            self.resource.backend_metadata = {"s3_endpoint": "http://[fd00::10]:9000"}
            self.resource.save(update_fields=["backend_metadata"])
            with self.assertRaises(s3_browser.S3BrowserUnavailable):
                s3_browser.gateway_for(self.resource)

    def upload(self, **extra):
        return self.post(
            "s3-upload",
            {
                "bucket": "data",
                "key": "report.pdf",
                "file": SimpleUploadedFile("report.pdf", b"new"),
                **extra,
            },
            format="multipart",
        )

    def test_an_upload_does_not_overwrite_an_existing_file_unasked(self):
        # Garage ignores If-None-Match, so the file is looked up first.
        self.s3.head_object.return_value = {"ContentLength": 3}

        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "ObjectExists")
        self.s3.put_object.assert_not_called()

    def test_a_file_written_between_the_check_and_the_upload_is_kept(self):
        self.s3.head_object.side_effect = client_error("NoSuchKey", status=404)
        self.s3.put_object.side_effect = client_error("PreconditionFailed", status=412)

        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "ObjectExists")
        self.assertEqual(self.s3.put_object.call_args.kwargs["IfNoneMatch"], "*")

    def test_a_key_that_may_not_read_uploads_on_the_condition_alone(self):
        self.s3.head_object.side_effect = client_error("AccessDenied", status=403)

        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.s3.put_object.call_args.kwargs["IfNoneMatch"], "*")

    def test_an_upload_overwrites_when_asked(self):
        response = self.upload(overwrite=True)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(self.s3.put_object.call_args.kwargs["IfNoneMatch"])
        self.s3.head_object.assert_not_called()

    def test_an_upload_of_a_new_file_goes_ahead(self):
        self.s3.head_object.side_effect = client_error("NoSuchKey", status=404)

        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.s3.put_object.assert_called_once()

    def test_a_key_is_taken_exactly_as_given_spaces_included(self):
        self.s3.delete_objects.return_value = {"Errors": []}

        self.post("s3-delete-objects", {"bucket": "data", "keys": ["report "]})

        sent = self.s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        self.assertEqual(sent, [{"Key": "report "}])

    def test_a_dot_folder_in_a_path_key_is_refused_with_a_reason(self):
        response = self.post("s3-download-url", {"bucket": "data", "key": "a/../x"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("S3 client", str(response.data))
        self.s3.head_object.assert_not_called()

    def test_a_dot_folder_key_can_still_be_deleted(self):
        # A multi-delete names keys in its body, not the path.
        self.s3.delete_objects.return_value = {"Errors": []}

        response = self.post(
            "s3-delete-objects", {"bucket": "data", "keys": ["a/../x"]}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_control_character_in_a_key_is_refused(self):
        response = self.post(
            "s3-delete-objects", {"bucket": "data", "keys": ["a\x01b"]}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.s3.delete_objects.assert_not_called()

    def test_the_endpoint_is_spelled_as_a_browser_sends_host(self):
        for endpoint, expected in (
            ("https://RGW.Example.org:443", "https://rgw.example.org"),
            ("https://rgw.example.org:8443", "https://rgw.example.org:8443"),
        ):
            with self.subTest(endpoint=endpoint):
                self.resource.backend_metadata = {"s3_endpoint": endpoint}
                self.resource.save(update_fields=["backend_metadata"])
                self.assertEqual(
                    s3_browser.gateway_for(self.resource).endpoint, expected
                )


class GenericFlavorTest(S3BrowserTestBase):
    flavor = None

    def test_an_offering_without_a_flavor_is_generic_and_shows_no_sizes(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}

        info = self.get("s3-info").data
        row = self.get("s3-buckets").data[0]

        self.assertEqual(info["capabilities"]["flavor"], "generic")
        self.assertFalse(info["capabilities"]["bucket_stats"])
        self.assertEqual(
            info["capabilities"]["bucket_parts"], list(s3_browser.BUCKET_PARTS)
        )
        self.assertIsNone(row["size_bytes"])
        self.s3.head_bucket.assert_not_called()

    def test_without_a_published_region_it_signs_for_us_east_1(self):
        self.resource.backend_metadata = {"s3_endpoint": ENDPOINT}
        self.resource.save(update_fields=["backend_metadata"])

        self.assertEqual(self.get("s3-info").data["region"], "us-east-1")

    def test_an_unknown_flavor_reads_as_generic(self):
        offering = self.resource.offering
        offering.plugin_options["s3_browser_flavor"] = "nonsense"
        offering.save(update_fields=["plugin_options"])

        info = self.get("s3-info").data

        self.assertEqual(info["capabilities"]["flavor"], "generic")


class GarageFlavorTest(S3BrowserTestBase):
    flavor = "garage"

    def test_bucket_details_ask_only_for_what_garage_implements(self):
        s3 = self.s3
        s3.get_bucket_location.return_value = {"LocationConstraint": "garage"}
        # Garage answers NoSuchBucket for a bucket with no CORS rules.
        s3.get_bucket_cors.side_effect = client_error("NoSuchBucket", status=404)
        s3.get_bucket_lifecycle_configuration.return_value = {
            "Rules": [{"ID": "tmp", "Status": "Enabled", "Filter": {"Prefix": "t/"}}]
        }

        data = self.get("s3-bucket-details", bucket="data").data

        self.assertEqual(data["location"], "garage")
        self.assertIsNone(data["cors_rules"])
        self.assertEqual(data["lifecycle_rules"][0]["prefix"], "t/")
        self.assertEqual(data["unavailable"], [])
        self.assertIsNone(data["acl"])
        self.assertIsNone(data["versioning"])
        for unasked in (
            s3.get_bucket_acl,
            s3.get_bucket_versioning,
            s3.get_bucket_policy,
            s3.get_bucket_tagging,
            s3.get_object_lock_configuration,
        ):
            unasked.assert_not_called()
        s3.head_bucket.assert_called_once_with(Bucket="data", ReadStats=False)

    def test_a_missing_bucket_is_404_not_an_empty_configuration(self):
        self.s3.head_bucket.side_effect = client_error("NoSuchBucket", status=404)

        response = self.get("s3-bucket-details", bucket="gone")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_object_details_skip_acl_tags_and_versions(self):
        self.s3.head_object.return_value = {"ContentLength": 1, "ETag": '"e"'}

        data = self.get("s3-object-details", bucket="data", key="a").data

        self.assertEqual(data["unavailable"], [])
        self.assertIsNone(data["tags"])
        self.s3.get_object_acl.assert_not_called()
        self.s3.list_object_versions.assert_not_called()

    def test_the_bucket_list_shows_neither_versioning_nor_public_access(self):
        self.s3.list_buckets.return_value = {"Buckets": [{"Name": "data"}]}

        row = self.get("s3-buckets").data[0]

        self.assertIsNone(row["versioning"])
        self.assertIsNone(row["is_public"])
        self.s3.get_bucket_acl.assert_not_called()

    def test_without_a_published_region_it_signs_for_garage(self):
        self.resource.backend_metadata = {"s3_endpoint": ENDPOINT}
        self.resource.save(update_fields=["backend_metadata"])

        self.assertEqual(self.get("s3-info").data["region"], "garage")


class FlavorOptionTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.offering = self.fixture.offering
        self.client.force_authenticate(self.fixture.staff)

    def update(self, plugin_options):
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(url, {"plugin_options": plugin_options})

    def test_the_flavor_persists_through_an_integration_update(self):
        response = self.update(
            {"enable_s3_browser": True, "s3_browser_flavor": "garage"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options["s3_browser_flavor"], "garage")

    def test_an_unknown_flavor_is_refused(self):
        response = self.update({"s3_browser_flavor": "nonsense"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
