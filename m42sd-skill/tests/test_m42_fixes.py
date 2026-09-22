"""Regression tests for the code-review fixes (H1, M1-M10, L1-L15)."""
import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_m42 import (
    STATE_ROWS, TEST_PROFILE, config_path_env, configured_profile,
    journal_result, m42, unavailable_discovery, write_private,
)

ACTIVITY_ID = "11111111-1111-1111-1111-111111111111"
JOURNAL_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"


def close_args(**overrides):
    values = dict(
        ticket_number="INC123", confirm=True, comment="Test solution",
        reason="solved", work_minutes=15, kb=None, notify_initiator=False,
        no_auto_recipient=True, expected_timestamp=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def update_args(**overrides):
    values = dict(
        ticket_number="INC123", state=None, recipient=None, auto_recipient=False,
        no_auto_recipient=False, subject=None, urgency=None, priority=None,
        category=None, resume_at=None, expected_timestamp=None,
        allow_unreviewed_state=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def run_cli(*argv):
    """Run main() with argv; return (exit code, parsed stdout JSON or None, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(m42.sys, "argv", ["m42.py", *argv]), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            m42.main()
        except SystemExit as raised:
            code = raised.code
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


def http_error(code, body=b"", url="https://example.com/x"):
    return urllib.error.HTTPError(url, code, "status", {}, io.BytesIO(body))


def http_response(body):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


class CloseWorkTimeRetryTests(unittest.TestCase):
    """H1: every failure after the work-time booking reports the booking."""

    def make_client(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        client.single = mock.Mock(return_value={"ID": ACTIVITY_ID})
        client.fragments = mock.Mock(return_value=STATE_ROWS)
        return client

    def run_close(self, client, request_effect, common_effect, **overrides):
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "request", side_effect=request_effect), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common_effect), \
                mock.patch.object(m42, "_record_close_work_time", return_value="work-entry"), \
                mock.patch.object(m42, "_fragment_put", return_value=None), \
                mock.patch.object(m42, "_gui_journal_entry", return_value=journal_result()), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_close_ticket(close_args(**overrides))
        return json.loads(stdout.getvalue())

    def assert_booking_reported(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(result["work_minutes"], 15)
        self.assertEqual(result["work_time_entry"], "work-entry")
        self.assertTrue(result["work_time_recorded"])
        self.assertIn("do NOT book it again", result["retry_hint"])

    def test_fallback_not_allowed_reports_booked_work_time(self):
        client = self.make_client()
        client.tenant_profile["behavior"]["state_close_fallback_families"] = []
        result = self.run_close(
            client, m42.M42Error("endpoint rejected"),
            lambda *_: {"CID": "c", "State": 202, "TimeStamp": "t"},
        )
        self.assert_booking_reported(result)
        self.assertIn("does not allow state-close fallback", result["error"])

    def test_failed_fallback_state_write_reports_booked_work_time(self):
        client = self.make_client()
        result = self.run_close(
            client, m42.M42Error("endpoint rejected"),
            lambda *_: {"CID": "c", "State": 202, "TimeStamp": "t"},
        )
        self.assert_booking_reported(result)
        self.assertIn("verification failed", result["error"])

    def test_unexpected_exception_after_booking_reports_booked_work_time(self):
        client = self.make_client()
        result = self.run_close(
            client, RuntimeError("boom"),
            lambda *_: {"CID": "c", "State": 202, "TimeStamp": "t"},
        )
        self.assert_booking_reported(result)
        self.assertIn("unexpected error: boom", result["error"])

    def test_zero_minutes_failure_says_nothing_was_booked(self):
        client = self.make_client()
        client.tenant_profile["behavior"]["state_close_fallback_families"] = []
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "request", side_effect=m42.M42Error("rejected")), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 202, "TimeStamp": "t"}), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_close_ticket(close_args(work_minutes=0))
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["work_time_recorded"])
        self.assertIsNone(result["retry_hint"])


class CloseFallbackTests(unittest.TestCase):
    """M5: the fallback re-reads the ticket and recognizes a completed close."""

    def test_timeout_with_ticket_already_closed_is_success_without_state_writes(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        common_rows = [
            {"CID": "c", "State": 202, "TimeStamp": "t"},
            {"CID": "c", "State": 204, "TimeStamp": "t2"},
        ]
        stdout = io.StringIO()
        close_entry = mock.Mock(return_value=journal_result("closed-jid"))
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                mock.patch.object(client, "request",
                                  side_effect=m42.M42TimeoutError("timeout on POST")), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common_rows), \
                mock.patch.object(m42, "_record_close_work_time", return_value=None), \
                mock.patch.object(m42, "_fragment_put") as write, \
                mock.patch.object(m42, "_gui_journal_entry") as processed, \
                mock.patch.object(m42, "_close_journal_entry", close_entry), \
                contextlib.redirect_stdout(stdout):
            m42.cmd_close_ticket(close_args(work_minutes=0))
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["journal_entry"], "closed-jid")
        self.assertIn("reads back as closed", result["note"])
        self.assertIn("timeout", result["note"])
        write.assert_not_called()
        processed.assert_not_called()

    def test_fallback_uses_fresh_timestamp_not_the_initial_read(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        state = {"value": 202, "stamp": "initial"}

        def common(*_):
            row = {"CID": "c", "State": state["value"], "TimeStamp": state["stamp"]}
            state["stamp"] = f"fresh-{state['stamp']}"
            return row

        puts = []

        def put(_c, dd, body):
            puts.append(body)
            state["value"] = body["State"]

        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                mock.patch.object(client, "request", side_effect=m42.M42Error("rejected")), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common), \
                mock.patch.object(m42, "_record_close_work_time", return_value=None), \
                mock.patch.object(m42, "_fragment_put", side_effect=put), \
                mock.patch.object(m42, "_gui_journal_entry", return_value=journal_result()), \
                mock.patch.object(m42, "_close_journal_entry", return_value=journal_result()), \
                contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_close_ticket(close_args(work_minutes=0))
        self.assertEqual([body["State"] for body in puts], [220, 204])
        self.assertNotEqual(puts[0]["TimeStamp"], "initial")


class RedirectPolicyTests(unittest.TestCase):
    """M1: the shared opener refuses redirects that leave the HTTPS origin."""

    def redirect(self, source, target):
        handler = m42._SameOriginRedirectHandler()
        req = m42.urllib.request.Request(source, headers={"Authorization": "Bearer x"})
        return handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_cross_host_and_downgrade_redirects_are_refused(self):
        for target in (
            "https://evil.example.net/m42Services/api/x",
            "http://example.com/m42Services/api/x",
            "https://example.com:8443/m42Services/api/x",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(m42.M42Error, "refusing HTTP 302 redirect"):
                    self.redirect("https://example.com/m42Services/api/x", target)

    def test_same_origin_https_redirect_is_followed(self):
        new = self.redirect("https://example.com/m42Services/api/x",
                            "https://example.com/m42Services/api/y")
        self.assertEqual(new.full_url, "https://example.com/m42Services/api/y")

    def test_all_requests_use_the_shared_opener(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(m42._OPENER, "open",
                               side_effect=[http_response(b'{"RawToken":"a"}'),
                                            http_response(b'[]')]) as opener:
            client.request("GET", "/api/test")
        self.assertEqual(opener.call_count, 2)


class VerifiedStateWriteTests(unittest.TestCase):
    """M2: state writes outside close are read back before success is claimed."""

    def test_update_reports_verification_failure_and_writes_no_journal(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 200, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_fragment_put", return_value=None), \
                mock.patch.object(m42, "_gui_journal_entry") as journal, \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_update_ticket(update_args(state="paused", no_auto_recipient=True))
        result = json.loads(stdout.getvalue())
        self.assertIn("verification failed", result["error"])
        self.assertEqual(result["applied"], [])
        journal.assert_not_called()

    def test_forward_reports_verification_failure_before_recipient_change(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 203, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_fragment_put", return_value=None) as write, \
                mock.patch.object(m42, "_gui_journal_entry") as journal, \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_forward_ticket(SimpleNamespace(
                    ticket_number="INC123", target="support", to_role=True,
                    comment=None, expected_timestamp=None))
        self.assertIn("verification failed", json.loads(stdout.getvalue())["error"])
        self.assertEqual(write.call_count, 1)
        journal.assert_not_called()

    def test_reopen_reports_verification_failure_and_writes_no_journal(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 204, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_fragment_put", return_value=None), \
                mock.patch.object(m42, "_gui_journal_entry") as journal, \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_reopen_ticket(SimpleNamespace(
                    ticket_number="INC123", confirm=True, comment=None,
                    no_auto_recipient=True, expected_timestamp=None))
        self.assertIn("verification failed", json.loads(stdout.getvalue())["error"])
        journal.assert_not_called()

    def test_add_comment_fill_is_read_back(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": ACTIVITY_ID}

            def request(self, method, path, **kwargs):
                if method == "POST":
                    return {"JournalId": JOURNAL_ID}
                if method == "GET":
                    return {"ID": JOURNAL_ID, "OriginalSolutionHtml": "",
                            "ActivityAction": 0, "VisibleInPortal": 0}
                return None

        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=Client()), \
                mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(m42, "_journal_entry_belongs_to_ticket", return_value=True), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_add_comment(SimpleNamespace(
                    ticket_number="INC123", text="Hello", internal=True))
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["journal_id"], JOURNAL_ID)
        self.assertIn("verification failed", result["error"])


class ReviewedStateTests(unittest.TestCase):
    """M3: numeric/display-name states must be reviewed; closed-like values are blocked."""

    def client(self, **overrides):
        client = m42.Client("https://example.com", "token", configured_profile(overrides))
        client.fragments = mock.Mock(return_value=[
            *STATE_ROWS, {"ID": "s9", "Value": 999, "DisplayString": "Archiviert"},
        ])
        return client

    def test_unreviewed_numeric_and_display_name_require_flag(self):
        client = self.client()
        for value in ("999", "Archiviert"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(m42.M42Error, "not part of the reviewed"):
                    m42._resolve_state_value(client, value)
                self.assertEqual(
                    m42._resolve_state_value(client, value, allow_unreviewed=True), 999)

    def test_reviewed_numeric_and_display_name_resolve(self):
        client = self.client()
        self.assertEqual(m42._resolve_state_value(client, "203"), 203)
        self.assertEqual(m42._resolve_state_value(client, "Angehalten"), 203)

    def test_unicode_digits_are_not_numeric_state_input(self):
        with self.assertRaisesRegex(m42.M42Error, "unknown state"):
            m42._resolve_state_value(self.client(), "²")

    def test_update_blocks_closed_state_even_with_unreviewed_flag(self):
        client = self.client()
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 202, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_fragment_put") as write, \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                m42.cmd_update_ticket(update_args(state="204", allow_unreviewed_state=True))
        self.assertIn("use close-ticket", json.loads(stdout.getvalue())["error"])
        write.assert_not_called()


class DeleteJournalGuardTests(unittest.TestCase):
    """M4 and L15: delete-journal guards."""

    def run_delete(self, entry, force=False, confirm=True):
        client = mock.Mock()
        client.single.return_value = entry
        stdout = io.StringIO()
        code = 0
        with mock.patch.object(m42, "load_client", return_value=client), \
                contextlib.redirect_stdout(stdout):
            try:
                m42.cmd_delete_journal(SimpleNamespace(
                    ticket_number="INC123", journal_id=JOURNAL_ID,
                    force=force, confirm=confirm))
            except SystemExit as raised:
                code = raised.code
        return code, json.loads(stdout.getvalue()), client

    def test_template_entry_without_text_requires_force(self):
        code, result, client = self.run_delete(
            {"ID": JOURNAL_ID, "ActivityAction": 8, "OriginalSolutionHtml": ""})
        self.assertEqual(code, 1)
        self.assertIn("ActivityAction=8", result["error"])
        client.request.assert_not_called()

    def test_text_entry_requires_force(self):
        code, result, client = self.run_delete(
            {"ID": JOURNAL_ID, "ActivityAction": 0, "OriginalSolutionHtml": "note"})
        self.assertEqual(code, 1)
        self.assertIn("still has text", result["error"])
        client.request.assert_not_called()

    def test_empty_plain_comment_deletes_without_force(self):
        for action in (None, 0, "0"):
            with self.subTest(action=action):
                code, result, client = self.run_delete(
                    {"ID": JOURNAL_ID, "ActivityAction": action,
                     "OriginalSolutionHtml": " "})
                self.assertEqual(code, 0)
                self.assertEqual(result["deleted"], JOURNAL_ID)
                client.request.assert_called_once_with(
                    "DELETE", f"/api/data/fragments/{m42.DD_JOURNAL}/{JOURNAL_ID}")

    def test_force_deletes_template_entry_with_text(self):
        code, result, client = self.run_delete(
            {"ID": JOURNAL_ID, "ActivityAction": 8, "OriginalSolutionHtml": "x"},
            force=True)
        self.assertEqual(code, 0)
        client.request.assert_called_once()

    def test_confirm_is_required(self):
        code, result, client = self.run_delete(
            {"ID": JOURNAL_ID, "ActivityAction": 0, "OriginalSolutionHtml": ""},
            confirm=False)
        self.assertEqual(code, 1)
        self.assertIn("--confirm", result["error"])
        client.single.assert_not_called()
        client.request.assert_not_called()


class DataDefinitionNameTests(unittest.TestCase):
    """M6: data-definition names from user input are validated before URL use."""

    def test_list_pickup_rejects_path_injection(self):
        client = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaisesRegex(m42.M42Error, "invalid data definition name"):
                m42.cmd_list_pickup(SimpleNamespace(dd="SPSCommon/../objects?x=1"))
        client.fragments.assert_not_called()

    def test_fragments_validates_data_definition(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(client, "request") as request:
            with self.assertRaisesRegex(m42.M42Error, "invalid data definition name"):
                client.fragments("1bad name")
        request.assert_not_called()
        self.assertEqual(m42.validate_dd_name(" SPSCommonPickupObjectStatus "),
                         "SPSCommonPickupObjectStatus")


class PaginationTests(unittest.TestCase):
    """M7 and L11: truncation is reported, --max is capped, page size is clamped."""

    def test_truncation_at_max_records_is_flagged_and_surfaced(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(client, "request", return_value=[{"ID": "a"}, {"ID": "b"}]):
            rows = client.fragments("Example", page_size=2, max_records=2)
        self.assertTrue(rows.truncated)
        result = m42._mark_truncated({"ok": True}, rows)
        self.assertTrue(result["truncated"])
        self.assertIn("narrow the filter", result["truncation_note"])

    def test_final_short_page_is_not_truncated(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(client, "request", return_value=[{"ID": "a"}]):
            rows = client.fragments("Example", page_size=2, max_records=2)
        self.assertFalse(rows.truncated)
        self.assertNotIn("truncated", m42._mark_truncated({"ok": True}, rows))

    def test_page_size_is_clamped_to_max_records(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(client, "request", return_value=[{"ID": "a"}]) as request:
            client.fragments("Example", page_size=1000, max_records=1)
        self.assertEqual(request.call_args.kwargs["params"]["pageSize"], 1)

    def test_search_tickets_max_is_capped_and_rejects_negatives(self):
        for value in ("0", "-5", "10001", "abc"):
            with self.subTest(value=value):
                code, _result, stderr = run_cli(
                    "search-tickets", "--where", "1=1", "--max", value)
                self.assertEqual(code, 2)
                self.assertIn("--max", stderr)
        code, _result, stderr = run_cli("search-kb", "--tags", "vpn", "--max", "0")
        self.assertEqual(code, 2)

    def test_search_tickets_output_flags_truncation(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "request", return_value=[{"ID": "a"}, {"ID": "b"}]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_search_tickets(SimpleNamespace(where="1=1", columns=None, max=2))
        self.assertTrue(json.loads(stdout.getvalue())["truncated"])

    def test_many_identical_display_names_report_ambiguity(self):
        client = m42.Client("https://example.com", "token")
        rows = [{"ID": f"id-{i}", "DisplayName": "Max Muster"} for i in range(10)]

        def request(method, path, params=None, **kwargs):
            if "AccountName" in params["where"] or "MailAddress" in params["where"]:
                return []
            self.assertEqual(params["pageSize"], 10)
            return rows
        with mock.patch.object(client, "request", side_effect=request):
            with self.assertRaisesRegex(m42.M42Error, "ambiguous user"):
                m42._resolve_user_or_fail(client, "Max Muster")


class SecretsHandlingTests(unittest.TestCase):
    """M9: config resolution, permissions, atomic writes, token sources."""

    def test_resolution_order(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy.json"
            explicit = Path(directory) / "explicit.json"
            xdg = Path(directory) / "xdg"
            with mock.patch.object(m42, "LEGACY_CONFIG_PATH", str(legacy)), \
                    mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg),
                                                 "M42_CONFIG_PATH": ""}):
                del os.environ["M42_CONFIG_PATH"]
                self.assertEqual(m42.resolve_config_path(),
                                 str(xdg / "m42sd" / "m42_config.json"))
                legacy.write_text("{}", encoding="utf-8")
                self.assertEqual(m42.resolve_config_path(), str(legacy))
                os.environ["M42_CONFIG_PATH"] = str(explicit)
                self.assertEqual(m42.resolve_config_path(), str(explicit))

    def test_default_falls_back_to_home_config(self):
        with mock.patch.object(m42, "LEGACY_CONFIG_PATH", "/nonexistent/legacy.json"), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "", "HOME": "/tmp/home"}):
            del os.environ["XDG_CONFIG_HOME"]
            os.environ.pop("M42_CONFIG_PATH", None)
            self.assertEqual(m42.resolve_config_path(),
                             os.path.join("/tmp/home", ".config", "m42sd", "m42_config.json"))

    def test_group_or_world_readable_config_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            path.write_text(json.dumps({"base_url": "https://example.com",
                                        "api_token": "t"}), encoding="utf-8")
            path.chmod(0o640)
            with config_path_env(path), \
                    mock.patch.dict(os.environ, {"M42_BASE_URL": "", "M42_API_TOKEN": ""}):
                with self.assertRaisesRegex(m42.M42Error, "chmod 600"):
                    m42.load_client()
            path.chmod(0o600)
            with config_path_env(path), \
                    mock.patch.dict(os.environ, {"M42_BASE_URL": "", "M42_API_TOKEN": ""}):
                self.assertEqual(m42.load_client().base_url, "https://example.com/m42Services")

    def test_setup_writes_new_config_atomically_into_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "m42_config.json"
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(json.dumps(TEST_PROFILE), encoding="utf-8")
            args = SimpleNamespace(token=None, base_url="https://example.com",
                                   profile_file=str(profile_path))
            with config_path_env(path), \
                    mock.patch.dict(os.environ, {"M42_API_TOKEN": "a.e30.x"}), \
                    mock.patch.object(m42.Client, "_access", return_value="access"), \
                    mock.patch.object(m42, "_discover_tenant", return_value=unavailable_discovery()), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                m42.cmd_setup(args)
            self.assertEqual(json.loads(stdout.getvalue())["written"], str(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["m42_config.json"])

    def test_failed_write_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            write_private(path, '{"keep": true}')
            with mock.patch.object(m42.json, "dump", side_effect=RuntimeError("disk")):
                with self.assertRaises(RuntimeError):
                    m42._write_config(str(path), {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"keep": True})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["m42_config.json"])

    def test_token_flag_is_deprecated_and_env_is_preferred(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(m42._setup_token(SimpleNamespace(token="argv-token")),
                             "argv-token")
        self.assertIn("deprecated", stderr.getvalue())
        with mock.patch.dict(os.environ, {"M42_API_TOKEN": "env-token"}):
            self.assertEqual(m42._setup_token(SimpleNamespace(token=None)), "env-token")

    def test_setup_without_token_and_without_tty_fails_before_any_write(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        with mock.patch.dict(os.environ, {"M42_API_TOKEN": ""}), \
                mock.patch.object(m42.sys, "stdin", stdin), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                m42._setup_token(SimpleNamespace(token=None))
        self.assertIn("export M42_API_TOKEN", json.loads(stdout.getvalue())["error"])


class ExpectedTimestampTests(unittest.TestCase):
    """M10: get-ticket exposes the timestamp; mutations can require it."""

    def test_get_ticket_outputs_common_timestamp_and_unescapes_description(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "single", return_value={
                    "ID": ACTIVITY_ID, "Description": "a &lt;b&gt; &amp; c"}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 202, "TimeStamp": "stamp"}), \
                mock.patch.object(client, "fragments", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_get_ticket(SimpleNamespace(ticket_number="INC123", columns=None,
                                               portal_only=False, attachments=False))
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["timestamp"], "stamp")
        self.assertEqual(result["ticket"]["Description"], "a <b> & c")

    def test_mismatch_stops_every_mutation_before_writing(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        common = {"CID": "c", "State": 202, "TimeStamp": "current"}
        commands = [
            (m42.cmd_update_ticket, update_args(state="paused", expected_timestamp="old")),
            (m42.cmd_forward_ticket, SimpleNamespace(
                ticket_number="INC123", target="support", to_role=True, comment=None,
                expected_timestamp="old")),
            (m42.cmd_close_ticket, close_args(expected_timestamp="old")),
            (m42.cmd_reopen_ticket, SimpleNamespace(
                ticket_number="INC123", confirm=True, comment=None,
                no_auto_recipient=True, expected_timestamp="old")),
        ]
        for command, args in commands:
            with self.subTest(command=command.__name__):
                state = 204 if command is m42.cmd_reopen_ticket else 202
                with mock.patch.object(m42, "load_client", return_value=client), \
                        mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                        mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                        mock.patch.object(m42, "_ticket_common_fragment",
                                          return_value={**common, "State": state}), \
                        mock.patch.object(m42, "_record_close_work_time") as work, \
                        mock.patch.object(client, "request") as request, \
                        mock.patch.object(m42, "_fragment_put") as write, \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    with self.assertRaises(SystemExit):
                        command(args)
                result = json.loads(stdout.getvalue())
                self.assertIn("ticket changed since it was read", result["error"])
                self.assertEqual(result["current_timestamp"], "current")
                write.assert_not_called()
                request.assert_not_called()
                work.assert_not_called()

    def test_matching_timestamp_allows_the_mutation(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        common_rows = [
            {"CID": "c", "State": 202, "TimeStamp": "current"},
            {"CID": "c", "State": 203, "TimeStamp": "next"},
        ]
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common_rows), \
                mock.patch.object(m42, "_fragment_put"), \
                mock.patch.object(m42, "_gui_journal_entry", return_value=journal_result()), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_update_ticket(update_args(state="paused", expected_timestamp="current",
                                              no_auto_recipient=True))
        self.assertEqual(json.loads(stdout.getvalue())["applied"], {"State": 203})


class StateNormalizationTests(unittest.TestCase):
    """L1: the common fragment's State is normalized to int."""

    def test_string_state_matches_closed_values(self):
        client = mock.Mock()
        client.single.return_value = {"ID": "a", "CID": "c", "State": "204", "TimeStamp": "t"}
        common = m42._ticket_common_fragment(client, "INC123")
        self.assertEqual(common["State"], 204)
        self.assertIn(common["State"], {204})
        client.single.return_value = {"ID": "a", "CID": "c", "State": None}
        self.assertIsNone(m42._ticket_common_fragment(client, "INC123")["State"])
        client.single.return_value = {"ID": "a", "CID": "c", "State": "n/a"}
        self.assertEqual(m42._ticket_common_fragment(client, "INC123")["State"], "n/a")


class AutoRecipientWarningTests(unittest.TestCase):
    """L2: auto-recipient failures are surfaced, not swallowed."""

    def test_reopen_reports_auto_recipient_warning(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        common_rows = [
            {"CID": "c", "State": 204, "TimeStamp": "t"},
            {"CID": "c", "State": 202, "TimeStamp": "t2"},
        ]

        def put(_c, dd, body):
            if dd == m42.DD_ACTIVITY:
                raise m42.M42Error("recipient rejected")

        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common_rows), \
                mock.patch.object(m42, "_fragment_put", side_effect=put), \
                mock.patch.object(m42, "_current_identity", return_value=USER_ID), \
                mock.patch.object(m42, "_gui_journal_entry", return_value=journal_result()), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_reopen_ticket(SimpleNamespace(
                ticket_number="INC123", confirm=True, comment=None,
                no_auto_recipient=False, expected_timestamp=None))
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("recipient rejected", result["auto_recipient_warning"])

    def test_close_reports_auto_recipient_warning(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        common_rows = [
            {"CID": "c", "State": 202, "TimeStamp": "t"},
            {"CID": "c", "State": 204, "TimeStamp": "t2"},
        ]
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID, "TimeStamp": "t"}), \
                mock.patch.object(client, "request", return_value=None), \
                mock.patch.object(m42, "_ticket_common_fragment", side_effect=common_rows), \
                mock.patch.object(m42, "_record_close_work_time", return_value=None), \
                mock.patch.object(m42, "_fragment_put", side_effect=m42.M42Error("denied")), \
                mock.patch.object(m42, "_current_identity", return_value=USER_ID), \
                mock.patch.object(m42, "_gui_journal_entry", return_value=journal_result()), \
                mock.patch.object(m42, "_close_journal_entry", return_value=journal_result()), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_close_ticket(close_args(work_minutes=0, no_auto_recipient=False))
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("denied", result["auto_recipient_warning"])


class TransportErrorTests(unittest.TestCase):
    """L3 and L15: every transport failure maps to M42Error; GET-only retries."""

    def client(self):
        client = m42.Client("https://example.com", "token")
        client._access_token, client._access_exp = "access", m42.time.time() + 1000
        return client

    def test_access_maps_dns_timeout_and_bad_json(self):
        cases = [
            (urllib.error.URLError(socket.gaierror("name not known")), "connection error"),
            (urllib.error.URLError(socket.timeout()), "timeout"),
            (http_response(b"<html>"), "non-JSON"),
            (http_response(b"[1, 2]"), "no usable JSON object"),
        ]
        for effect, expected in cases:
            with self.subTest(expected=expected):
                client = m42.Client("https://example.com", "token")
                with mock.patch.object(m42, "_urlopen", side_effect=[effect]):
                    with self.assertRaisesRegex(m42.M42Error, expected):
                        client._access()

    def test_request_maps_timeout_and_url_errors(self):
        client = self.client()
        with mock.patch.object(m42.time, "sleep"), \
                mock.patch.object(m42, "_urlopen",
                                  side_effect=urllib.error.URLError(socket.timeout())):
            with self.assertRaisesRegex(m42.M42TimeoutError, "timeout on POST /api/x"):
                client.request("POST", "/api/x", body={})
        with mock.patch.object(m42, "_urlopen",
                               side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(m42.M42Error, "connection error on GET /api/x"):
                client.request("GET", "/api/x")

    def test_401_refresh_then_success(self):
        client = self.client()
        with mock.patch.object(m42, "_urlopen", side_effect=[
            http_error(401, b"expired"),
            http_response(b'{"RawToken":"fresh"}'),
            http_response(b'{"ok": 1}'),
        ]) as urlopen:
            self.assertEqual(client.request("GET", "/api/x"), {"ok": 1})
        self.assertEqual(urlopen.call_args_list[-1].args[0].get_header("Authorization"),
                         "Bearer fresh")

    def test_401_during_refresh_is_reported_as_token_exchange_failure(self):
        client = self.client()
        with mock.patch.object(m42, "_urlopen", side_effect=[
            http_error(401, b"expired"), http_error(401, b"bad token"),
        ]):
            with self.assertRaisesRegex(m42.M42Error, "token exchange failed"):
                client.request("GET", "/api/x")

    def test_get_is_retried_on_throttling_but_writes_are_not(self):
        client = self.client()
        with mock.patch.object(m42.time, "sleep") as sleep, \
                mock.patch.object(m42, "_urlopen", side_effect=[
                    http_error(503), http_response(b"[]")]):
            self.assertEqual(client.request("GET", "/api/x"), [])
        sleep.assert_called_once_with(m42.GET_RETRY_DELAYS[0])
        with mock.patch.object(m42.time, "sleep") as sleep, \
                mock.patch.object(m42, "_urlopen", side_effect=[http_error(503)]) as urlopen:
            with self.assertRaisesRegex(m42.M42Error, "HTTP 503 on PUT"):
                client.request("PUT", "/api/x", body={})
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_get_retry_is_bounded(self):
        client = self.client()
        attempts = len(m42.GET_RETRY_DELAYS) + 1
        with mock.patch.object(m42.time, "sleep"), \
                mock.patch.object(m42, "_urlopen",
                                  side_effect=[http_error(429)] * (attempts + 2)) as urlopen:
            with self.assertRaisesRegex(m42.M42Error, "HTTP 429"):
                client.request("GET", "/api/x")
        self.assertEqual(urlopen.call_count, attempts)


class ArgumentValidationTests(unittest.TestCase):
    """L4, L8, L12: argument bounds and rejected inputs."""

    def test_work_minutes_upper_bound(self):
        code, _result, stderr = run_cli(
            "close-ticket", "--ticket-number", "INC1", "--reason", "solved",
            "--comment", "x", "--confirm", "--work-minutes", "1441")
        self.assertEqual(code, 2)
        self.assertIn("1440", stderr)
        self.assertEqual(m42._nonnegative_minutes("1440"), 1440.0)
        with self.assertRaisesRegex(m42.M42Error, "1440"):
            m42._record_close_work_time(mock.Mock(), ACTIVITY_ID, 100000)

    def test_priority_is_range_checked(self):
        with self.assertRaises(m42.argparse.ArgumentTypeError):
            m42._priority_arg("100")
        with self.assertRaises(m42.argparse.ArgumentTypeError):
            m42._priority_arg("-1")
        self.assertEqual(m42._priority_arg("5"), 5)

    def test_empty_recipient_and_nothing_to_update_fail(self):
        for overrides, expected in (
            ({"recipient": ""}, "--recipient must not be empty"),
            ({}, "nothing to update"),
            ({"state": " "}, "--state must not be empty"),
        ):
            with self.subTest(overrides=overrides):
                with mock.patch.object(m42, "load_client") as load_client, \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    with self.assertRaises(SystemExit):
                        m42.cmd_update_ticket(update_args(**overrides))
                self.assertIn(expected, json.loads(stdout.getvalue())["error"])
                load_client.assert_not_called()

    def test_guid_pass_through_verifies_record_exists(self):
        client = mock.Mock()
        client.single.return_value = None
        with self.assertRaisesRegex(m42.M42Error, "user not found"):
            m42._resolve_user_arg(client, USER_ID)
        with self.assertRaisesRegex(m42.M42Error, "category not found"):
            m42._resolve_category_name(client, USER_ID)
        client.single.return_value = {"ID": USER_ID}
        self.assertEqual(m42._resolve_user_arg(client, USER_ID), USER_ID)
        self.assertEqual(m42._resolve_category_name(client, USER_ID), USER_ID)

    def test_close_kb_must_be_a_guid(self):
        with mock.patch.object(m42, "load_client") as load_client, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                m42.cmd_close_ticket(close_args(kb="KB0001"))
        self.assertIn("--kb must be the KB article GUID",
                      json.loads(stdout.getvalue())["error"])
        load_client.assert_called_once()

    def test_ticket_number_rejection(self):
        for value in ("123", "INC", "INC 123", "", "INC-12x"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(m42.M42Error, "invalid ticket number"):
                    m42.parse_ticket_number(value)
        self.assertEqual(m42.parse_ticket_number(" inc123 "), "INC123")

    def test_asql_quote_doubles_single_quotes(self):
        self.assertEqual(m42.asql_quote("O'Brien"), "'O''Brien'")
        self.assertEqual(m42.asql_quote("x'; DROP--"), "'x''; DROP--'")
        self.assertEqual(m42.asql_quote(7), "'7'")

    def test_iso_utc_date_only_is_utc_midnight(self):
        self.assertEqual(m42._iso_utc("2026-09-10"), "2026-09-10T00:00:00Z")
        self.assertEqual(m42._iso_utc("2026-09-10T08:00:00+02:00"), "2026-09-10T06:00:00Z")
        with self.assertRaisesRegex(m42.M42Error, "invalid date/time"):
            m42._iso_utc("next tuesday")

    def test_verify_flag_and_list_services_user_flag_are_gone(self):
        code, _result, stderr = run_cli("setup", "--base-url", "https://x", "--verify")
        self.assertEqual(code, 2)
        self.assertIn("--verify", stderr)
        code, _result, stderr = run_cli("list-services", "--user", "someone")
        self.assertEqual(code, 2)


class ProfileValidationTests(unittest.TestCase):
    """L5, L6: the example profile is invalid by design; stricter checks."""

    def test_unedited_example_profile_is_rejected(self):
        example = Path(__file__).parents[1] / "references" / "tenant-profile.example.json"
        with self.assertRaisesRegex(m42.M42Error, "placeholder"):
            m42.validate_tenant_profile(json.loads(example.read_text(encoding="utf-8")))

    def test_example_host_in_portal_template_is_rejected(self):
        for host in ("helpdesk.example.com", "example.org", "EXAMPLE.NET"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(m42.M42Error, "documentation example host"):
                    m42.validate_tenant_profile(
                        {"portal_url_template": f"https://{host}/p/{{ticket_number}}"})
        profile = m42.validate_tenant_profile(
            {"portal_url_template": "https://helpdesk.example-corp.de/p/{ticket_number}"})
        self.assertTrue(profile["portal_url_template"].startswith("https://"))

    def test_placeholder_markers_anywhere_are_rejected(self):
        for profile in (
            {"ticket_prefixes": {"<UNSUPPORTED_PREFIX>": None}},
            {"close_reasons": {"solved": 1}, "behavior": {"operator_language": "<lang>"}},
            {"behavior": {"close_questions": ["<question>"]}},
        ):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(m42.M42Error, "placeholder"):
                    m42.validate_tenant_profile(profile)

    def test_duplicate_state_values_are_rejected(self):
        with self.assertRaisesRegex(m42.M42Error, "distinct value"):
            m42.validate_tenant_profile({"states": {"solved": 204, "closed": 204}})

    def test_role_id_is_stripped_and_must_be_a_guid(self):
        profile = m42.validate_tenant_profile({
            "roles": {"support": {"id": f" {USER_ID} ", "name": " Support "}},
            "role_assignment_attribute": "RecipientRole",
        })
        self.assertEqual(profile["roles"]["support"], {"id": USER_ID, "name": "Support"})

    def test_unknown_portal_placeholders_are_rejected(self):
        for template in (
            "https://portal.corp/{ticket_number}/{user}",
            "https://portal.corp/{ticket_number!r}",
            "https://portal.corp/{ticket_number:>5}",
            "https://portal.corp/{ticket_number",
            "https://portal.corp/tickets",
        ):
            with self.subTest(template=template):
                with self.assertRaises(m42.M42Error):
                    m42.validate_tenant_profile({"portal_url_template": template})

    def test_prefixes_that_can_never_match_are_rejected(self):
        for prefix in ("INC1", "IN C", "1"):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(m42.M42Error, "can never match"):
                    m42.validate_tenant_profile({"ticket_prefixes": {prefix: "incident"}})
        profile = m42.validate_tenant_profile({"ticket_prefixes": {"job-": "task"}})
        self.assertEqual(profile["ticket_prefixes"], {"JOB-": "task"})
        with self.assertRaisesRegex(m42.M42Error, "more than once"):
            m42.validate_tenant_profile({"ticket_prefixes": {"inc": "incident", "INC": "task"}})

    def test_state_change_journal_action_is_accepted(self):
        profile = m42.validate_tenant_profile({"journal_actions": {"state_change": 12}})
        self.assertEqual(profile["journal_actions"]["state_change"], 12)


class SetupWarningTests(unittest.TestCase):
    """L7: no misleading role warning for Recipient-based role assignment."""

    def test_recipient_roles_with_readable_inventory_do_not_claim_an_error(self):
        discovery = unavailable_discovery()
        discovery["roles"] = {"available": True, "rows": [
            {"ID": "r", "Name": "Support", "RoleId": "55555555-5555-5555-5555-555555555555"},
        ]}
        profile = configured_profile({"role_assignment_attribute": "Recipient"})
        warnings = m42._validate_profile_against_discovery(profile, discovery)
        self.assertFalse(any("could not live-verify roles" in w for w in warnings))
        self.assertTrue(any("Recipient" in w for w in warnings))

    def test_unreadable_role_inventory_still_warns_with_its_error(self):
        warnings = m42._validate_profile_against_discovery(
            configured_profile({"role_assignment_attribute": "Recipient"}),
            unavailable_discovery())
        self.assertTrue(any("could not live-verify roles: not readable" in w
                            for w in warnings))


class OutputEncodingTests(unittest.TestCase):
    """L9: stdout is reconfigured to UTF-8 at startup."""

    def test_main_reconfigures_streams(self):
        stream = mock.Mock()
        with mock.patch.object(m42.sys, "stdout", stream), \
                mock.patch.object(m42.sys, "stderr", stream):
            m42._configure_output_streams()
        self.assertEqual(stream.reconfigure.call_count, 2)
        stream.reconfigure.assert_called_with(encoding="utf-8", errors="backslashreplace")
        plain = object()
        with mock.patch.object(m42.sys, "stdout", plain), \
                mock.patch.object(m42.sys, "stderr", plain):
            m42._configure_output_streams()  # no reconfigure attribute: no error


class JournalFallbackTests(unittest.TestCase):
    """L10: the journal fallback also runs when the first query errors."""

    def test_fallback_runs_after_primary_query_error(self):
        client = mock.Mock()
        client.fragments.side_effect = [m42.M42Error("T() unsupported"), [{"ID": "j1"}]]
        rows = m42._ticket_journal_rows(client, "INC123", ACTIVITY_ID)
        self.assertEqual(rows, [{"ID": "j1"}])
        self.assertIn("[Expression-ObjectID]='11111111-1111-1111-1111-111111111111'",
                      client.fragments.call_args.kwargs["where"])

    def test_primary_error_is_raised_when_fallback_also_fails(self):
        client = mock.Mock()
        client.fragments.side_effect = [m42.M42Error("primary"), m42.M42Error("fallback")]
        with self.assertRaisesRegex(m42.M42Error, "primary"):
            m42._ticket_journal_rows(client, "INC123", ACTIVITY_ID)


class JournalHelperTests(unittest.TestCase):
    """L12, L13: journal results are dicts and the activity ID is passed through."""

    def test_journal_helper_reuses_known_activity_id(self):
        client = mock.Mock()
        client.tenant_profile = configured_profile()
        client.request.side_effect = [
            {"JournalId": JOURNAL_ID}, None,
            {"ID": JOURNAL_ID, "ActivityAction": 5, "VisibleInPortal": 0},
        ]
        with mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(m42, "_journal_entry_belongs_to_ticket", return_value=True), \
                mock.patch.object(m42, "_activity_id") as lookup:
            result = m42._gui_journal_entry(client, "INC123", "pause",
                                            activity_id=ACTIVITY_ID)
        lookup.assert_not_called()
        self.assertEqual(result, journal_result(JOURNAL_ID))
        self.assertEqual(client.request.call_args_list[0].kwargs["body"]["TargetObjectId"],
                         ACTIVITY_ID)

    def test_journal_warning_texts(self):
        self.assertEqual(m42._journal_warning(None), "journal entry was not created")
        self.assertIn("boom", m42._journal_warning(journal_result(None, False, "boom")))
        self.assertIn("not filled", m42._journal_warning(journal_result("x", False, "e")))
        self.assertIsNone(m42._journal_warning(journal_result()))
        self.assertEqual(m42._journal_entry_id(journal_result("x")), "x")
        self.assertIsNone(m42._journal_entry_id(None))

    def test_search_kb_fetches_bodies_only_for_matches(self):
        client = m42.Client("https://example.com", "token")
        calls = []

        def fragments(dd, where="", columns="ID", **kwargs):
            calls.append((where, columns))
            if "SolutionText" in columns:
                return [{"ID": "a", "SolutionText": "body-a"}]
            return [{"ID": "a", "Keywords": "vpn, mail"}, {"ID": "b", "Keywords": "printer"}]

        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", side_effect=fragments), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_search_kb(SimpleNamespace(tags="vpn", max=10))
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["articles"][0]["SolutionText"], "body-a")
        self.assertNotIn("SolutionText", calls[0][1])
        self.assertEqual(calls[1][0], "ID IN ('a')")


class SafetyPathTests(unittest.TestCase):
    """L15: confirmation refusals, closed guards, and main() error conversion."""

    def test_confirm_refusals_make_no_calls(self):
        client = mock.Mock()
        cases = [
            (m42.cmd_close_ticket, close_args(confirm=False)),
            (m42.cmd_reopen_ticket, SimpleNamespace(
                ticket_number="INC123", confirm=False, comment=None,
                no_auto_recipient=True, expected_timestamp=None)),
            (m42.cmd_delete_journal, SimpleNamespace(
                ticket_number="INC123", journal_id=JOURNAL_ID, force=True, confirm=False)),
        ]
        for command, args in cases:
            with self.subTest(command=command.__name__):
                client.reset_mock()
                with mock.patch.object(m42, "load_client", return_value=client), \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    with self.assertRaises(SystemExit):
                        command(args)
                self.assertIn("--confirm is required", json.loads(stdout.getvalue())["error"])
                client.single.assert_not_called()
                client.request.assert_not_called()

    def test_already_closed_guards(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        cases = [
            (m42.cmd_update_ticket, update_args(subject="x"), "already closed"),
            (m42.cmd_forward_ticket, SimpleNamespace(
                ticket_number="INC123", target="support", to_role=True, comment=None,
                expected_timestamp=None), "already closed"),
            (m42.cmd_close_ticket, close_args(), "already closed"),
        ]
        for command, args, expected in cases:
            with self.subTest(command=command.__name__):
                with mock.patch.object(m42, "load_client", return_value=client), \
                        mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                        mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                        mock.patch.object(m42, "_ticket_common_fragment",
                                          return_value={"CID": "c", "State": 204, "TimeStamp": "t"}), \
                        mock.patch.object(m42, "_record_close_work_time") as work, \
                        mock.patch.object(m42, "_fragment_put") as write, \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    with self.assertRaises(SystemExit):
                        command(args)
                self.assertIn(expected, json.loads(stdout.getvalue())["error"])
                write.assert_not_called()
                work.assert_not_called()

    def test_reopen_refuses_open_ticket(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": ACTIVITY_ID}), \
                mock.patch.object(m42, "_ticket_common_fragment",
                                  return_value={"CID": "c", "State": 202, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_fragment_put") as write, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                m42.cmd_reopen_ticket(SimpleNamespace(
                    ticket_number="INC123", confirm=True, comment=None,
                    no_auto_recipient=True, expected_timestamp=None))
        self.assertIn("is not closed (state=202)", json.loads(stdout.getvalue())["error"])
        write.assert_not_called()

    def test_tenant_mismatch_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            write_private(path, json.dumps({
                "base_url": "https://tenant-one.corp/m42Services", "api_token": "stored",
            }))
            with config_path_env(path), \
                    mock.patch.dict(os.environ, {"M42_BASE_URL": "https://tenant-two.corp",
                                                 "M42_API_TOKEN": ""}):
                with self.assertRaisesRegex(m42.M42Error, "selects a different tenant"):
                    m42.load_client()

    def test_main_converts_errors_to_json_and_exit_codes(self):
        with mock.patch.object(m42, "load_client", side_effect=m42.M42Error("no config")):
            code, result, _stderr = run_cli("whoami")
        self.assertEqual(code, 1)
        self.assertEqual(result, {"ok": False, "error": "no config"})
        with mock.patch.object(m42, "load_client", side_effect=RuntimeError("boom")):
            code, result, _stderr = run_cli("whoami")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "unexpected error: boom")
        with mock.patch.object(m42, "load_client",
                               side_effect=m42.M42Error("with extra", applied=["State"])):
            code, result, _stderr = run_cli("whoami")
        self.assertEqual(result["applied"], ["State"])
        code, result, stderr = run_cli("no-such-command")
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("invalid choice", stderr)

    def test_whoami_uses_client_token_for_expiry(self):
        client = m42.Client("https://example.com", "a.e30.x")
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_whoami(SimpleNamespace())
        self.assertEqual(json.loads(stdout.getvalue())["token_expiry"]["note"], "no exp claim")


if __name__ == "__main__":
    unittest.main()
