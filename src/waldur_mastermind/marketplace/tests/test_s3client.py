"""The bucket browser's own S3 client: signing, requests and response parsing."""

import base64
import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import requests
import responses
from django.test import SimpleTestCase

from waldur_mastermind.marketplace.s3client import (
    S3Client,
    S3ClientError,
    S3ConnectionError,
    S3TimeoutError,
    signer,
)

# AWS's worked examples for S3 SigV4 ("Signature Calculations in AWS Signature
# Version 4", header-based GET Object and query-string presigned URL).
AWS_KEY = signer.Credentials(
    "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
)
AWS_DATE = datetime(2013, 5, 24, tzinfo=UTC)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

ENDPOINT = "https://rgw.example.org"
NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


class SignerTest(SimpleTestCase):
    def test_header_signature_matches_the_aws_example(self):
        headers = {
            "Host": "examplebucket.s3.amazonaws.com",
            "Range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_SHA256,
            "x-amz-date": "20130524T000000Z",
        }

        signer.sign_v4_s3(
            method="GET",
            url=urlsplit("https://examplebucket.s3.amazonaws.com/test.txt"),
            region="us-east-1",
            headers=headers,
            credentials=AWS_KEY,
            content_sha256=EMPTY_SHA256,
            date=AWS_DATE,
        )

        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/"
            "aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )

    def test_presigned_url_matches_the_aws_example(self):
        url = signer.presign_v4(
            method="GET",
            url=urlsplit("https://examplebucket.s3.amazonaws.com/test.txt"),
            region="us-east-1",
            credentials=AWS_KEY,
            date=AWS_DATE,
            expires=86400,
        )

        self.assertEqual(
            parse_qs(url.query)["X-Amz-Signature"],
            ["aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"],
        )


class ClientTest(SimpleTestCase):
    def setUp(self):
        self.client = S3Client(
            endpoint_url=ENDPOINT,
            region_name="default",
            aws_access_key_id="AKIAEXAMPLE000000001",
            aws_secret_access_key="secret",
        )

    @responses.activate
    def test_listing_is_path_style_signed_and_parsed(self):
        responses.get(
            f"{ENDPOINT}/data",
            body=f"""<ListBucketResult {NS}>
              <IsTruncated>true</IsTruncated>
              <NextContinuationToken>tok</NextContinuationToken>
              <Contents><Key>logs/a b.txt</Key><Size>3</Size>
                <LastModified>2026-11-17T20:59:44.318Z</LastModified>
                <ETag>"abc"</ETag><StorageClass>STANDARD</StorageClass></Contents>
              <CommonPrefixes><Prefix>logs/sub/</Prefix></CommonPrefixes>
            </ListBucketResult>""",
        )

        result = self.client.list_objects_v2(
            Bucket="data", Prefix="logs/", Delimiter="/", MaxKeys=200
        )

        sent = responses.calls[0].request
        self.assertEqual(
            parse_qs(urlsplit(sent.url).query),
            {
                "delimiter": ["/"],
                "encoding-type": ["url"],
                "list-type": ["2"],
                "max-keys": ["200"],
                "prefix": ["logs/"],
            },
        )
        self.assertTrue(sent.headers["Authorization"].startswith("AWS4-HMAC-SHA256 "))
        self.assertEqual(result["Contents"][0]["Key"], "logs/a b.txt")
        self.assertEqual(result["Contents"][0]["Size"], 3)
        self.assertEqual(result["Contents"][0]["LastModified"].year, 2026)
        self.assertEqual(result["CommonPrefixes"], [{"Prefix": "logs/sub/"}])
        self.assertEqual(result["NextContinuationToken"], "tok")
        self.assertTrue(result["IsTruncated"])

    @responses.activate
    def test_a_key_is_percent_encoded_in_the_path(self):
        responses.head(f"{ENDPOINT}/data/dir/a%20b%2Bc.txt", headers={"ETag": '"x"'})

        self.client.head_object(Bucket="data", Key="dir/a b+c.txt")

        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_an_error_body_becomes_its_s3_code(self):
        responses.get(
            f"{ENDPOINT}/data",
            status=404,
            body="<Error><Code>NoSuchBucketPolicy</Code><Message>none</Message></Error>",
        )

        with self.assertRaises(S3ClientError) as caught:
            self.client.get_bucket_policy(Bucket="data")

        self.assertEqual(caught.exception.code, "NoSuchBucketPolicy")
        self.assertEqual(caught.exception.status, 404)

    @responses.activate
    def test_a_head_without_a_body_is_named_from_its_status(self):
        responses.head(f"{ENDPOINT}/data/gone", status=404)

        with self.assertRaises(S3ClientError) as caught:
            self.client.head_object(Bucket="data", Key="gone")

        self.assertEqual(caught.exception.code, "NoSuchKey")

    @responses.activate
    def test_read_stats_and_headers_come_back(self):
        responses.head(
            f"{ENDPOINT}/data",
            headers={"X-RGW-Object-Count": "2", "X-RGW-Bytes-Used": "10"},
        )

        result = self.client.head_bucket(Bucket="data", ReadStats=True)

        self.assertIn("read-stats=true", responses.calls[0].request.url)
        headers = result["ResponseMetadata"]["HTTPHeaders"]
        self.assertEqual(headers["x-rgw-object-count"], "2")

    @responses.activate
    def test_object_headers_and_metadata_are_read(self):
        responses.head(
            f"{ENDPOINT}/data/a",
            headers={
                "Content-Length": "42",
                "Content-Type": "text/csv",
                "ETag": '"abc-3"',
                "Last-Modified": "Wed, 18 Nov 2026 11:43:08 GMT",
                "x-amz-meta-source": "lab",
                "x-amz-object-lock-legal-hold": "ON",
            },
        )

        result = self.client.head_object(Bucket="data", Key="a")

        self.assertEqual(result["ContentLength"], 42)
        self.assertEqual(result["ContentType"], "text/csv")
        self.assertEqual(result["Metadata"], {"source": "lab"})
        self.assertEqual(result["ObjectLockLegalHoldStatus"], "ON")
        self.assertEqual(result["LastModified"].hour, 11)

    @responses.activate
    def test_acl_grants_carry_their_type_and_group(self):
        responses.get(
            f"{ENDPOINT}/data",
            body=f"""<AccessControlPolicy {NS}>
              <Owner><ID>RGW1</ID><DisplayName>owner</DisplayName></Owner>
              <AccessControlList>
                <Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xsi:type="Group"><URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>
                  </Grantee><Permission>READ</Permission></Grant>
              </AccessControlList></AccessControlPolicy>""",
        )

        result = self.client.get_bucket_acl(Bucket="data")

        self.assertIn("acl=", responses.calls[0].request.url)
        self.assertEqual(result["Owner"]["DisplayName"], "owner")
        grantee = result["Grants"][0]["Grantee"]
        self.assertEqual(grantee["Type"], "Group")
        self.assertTrue(grantee["URI"].endswith("AllUsers"))

    @responses.activate
    def test_multi_delete_sends_content_md5_and_reports_refusals(self):
        responses.post(
            f"{ENDPOINT}/data",
            body=f"""<DeleteResult {NS}>
              <Error><Key>held.txt</Key><Code>AccessDenied</Code>
                <Message>denied</Message></Error>
            </DeleteResult>""",
        )

        result = self.client.delete_objects(
            Bucket="data", Delete={"Objects": [{"Key": "held.txt"}], "Quiet": True}
        )

        sent = responses.calls[0].request
        self.assertIn("delete=", sent.url)
        self.assertEqual(
            sent.headers["Content-MD5"],
            base64.b64encode(hashlib.md5(sent.body).digest()).decode(),
        )
        self.assertIn(b"<Key>held.txt</Key>", sent.body)
        self.assertEqual(result["Errors"][0]["Code"], "AccessDenied")

    @responses.activate
    def test_an_upload_streams_without_checksum_headers(self):
        responses.put(f"{ENDPOINT}/data/a.txt", headers={"ETag": '"e"'})

        self.client.put_object(
            Bucket="data",
            Key="a.txt",
            Body=b"abc",
            ContentLength=3,
            ContentType="text/plain",
        )

        sent = responses.calls[0].request
        self.assertEqual(sent.headers["X-Amz-Content-Sha256"], "UNSIGNED-PAYLOAD")
        self.assertFalse(
            [name for name in sent.headers if name.lower().startswith("x-amz-checksum")]
        )

    @responses.activate
    def test_a_read_is_retried_once_on_a_503(self):
        responses.get(f"{ENDPOINT}/", status=503)
        responses.get(f"{ENDPOINT}/", body=f"<ListAllMyBucketsResult {NS}/>")

        self.assertEqual(self.client.list_buckets(), {"Buckets": []})
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_a_write_is_not_retried(self):
        responses.post(f"{ENDPOINT}/data", status=503)

        with self.assertRaises(S3ClientError):
            self.client.delete_objects(
                Bucket="data", Delete={"Objects": [{"Key": "a"}]}
            )

        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_timeouts_and_refusals_are_told_apart(self):
        responses.get(f"{ENDPOINT}/", body=requests.ReadTimeout("slow"))
        with self.assertRaises(S3TimeoutError):
            self.client.list_buckets()

        responses.replace(
            responses.GET, f"{ENDPOINT}/", body=requests.ConnectionError("refused")
        )
        with self.assertRaises(S3ConnectionError):
            self.client.list_buckets()

    def test_a_presigned_link_names_the_download_and_is_signed(self):
        url = urlsplit(
            self.client.presign_get_object(
                Bucket="data",
                Key="dir/a b.txt",
                ExpiresIn=300,
                ResponseContentDisposition='attachment; filename="a b.txt"',
            )
        )
        query = parse_qs(url.query)

        self.assertEqual(url.path, "/data/dir/a%20b.txt")
        self.assertEqual(
            query["response-content-disposition"], ['attachment; filename="a b.txt"']
        )
        self.assertEqual(query["X-Amz-Expires"], ["300"])
        self.assertEqual(len(query["X-Amz-Signature"][0]), 64)


class ClientRobustnessTest(SimpleTestCase):
    def setUp(self):
        self.client = S3Client(
            endpoint_url=ENDPOINT,
            region_name="default",
            aws_access_key_id="AKIAEXAMPLE000000001",
            aws_secret_access_key="secret",
        )

    @responses.activate
    def test_a_listing_asks_for_url_encoded_keys_and_decodes_them(self):
        responses.get(
            f"{ENDPOINT}/data",
            body=f"""<ListBucketResult {NS}><EncodingType>url</EncodingType>
              <Contents><Key>sp%20ace%2Bplus%01ctl%2F%C3%A9.txt</Key><Size>1</Size></Contents>
              <CommonPrefixes><Prefix>dir%20a/</Prefix></CommonPrefixes>
            </ListBucketResult>""",
        )

        result = self.client.list_objects_v2(Bucket="data", Delimiter="/")

        self.assertIn("encoding-type=url", responses.calls[0].request.url)
        self.assertEqual(result["Contents"][0]["Key"], "sp ace+plus\x01ctl/\u00e9.txt")
        self.assertEqual(result["CommonPrefixes"], [{"Prefix": "dir a/"}])

    @responses.activate
    def test_an_unreadable_answer_is_a_gateway_error_not_a_crash(self):
        responses.get(f"{ENDPOINT}/", body="<html>proxy login</html")

        with self.assertRaises(S3ClientError) as caught:
            self.client.list_buckets()

        self.assertEqual(caught.exception.code, "MalformedResponse")
        self.assertEqual(caught.exception.status, 502)

    @responses.activate
    def test_an_entity_in_an_answer_is_refused_as_malformed(self):
        responses.get(
            f"{ENDPOINT}/",
            body='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
        )

        with self.assertRaises(S3ClientError) as caught:
            self.client.list_buckets()

        self.assertEqual(caught.exception.code, "MalformedResponse")

    @responses.activate
    def test_a_delete_that_landed_before_a_lost_answer_is_a_success(self):
        responses.delete(f"{ENDPOINT}/data", status=503)
        responses.delete(
            f"{ENDPOINT}/data",
            status=404,
            body="<Error><Code>NoSuchBucket</Code></Error>",
        )

        self.client.delete_bucket(Bucket="data")

        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_a_first_delete_of_a_missing_bucket_still_fails(self):
        responses.delete(
            f"{ENDPOINT}/data",
            status=404,
            body="<Error><Code>NoSuchBucket</Code></Error>",
        )

        with self.assertRaises(S3ClientError):
            self.client.delete_bucket(Bucket="data")

    @responses.activate
    def test_other_transport_failures_are_connection_errors(self):
        responses.get(f"{ENDPOINT}/", body=requests.exceptions.InvalidHeader("bad"))

        with self.assertRaises(S3ConnectionError):
            self.client.list_buckets()

    def test_clients_share_one_connection_pool(self):
        other = S3Client(
            endpoint_url=ENDPOINT,
            region_name="default",
            aws_access_key_id="x",
            aws_secret_access_key="y",
        )
        self.assertIs(self.client.session, other.session)

    @responses.activate
    def test_a_conditional_upload_sends_if_none_match(self):
        responses.put(f"{ENDPOINT}/data/a.txt")

        self.client.put_object(
            Bucket="data", Key="a.txt", Body=b"abc", ContentLength=3, IfNoneMatch="*"
        )

        self.assertEqual(responses.calls[0].request.headers["If-None-Match"], "*")
