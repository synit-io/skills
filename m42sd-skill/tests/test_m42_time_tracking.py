import unittest
from types import SimpleNamespace
from unittest import mock

from test_m42 import configured_profile, m42


ACTIVITY_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = "22222222-2222-2222-2222-222222222222"
ENTRY_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"
CONCRETE_RELATION = "UsedInTypeSPSActivityTypeIncident"
BASE_RELATION = "UsedInTypeSPSActivityTypeBase"


class TimeTrackingClient:
    """Model concrete-only and legacy relation schemas, including silent ignores."""
    def __init__(self, mode, ci_type="SPSActivityTypeIncident"):
        self.mode = mode
        self.ci_type = ci_type
        self.requests = []
        self.row = {"ID": ENTRY_ID, "TimeStamp": "created"}

    def single(self, dd, *args, **kwargs):
        if dd == m42.DD_TIME_TRACKING_CONFIG:
            return {"TicketsClosureActivityType": 4, "SupportedActivityCiTypes": self.ci_type}
        raise AssertionError(dd)

    def fragments(self, dd, **kwargs):
        if dd == m42.DD_TIME_ACTIVITY_TYPE:
            return [{"Value": 4, "DisplayString": "Resolution"}]
        raise AssertionError(dd)

    def request(self, method, path, **kwargs):
        body = kwargs.get("body")
        self.requests.append((method, path, body))
        if method == "GET" and path.endswith(f"/{ACTIVITY_ID}"):
            return {f"UsedInType{self.ci_type}": OWNER_ID}
        if method == "POST":
            return ENTRY_ID
        if method == "GET" and path.endswith(f"/{ENTRY_ID}"):
            if self.mode == "readback-fails" and self.writes:
                raise m42.M42Error("readback unavailable")
            return dict(self.row)
        if method == "PUT":
            relation = next(key for key in body if key.startswith("UsedInType"))
            if body["TimeStamp"] != self.row["TimeStamp"]:
                raise m42.M42Error("stale timestamp")
            self.row["TimeStamp"] = f"after-{len(self.writes)}"
            if self.mode == "both-fail":
                raise m42.M42Error("relation rejected")
            if relation == CONCRETE_RELATION and self.mode == "base-fallback":
                raise m42.M42Error("concrete relation unavailable")
            if relation == CONCRETE_RELATION and self.mode == "concrete-ignored":
                return None
            if self.mode == "both-ignored":
                return None
            self.row[relation] = (
                USER_ID if self.mode == "wrong-owner" else body[relation]
            )
            if self.mode == "applied-with-error":
                raise m42.M42Error("write response lost")
            return None
        raise AssertionError((method, path))

    @property
    def writes(self):
        return [body for method, _path, body in self.requests if method == "PUT"]


class TimeTrackingTests(unittest.TestCase):
    def record(self, client):
        with mock.patch.object(m42, "_current_identity", return_value=USER_ID):
            return m42._record_close_work_time(client, ACTIVITY_ID, 5)

    def assert_single_created_row(self, client):
        self.assertEqual(sum(method == "POST" for method, _, _ in client.requests), 1)

    def test_concrete_owner_relation_used_for_each_ci(self):
        for ci_type in ("SPSActivityTypeIncident", "SPSActivityTypeTask", "SPSActivityTypeProblem"):
            with self.subTest(ci_type=ci_type):
                client = TimeTrackingClient("concrete", ci_type)
                self.assertEqual(self.record(client), ENTRY_ID)
                self.assertEqual(client.writes, [{
                    "ID": ENTRY_ID, "TimeStamp": "created",
                    f"UsedInType{ci_type}": OWNER_ID,
                }])
                self.assert_single_created_row(client)

    def test_base_fallback_requires_fresh_unlinked_readback(self):
        for mode in ("base-fallback", "concrete-ignored"):
            with self.subTest(mode=mode):
                client = TimeTrackingClient(mode)
                self.assertEqual(self.record(client), ENTRY_ID)
                self.assertEqual(client.writes, [
                    {"ID": ENTRY_ID, "TimeStamp": "created", CONCRETE_RELATION: OWNER_ID},
                    {"ID": ENTRY_ID, "TimeStamp": "after-1", BASE_RELATION: OWNER_ID},
                ])
                self.assert_single_created_row(client)

    def test_readback_success_after_put_error_does_not_retry_link(self):
        client = TimeTrackingClient("applied-with-error")
        self.assertEqual(self.record(client), ENTRY_ID)
        self.assertEqual(len(client.writes), 1)
        self.assertIn(CONCRETE_RELATION, client.writes[0])
        self.assert_single_created_row(client)

    def test_failed_link_reports_existing_entry_and_stops(self):
        for mode, write_count in (
            ("both-fail", 2), ("both-ignored", 2),
            ("wrong-owner", 1), ("readback-fails", 1),
        ):
            with self.subTest(mode=mode):
                client = TimeTrackingClient(mode)
                with self.assertRaisesRegex(m42.M42Error, ENTRY_ID):
                    self.record(client)
                self.assertEqual(len(client.writes), write_count)
                self.assert_single_created_row(client)

    def test_base_owner_is_not_attempted_twice(self):
        client = TimeTrackingClient("both-fail", "SPSActivityTypeBase")
        with self.assertRaisesRegex(m42.M42Error, ENTRY_ID):
            self.record(client)
        self.assertEqual(len(client.writes), 1)
        self.assert_single_created_row(client)

    def test_failed_time_link_prevents_close_endpoint_and_state_writes(self):
        client = TimeTrackingClient("both-fail")
        client.tenant_profile = configured_profile()
        original_single = client.single

        def single(dd, *args, **kwargs):
            if dd == m42.DD_ACTIVITY:
                return {"ID": ACTIVITY_ID}
            return original_single(dd, *args, **kwargs)

        args = SimpleNamespace(
            ticket_number="INC123", confirm=True, comment="Test solution",
            reason="solved", work_minutes=5, kb=None,
            notify_initiator=False, no_auto_recipient=True,
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "single", side_effect=single), \
                mock.patch.object(m42, "_current_identity", return_value=USER_ID), \
                mock.patch.object(m42, "_ticket_common_fragment", return_value={"State": 202}), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(m42, "_resolve_semantic_state", return_value=220):
            with self.assertRaisesRegex(m42.M42Error, ENTRY_ID):
                m42.cmd_close_ticket(args)
        self.assert_single_created_row(client)
        self.assertTrue(all(
            path == f"/api/data/fragments/{m42.DD_TIME_TRACKING}"
            for method, path, _ in client.requests if method in ("POST", "PUT")
        ))


if __name__ == "__main__":
    unittest.main()
