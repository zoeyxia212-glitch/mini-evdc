"""Tests for the real-AWS wrapper without needing boto3 or an AWS account.

botocore's ClientError carries the AWS error in `.response`; the wrapper only
relies on that shape, so a look-alike exception is enough to test the mapping.
"""
import os
import unittest

from tests.helpers import fast_policy, no_sleep
from evdc.adapters.aws import AwsAdapter
from evdc.app import App, resolve
from evdc.clouds.awsreal import Ec2Client, translate
from evdc.clouds.fake import FakeEc2
from evdc.errors import (AuthError, CloudApiError, IncorrectState, ResourceNotFound, ThrottledError,
                         TransientError)
from evdc.models import ResourceStatus


class ClientError(Exception):
    """Same shape as botocore.exceptions.ClientError."""

    def __init__(self, code, status=400, message="boom"):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message},
                         "ResponseMetadata": {"HTTPStatusCode": status}}


class EndpointConnectionError(Exception):
    pass


class TranslateTests(unittest.TestCase):
    def test_codes(self):
        cases = [
            (ClientError("RequestLimitExceeded", 503), ThrottledError),
            (ClientError("Throttling"), ThrottledError),
            (ClientError("AuthFailure", 401), AuthError),
            (ClientError("UnauthorizedOperation", 403), AuthError),
            (ClientError("InvalidInstanceID.NotFound"), ResourceNotFound),
            (ClientError("InvalidVolume.NotFound"), ResourceNotFound),
            (ClientError("IncorrectInstanceState"), IncorrectState),
            (ClientError("InternalError", 500), TransientError),
            (ClientError("InvalidParameterValue"), CloudApiError),
            (EndpointConnectionError("could not connect"), TransientError),
        ]
        for exc, expected in cases:
            with self.subTest(code=str(exc)):
                out = translate(exc)
                self.assertIs(type(out), expected)
                self.assertEqual(out.provider, "aws")

    def test_retryability(self):
        self.assertTrue(translate(ClientError("RequestLimitExceeded")).retryable)
        self.assertFalse(translate(ClientError("AuthFailure")).retryable)
        self.assertFalse(translate(ClientError("InvalidParameterValue")).retryable)

    def test_unknown_exception_passes_through(self):
        err = ValueError("x")
        self.assertIs(translate(err), err)


class ScriptedEc2:
    """Raises the scripted errors first, then delegates to FakeEc2 (same method names as boto3)."""

    def __init__(self, errors=()):
        self.inner = FakeEc2("AKIA-demo", "secret-demo", count=6)
        self.errors = list(errors)
        self.calls = []

    def __getattr__(self, name):
        target = getattr(self.inner, name)

        def call(**kw):
            self.calls.append(name)
            if self.errors:
                raise self.errors.pop(0)
            return target(**kw)
        return call


class WrapperTests(unittest.TestCase):
    def adapter(self, errors=()):
        client = Ec2Client({"region": "ap-southeast-2"}, ec2=ScriptedEc2(errors))
        return no_sleep(AwsAdapter("real", client, fast_policy(), page_size=4)), client

    def test_full_listing_through_wrapper(self):
        a, _ = self.adapter()
        counts = {}
        for r in a.list_resources():
            counts[r.resource_type] = counts.get(r.resource_type, 0) + 1
        self.assertEqual(counts, {"instance": 6, "disk": 8, "vpc": 2, "eip": 3})

    def test_throttling_from_boto_is_retried(self):
        a, client = self.adapter([ClientError("RequestLimitExceeded", 503)] * 2)
        self.assertEqual(len(list(a.list_resources(("instance",)))), 6)
        self.assertEqual(client._ec2.calls[:3], ["describe_instances"] * 3)

    def test_auth_failure_is_not_retried(self):
        a, client = self.adapter([ClientError("AuthFailure", 401)])
        with self.assertRaises(AuthError):
            list(a.list_resources(("instance",)))
        self.assertEqual(len(client._ec2.calls), 1)

    def test_state_conflict_surfaces(self):
        a, _ = self.adapter()
        stopped = next(r for r in a.list_resources(("instance",)) if r.status == ResourceStatus.STOPPED)
        with self.assertRaises(IncorrectState):
            a.reboot(stopped.native_id)

    def test_only_whitelisted_methods(self):
        _, client = self.adapter()
        with self.assertRaises(AttributeError):
            client.terminate_instances(InstanceIds=["i-1"])


class ConfigTests(unittest.TestCase):
    def test_optional_env_values(self):
        os.environ.pop("EVDC_TEST_MISSING", None)
        self.assertIsNone(resolve("env:EVDC_TEST_MISSING?"))
        with self.assertRaises(KeyError):
            resolve("env:EVDC_TEST_MISSING")

    def test_real_aws_account_needs_boto3_or_says_so(self):
        try:
            import boto3  # noqa: F401
            self.skipTest("boto3 installed; would create a real client")
        except ImportError:
            pass
        app = App()
        self.addCleanup(app.close)
        with self.assertRaises(RuntimeError) as ctx:
            app.load_accounts([{"provider": "aws", "account": "real", "fake": False,
                                "credentials": {"region": "ap-southeast-2"}}])
        self.assertIn("boto3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
