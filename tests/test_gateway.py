import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stonetree_it_automation.errors import ValidationError  # noqa: E402
from stonetree_it_automation.gateway import Bitrix24Client  # noqa: E402
from stonetree_it_automation.models import Employee  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class QueueOpener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.payloads.pop(0))


class Bitrix24ClientTests(unittest.TestCase):
    def test_create_user_builds_expected_request(self):
        opener = QueueOpener([{"result": 123}])
        client = Bitrix24Client(
            "https://example.bitrix24.com/rest/1/secret/",
            opener=opener,
            sleeper=lambda _: None,
        )
        employee = Employee(
            employee_id="ST-001",
            first_name="Alex",
            last_name="Morgan",
            email="alex@example.com",
            role="it-support-specialist",
            job_title="IT Support Specialist",
        )

        user_id = client.create_user(employee, 7)

        request, timeout = opener.requests[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("123", user_id)
        self.assertTrue(request.full_url.endswith("/user.add.json"))
        self.assertEqual([7], body["UF_DEPARTMENT"])
        self.assertEqual("IT Support Specialist", body["WORK_POSITION"])
        self.assertEqual(10.0, timeout)

    def test_transient_bitrix_error_is_retried(self):
        opener = QueueOpener(
            [
                {"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"},
                {"result": []},
            ]
        )
        waits = []
        client = Bitrix24Client(
            "https://example.bitrix24.com/rest/1/secret/",
            opener=opener,
            sleeper=waits.append,
        )

        result = client.call("user.get", {"FILTER": {"EMAIL": "alex@example.com"}})

        self.assertEqual([], result)
        self.assertEqual([0.25], waits)
        self.assertEqual(2, len(opener.requests))

    def test_webhook_must_use_https(self):
        with self.assertRaises(ValidationError):
            Bitrix24Client("http://example.bitrix24.com/rest/1/secret/")


if __name__ == "__main__":
    unittest.main()

