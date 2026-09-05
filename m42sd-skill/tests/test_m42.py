import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "m42.py"
SPEC = importlib.util.spec_from_file_location("m42_under_test", SCRIPT)
m42 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m42)

STATE_ROWS = [
    {"ID": "s1", "Value": 200, "DisplayString": "Neu"},
    {"ID": "s2", "Value": 201, "DisplayString": "Zugewiesen"},
    {"ID": "s3", "Value": 202, "DisplayString": "In Bearbeitung"},
    {"ID": "s4", "Value": 203, "DisplayString": "Angehalten"},
    {"ID": "s5", "Value": 204, "DisplayString": "Geschlossen"},
    {"ID": "s6", "Value": 205, "DisplayString": "Geplant"},
    {"ID": "s7", "Value": 220, "DisplayString": "Gelöst"},
]

TEST_PROFILE = {
    "schema_version": 1,
    "state_group": 7,
    "states": {
        "new": 200,
        "assigned": 201,
        "in_progress": 202,
        "paused": 203,
        "planned": 205,
        "solved": 220,
        "closed": 204,
    },
    "urgency": {"low": 3, "medium": 2, "high": 1},
    "urgency_default": "low",
    "impact_default": 3,
    "close_reasons": {"solved": 402},
    "journal_actions": {
        "forward_user": 2,
        "forward_role": 3,
        "processed": 4,
        "pause": 5,
        "takeover": 7,
        "close": 8,
        "reopen": 10,
        "resume": 29,
        "close_task": 70,
        "solved": 84,
    },
    "ticket_prefixes": {"INC": "incident", "TSK": "task"},
    "roles": {
        "support": {
            "id": "55555555-5555-5555-5555-555555555555",
            "name": "Support",
        }
    },
    "role_assignment_attribute": "RecipientRole",
    "portal_url_template": None,
    "behavior": {
        "auto_recipient_states": ["in_progress", "solved"],
        "auto_recipient_on_close": ["incident"],
        "auto_recipient_on_reopen": True,
        "forward_state": "assigned",
        "forward_preserve_states": ["assigned", "in_progress"],
        "reopen_state": "in_progress",
        "default_comment_visibility": "portal",
        "preclose_state_by_family": {"incident": "solved", "task": None},
        "processed_journal_families": ["incident"],
        "state_close_fallback_families": ["incident", "task"],
        "comment_language_mode": "initiator",
        "operator_language": None,
        "close_questions": [],
    },
}


def configured_profile(overrides=None):
    profile = json.loads(json.dumps(TEST_PROFILE))
    for section, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(profile.get(section), dict):
            profile[section].update(value)
        else:
            profile[section] = value
    return m42.validate_tenant_profile(profile)


def unavailable_discovery():
    missing = {
        "available": False,
        "data_definition": "unavailable",
        "rows": [],
        "error": "not readable",
    }
    return {
        "states": dict(missing),
        "urgency": dict(missing),
        "impact": dict(missing),
        "close_reasons": dict(missing),
        "journal_actions": dict(missing),
        "roles": dict(missing),
        "ticket_prefixes": {
            "available": False,
            "data_definition": m42.DD_ACTIVITY,
            "prefix_counts": {},
            "error": "not readable",
        },
    }


class StateClient:
    def fragments(self, dd, **kwargs):
        self.last_where = kwargs.get("where")
        return list(STATE_ROWS)


class M42Tests(unittest.TestCase):
    def test_token_exchange_uses_official_content_type_then_compatibility_fallback(self):
        client = m42.Client("https://example.com", "token")
        first = urllib.error.HTTPError(
            "https://example.com", 500, "unsupported", {}, io.BytesIO(b"bad type")
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"RawToken":"access"}'
        with mock.patch.object(
            m42.urllib.request, "urlopen", side_effect=[first, response]
        ) as urlopen:
            self.assertEqual(client._access(), "access")

        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(
            [request.get_header("Content-type") for request in requests],
            ["application/json;charset=UTF-8", "text/json"],
        )

    def test_retry_reports_retry_http_error(self):
        client = m42.Client("https://example.com", "token")
        first = urllib.error.HTTPError(
            "https://example.com", 401, "unauthorized", {}, io.BytesIO(b"expired")
        )
        second = urllib.error.HTTPError(
            "https://example.com", 403, "forbidden", {}, io.BytesIO(b"denied")
        )
        with mock.patch.object(client, "_access", return_value="fresh"), \
                mock.patch.object(
                    m42.urllib.request, "urlopen", side_effect=[first, second]
                ):
            with self.assertRaisesRegex(
                    m42.M42Error, "HTTP 403 on retry.*denied"):
                client._do("GET", "/api/test", "https://example.com/api/test",
                           None, {"Authorization": "Bearer stale"})

    def test_base_url_appends_service_path_exactly_once(self):
        self.assertEqual(
            m42.Client("https://example.com/m42Services", "token").base_url,
            "https://example.com/m42Services",
        )
        self.assertEqual(
            m42.Client("https://example.com", "token").base_url,
            "https://example.com/m42Services",
        )

    def test_base_url_rejects_plain_http_except_loopback(self):
        with self.assertRaisesRegex(m42.M42Error, "must use HTTPS"):
            m42.Client("http://example.com", "token")
        self.assertEqual(
            m42.Client("http://127.0.0.1:8080", "token").base_url,
            "http://127.0.0.1:8080/m42Services",
        )

    def test_tenant_profile_has_no_unreviewed_journal_action_defaults(self):
        profile = m42.validate_tenant_profile({
            "urgency": {"high": 99},
            "close_reasons": {"solved": 499},
            "journal_actions": {"pause": 55},
        })
        client = SimpleNamespace(tenant_profile=profile)
        self.assertEqual(m42._profile_value(client, "urgency", "high"), 99)
        self.assertEqual(m42._profile_value(client, "close_reasons", "solved"), 499)
        self.assertEqual(m42._journal_action_value(client, "pause"), 55)
        self.assertEqual(m42._journal_action_value(client, "processed"), 0)
        self.assertEqual(m42._journal_action_value(client, "resume"), 0)
        self.assertEqual(m42._journal_action_value(client, "close_task"), 0)

    def test_empty_profile_contains_no_tenant_value_defaults(self):
        profile = m42.validate_tenant_profile({})
        self.assertEqual(profile["states"], {})
        self.assertEqual(profile["urgency"], {})
        self.assertEqual(profile["close_reasons"], {})
        self.assertEqual(profile["journal_actions"], {})
        self.assertEqual(profile["ticket_prefixes"], {})
        self.assertEqual(profile["roles"], {})
        self.assertIsNone(profile["impact_default"])

    def test_created_descriptions_use_plain_text_without_html_formatting(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.created = []

            def request(self, method, path, **kwargs):
                self.created.append(kwargs["body"])
                return None

            def fragments(self, *args, **kwargs):
                return []

        client = Client()
        ticket_args = SimpleNamespace(
            user="user",
            subject="subject",
            description="Line 1\r\nLine <2>",
            category=None,
            type="incident",
            urgency="low",
        )
        problem_args = SimpleNamespace(
            user=None,
            subject="subject",
            description="Line 1\r\nLine <2>",
            urgency="low",
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(m42, "_resolve_user_arg", return_value="user-id"), \
                contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_create_ticket(ticket_args)
            m42.cmd_create_problem(problem_args)

        for body in client.created:
            self.assertEqual(
                body[m42.DD_ACTIVITY]["DescriptionHTML"],
                "Line 1\nLine &lt;2&gt;",
            )

    def test_numeric_state_is_validated_against_live_pickup(self):
        client = StateClient()
        self.assertEqual(m42._resolve_state_value(client, "202"), 202)
        with self.assertRaisesRegex(m42.M42Error, "not in live"):
            m42._resolve_state_value(client, "999999")

    def test_english_state_alias_resolves_live_localized_value(self):
        client = StateClient()
        client.tenant_profile = configured_profile()
        self.assertEqual(m42._resolve_state_value(client, "paused"), 203)

    def test_profile_maps_custom_state_but_still_validates_live_value(self):
        class Client:
            tenant_profile = configured_profile({"states": {"closed": 999}})

            def fragments(self, *args, **kwargs):
                return [{"ID": "s", "Value": 999, "DisplayString": "Archived Final"}]

        self.assertEqual(m42._resolve_semantic_state(Client(), "closed"), 999)
        self.assertEqual(m42._closed_state_values(Client()), {999})
        self.assertEqual(m42._resolve_state_value(Client(), "closed"), 999)

    def test_journal_pair_has_no_cross_ticket_default(self):
        class EmptyJournal:
            def fragments(self, *args, **kwargs):
                return []

        with self.assertRaisesRegex(m42.M42Error, "refusing unsafe cross-ticket"):
            m42._journal_type_pair(EmptyJournal(), "SRQ123")

    def test_journal_entry_is_not_filled_until_target_ownership_is_verified(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.requests = []

            def request(self, method, path, **kwargs):
                self.requests.append((method, path))
                return {"JournalId": "33333333-3333-3333-3333-333333333333"}

        client = Client()
        with mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(m42, "_activity_id", return_value="activity"), \
                mock.patch.object(
                    m42, "_journal_entry_belongs_to_ticket", return_value=False
                ):
            result = m42._gui_journal_entry(client, "INC123", "pause")
        self.assertIn("unfilled:", result)
        self.assertEqual(client.requests, [("POST", "/api/journal/add")])

    def test_add_comment_without_target_pair_uses_object_update_fallback(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.requests = []
                self.object = None

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def fragments(self, *args, **kwargs):
                return []

            def request(self, method, path, **kwargs):
                self.requests.append((method, path))
                if path.startswith(f"/api/data/fragments/{m42.DD_ACTIVITY}/"):
                    return {
                        "UsedInTypeSPSActivityTypeIncident":
                            "44444444-4444-4444-4444-444444444444"
                    }
                if method == "GET":
                    self.object = {m42.DD_JOURNAL: []}
                    return self.object
                return None

        client = Client()
        args = SimpleNamespace(
            ticket_number="INC123",
            text="Summary\n- fixed <issue>",
            internal=True,
        )
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client):
            with contextlib.redirect_stdout(stdout):
                m42.cmd_add_comment(args)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["path"], "objects.update")
        self.assertEqual(
            [method for method, _path in client.requests], ["GET", "GET", "PUT"]
        )
        self.assertEqual(
            client.object[m42.DD_JOURNAL][0]["OriginalSolutionHtml"],
            "Summary\n- fixed &lt;issue&gt;",
        )

    def test_add_comment_primary_path_writes_plain_text_portal_comment(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.bodies = []

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def request(self, method, path, **kwargs):
                self.bodies.append(kwargs.get("body"))
                if method == "POST":
                    return {"JournalId": "33333333-3333-3333-3333-333333333333"}
                return None

        client = Client()
        args = SimpleNamespace(
            ticket_number="INC123",
            text="Summary\r\n- fixed <issue>",
            internal=False,
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(
                    m42, "_journal_entry_belongs_to_ticket", return_value=True
                ), \
                contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_add_comment(args)

        self.assertEqual(
            client.bodies[1]["OriginalSolutionHtml"],
            "Summary\n- fixed &lt;issue&gt;",
        )
        self.assertEqual(client.bodies[1]["VisibleInPortal"], 1)

    def test_add_comment_uses_configured_default_visibility(self):
        class Client:
            tenant_profile = configured_profile({
                "behavior": {"default_comment_visibility": "internal"}
            })

            def __init__(self):
                self.bodies = []

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def request(self, method, path, **kwargs):
                self.bodies.append(kwargs.get("body"))
                if method == "POST":
                    return {"JournalId": "33333333-3333-3333-3333-333333333333"}
                return None

        client = Client()
        args = SimpleNamespace(
            ticket_number="INC123", text="Internal update", internal=False
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(
                    m42, "_journal_entry_belongs_to_ticket", return_value=True
                ), contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_add_comment(args)

        self.assertEqual(client.bodies[1]["VisibleInPortal"], 0)

    def test_forward_passes_plain_text_note_to_journal_helper(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

        client = Client()
        calls = []
        fragment_put = mock.Mock()
        args = SimpleNamespace(
            ticket_number="INC123",
            target="support",
            to_role=True,
            comment=None,
        )
        patches = (
            mock.patch.object(m42, "load_client", return_value=client),
            mock.patch.object(
                m42,
                "_ticket_common_fragment",
                return_value={"State": 201, "CID": "c", "TimeStamp": "t"},
            ),
            mock.patch.object(m42, "_closed_state_values", return_value={204}),
            mock.patch.object(
                m42,
                "_resolve_semantic_state",
                side_effect=lambda _c, name: {"assigned": 201, "in_progress": 202}[name],
            ),
            mock.patch.object(
                m42, "_semantic_for_state_value", return_value="assigned"
            ),
            mock.patch.object(m42, "_fragment_put", fragment_put),
            mock.patch.object(m42, "_activity_time_stamp", return_value="t"),
            mock.patch.object(
                m42,
                "_gui_journal_entry",
                side_effect=lambda *values, **kwargs: calls.append(
                    (values, kwargs)
                ) or "jid",
            ),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with contextlib.redirect_stdout(io.StringIO()):
                m42.cmd_forward_ticket(args)
        values, kwargs = calls[0]
        self.assertEqual(values[1:3], ("INC123", "forward_role"))
        self.assertIn("Support", values[3])
        self.assertNotIn("<p>", values[3])
        self.assertNotIn("<br>", values[3])
        self.assertEqual(values[3], "Forwarded to role: Support")
        self.assertEqual(kwargs, {"portal": 0})
        fragment_put.assert_called_once_with(
            client,
            m42.DD_ACTIVITY,
            {
                "ID": "11111111-1111-1111-1111-111111111111",
                "TimeStamp": "t",
                "RecipientRole": "55555555-5555-5555-5555-555555555555",
            },
        )

    def test_state_update_passes_ticket_number_to_journal_helper(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111",
                        "TimeStamp": "t"}

        client = Client()
        calls = []
        args = SimpleNamespace(
            ticket_number="INC123",
            state="paused",
            recipient=None,
            auto_recipient=False,
            no_auto_recipient=False,
            subject=None,
            urgency=None,
            priority=None,
            category=None,
            resume_at=None,
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42,
                    "_ticket_common_fragment",
                    return_value={"State": 202, "CID": "c", "TimeStamp": "t"},
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(m42, "_resolve_state_value", return_value=203), \
                mock.patch.object(
                    m42,
                    "_semantic_for_state_value",
                    side_effect=lambda _c, value: {202: "in_progress", 203: "paused"}[value],
                ), \
                mock.patch.object(m42, "_fragment_put"), \
                mock.patch.object(
                    m42,
                    "_gui_journal_entry",
                    side_effect=lambda *values, **kwargs: calls.append(
                        (values, kwargs)
                    ) or "jid",
                ):
            with contextlib.redirect_stdout(io.StringIO()):
                m42.cmd_update_ticket(args)
        self.assertEqual(
            calls,
            [((client, "INC123", "pause", None), {"portal": 0})],
        )

    def test_unlabeled_state_update_adds_explicit_internal_journal_entry(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111",
                        "TimeStamp": "t"}

        client = Client()
        calls = []
        args = SimpleNamespace(
            ticket_number="INC123",
            state="planned",
            recipient=None,
            auto_recipient=False,
            no_auto_recipient=False,
            subject=None,
            urgency=None,
            priority=None,
            category=None,
            resume_at=None,
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42,
                    "_ticket_common_fragment",
                    return_value={"State": 202, "CID": "c", "TimeStamp": "t"},
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(m42, "_resolve_state_value", return_value=205), \
                mock.patch.object(
                    m42,
                    "_semantic_for_state_value",
                    side_effect=lambda _c, value: {
                        202: "in_progress",
                        205: "planned",
                    }[value],
                ), \
                mock.patch.object(m42, "_fragment_put"), \
                mock.patch.object(
                    m42,
                    "_gui_journal_entry",
                    side_effect=lambda *values, **kwargs: calls.append(
                        (values, kwargs)
                    ) or "jid",
                ):
            with contextlib.redirect_stdout(io.StringIO()):
                m42.cmd_update_ticket(args)

        self.assertEqual(
            calls,
            [((client, "INC123", "state_change", "State changed to planned."),
              {"portal": 0})],
        )

    def test_delete_journal_rejects_entry_owned_by_another_ticket(self):
        class Client:
            def __init__(self):
                self.requests = []

            def fragments(self, *args, **kwargs):
                return []

            def single(self, *args, **kwargs):
                return {"ID": "22222222-2222-2222-2222-222222222222"}

            def request(self, *args, **kwargs):
                self.requests.append((args, kwargs))

        client = Client()
        args = SimpleNamespace(
            confirm=True,
            ticket_number="INC123",
            journal_id="22222222-2222-2222-2222-222222222222",
            force=False,
        )
        with mock.patch.object(m42, "load_client", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m42.cmd_delete_journal(args)
        self.assertEqual(client.requests, [])

    def test_setup_repairs_existing_config_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            profile_path = Path(directory) / "tenant-profile.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            profile_path.write_text(json.dumps(TEST_PROFILE), encoding="utf-8")
            args = SimpleNamespace(
                token="a.e30.x",
                base_url="https://example.com",
                profile_file=str(profile_path),
                verify=False,
            )
            with mock.patch.object(m42, "CONFIG_PATH", str(path)), \
                    mock.patch.object(m42.Client, "_access", return_value="access"), \
                    mock.patch.object(
                        m42, "_discover_tenant", return_value=unavailable_discovery()
                    ):
                with contextlib.redirect_stdout(io.StringIO()):
                    m42.cmd_setup(args)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["api_token"], "a.e30.x")
            self.assertEqual(
                stored["tenant_profile"]["behavior"]["comment_language_mode"],
                "initiator",
            )
            self.assertIn("reviewed_at", stored["tenant_review"])

    def test_setup_without_profile_returns_discovery_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            args = SimpleNamespace(
                token="a.e30.x",
                base_url="https://example.com",
                profile_file=None,
                verify=False,
            )
            stdout = io.StringIO()
            with mock.patch.object(m42, "CONFIG_PATH", str(path)), \
                    mock.patch.object(m42.Client, "_access", return_value="access"), \
                    mock.patch.object(
                        m42, "_discover_tenant", return_value=unavailable_discovery()
                    ), contextlib.redirect_stdout(stdout):
                m42.cmd_setup(args)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["configured"])
            self.assertIn("questions", result)
            self.assertTrue(all(
                value is None
                for value in result["profile_template"]["states"].values()
            ))
            self.assertFalse(path.exists())

    def test_tenant_discovery_reads_configurable_values_and_only_prefix_counts(self):
        class Client:
            def fragments(self, dd, **kwargs):
                if dd == m42.DD_STATE:
                    return [{
                        "ID": "state-id",
                        "Value": 17,
                        "DisplayString": "Ready",
                        "StateGroup": 9,
                    }]
                if dd == m42.DD_URGENCY:
                    return [{"ID": "u", "Value": 23, "DisplayString": "Normal"}]
                if dd == m42.DD_IMPACT:
                    return [{"ID": "i", "Value": 31, "DisplayString": "Local"}]
                if dd == m42.DD_CLOSE_REASON:
                    return [{"ID": "r", "Value": 47, "DisplayString": "Complete"}]
                if dd == m42.DD_JOURNAL_TYPE:
                    return [{"ID": "j", "Value": 53, "DisplayString": "Closed"}]
                if dd == m42.DD_SECURITY_ROLE:
                    return [{
                        "ID": "security-role",
                        "Name": "Support",
                        "ShowInForwardAction": 1,
                        "RoleId": "55555555-5555-5555-5555-555555555555",
                    }]
                if dd == m42.DD_ACTIVITY:
                    return [
                        {"ID": "a", "TicketNumber": "CASE0001"},
                        {"ID": "b", "TicketNumber": "CASE0002"},
                        {"ID": "c", "TicketNumber": "JOB-9"},
                    ]
                raise AssertionError(dd)

        discovery = m42._discover_tenant(Client())
        self.assertEqual(
            discovery["ticket_prefixes"]["prefix_counts"],
            {"CASE": 2, "JOB-": 1},
        )
        serialized = json.dumps(discovery)
        self.assertNotIn("CASE0001", serialized)
        self.assertEqual(discovery["close_reasons"]["rows"][0]["Value"], 47)
        self.assertEqual(discovery["roles"]["rows"][0]["Name"], "Support")

    def test_setup_requires_every_behavior_answer(self):
        raw = json.loads(json.dumps(TEST_PROFILE))
        del raw["behavior"]["default_comment_visibility"]
        reviewed = m42.validate_tenant_profile(raw)
        with self.assertRaisesRegex(m42.M42Error, "missing behavior answers"):
            m42._validate_setup_answers(raw, reviewed, unavailable_discovery())

    def test_tenant_config_never_outputs_api_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            path.write_text(json.dumps({
                "base_url": "https://example.com/m42Services",
                "api_token": "secret-token-value",
                "tenant_profile": TEST_PROFILE,
                "tenant_review": {"reviewed_at": "2026-01-01T00:00:00Z"},
            }), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(m42, "CONFIG_PATH", str(path)), \
                    mock.patch.dict(
                        os.environ,
                        {"M42_BASE_URL": "", "M42_API_TOKEN": "",
                         "M42_TENANT_PROFILE_FILE": ""},
                        clear=False,
                    ), contextlib.redirect_stdout(stdout):
                m42.cmd_tenant_config(SimpleNamespace())
            output = stdout.getvalue()
            self.assertNotIn("secret-token-value", output)
            self.assertEqual(
                json.loads(output)["tenant_profile"]["ticket_prefixes"]["INC"],
                "incident",
            )

    def test_environment_tenant_does_not_reuse_stored_tenant_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m42_config.json"
            path.write_text(json.dumps({
                "base_url": "https://tenant-one.example.com/m42Services",
                "api_token": "stored-token",
                "tenant_profile": TEST_PROFILE,
            }), encoding="utf-8")
            with mock.patch.object(m42, "CONFIG_PATH", str(path)), \
                    mock.patch.dict(os.environ, {
                        "M42_BASE_URL": "https://tenant-two.example.com",
                        "M42_API_TOKEN": "other-token",
                        "M42_TENANT_PROFILE_FILE": "",
                    }, clear=False):
                client = m42.load_client()

            self.assertEqual(client.profile_source, "empty")
            self.assertEqual(client.tenant_profile["states"], {})

    def test_reopen_passes_unformatted_text_to_journal_writer(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

        client = Client()
        journal_calls = []
        args = SimpleNamespace(
            confirm=True,
            ticket_number="INC123",
            comment="<script>alert(1)</script>",
            no_auto_recipient=True,
        )
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42,
                    "_ticket_common_fragment",
                    return_value={"State": 204, "CID": "c", "TimeStamp": "t"},
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(m42, "_resolve_semantic_state", return_value=202), \
                mock.patch.object(m42, "_fragment_put"), \
                mock.patch.object(
                    m42,
                    "_gui_journal_entry",
                    side_effect=lambda *values, **kwargs: journal_calls.append(
                        (values, kwargs)
                    ) or "jid",
                ):
            with contextlib.redirect_stdout(io.StringIO()):
                m42.cmd_reopen_ticket(args)
        self.assertEqual(journal_calls[0][0][3], "<script>alert(1)</script>")
        self.assertEqual(journal_calls[0][1], {"portal": 0})

    def test_close_uses_profile_reason_and_live_closed_state(self):
        class Client:
            tenant_profile = configured_profile({
                "close_reasons": {"solved": 499}
            })

            def __init__(self):
                self.bodies = []

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def request(self, method, path, **kwargs):
                self.bodies.append(kwargs.get("body"))
                return None

        client = Client()
        common_rows = [
            {"State": 202, "CID": "c", "TimeStamp": "t"},
            {"State": 999, "CID": "c", "TimeStamp": "t2"},
        ]
        args = SimpleNamespace(
            confirm=True,
            ticket_number="INC123",
            comment="done",
            reason="solved",
            kb=None,
            notify_initiator=False,
            no_auto_recipient=True,
            work_minutes=15,
        )
        processed_entry = mock.Mock(return_value="processed-jid")
        close_entry = mock.Mock(return_value="closed-jid")
        work_entry = mock.Mock(return_value="work-jid")
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42, "_ticket_common_fragment", side_effect=common_rows
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={999}), \
                mock.patch.object(
                    m42, "_resolve_semantic_state", return_value=220
                ), \
                mock.patch.object(
                    m42, "_gui_journal_entry", processed_entry
                ), \
                mock.patch.object(
                    m42, "_record_close_work_time", work_entry
                ), \
                mock.patch.object(m42, "_close_journal_entry", close_entry):
            with contextlib.redirect_stdout(io.StringIO()):
                m42.cmd_close_ticket(args)
        self.assertEqual(client.bodies[0]["Reason"], 499)
        self.assertEqual(client.bodies[0]["Comments"], "done")
        work_entry.assert_called_once_with(
            client, "11111111-1111-1111-1111-111111111111", 15
        )
        processed_entry.assert_called_once_with(
            client, "INC123", "processed", portal=0
        )
        close_entry.assert_called_once_with(
            client, "INC123", "done", portal=0, close_reason=499,
            family="incident",
        )

    def test_task_close_fallback_skips_incident_processing_state(self):
        class Client:
            tenant_profile = configured_profile()

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def request(self, method, path, **kwargs):
                raise m42.M42Error("close endpoint rejected")

        client = Client()
        state_bodies = []
        args = SimpleNamespace(
            confirm=True,
            ticket_number="TSK123",
            comment="Summary",
            reason="solved",
            kb=None,
            notify_initiator=False,
            no_auto_recipient=True,
            work_minutes=5,
        )

        def capture_fragment(_client, dd, body):
            if dd == m42.DD_COMMON:
                state_bodies.append(body)

        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42,
                    "_ticket_common_fragment",
                    side_effect=lambda *_: {
                        "State": state_bodies[-1]["State"] if state_bodies else 202,
                        "CID": "c", "TimeStamp": "t",
                    },
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(
                    m42,
                    "_resolve_semantic_state",
                    side_effect=lambda _c, name: {"closed": 204, "solved": 220}[name],
                ), \
                mock.patch.object(m42, "_fragment_put", side_effect=capture_fragment), \
                mock.patch.object(
                    m42, "_record_close_work_time", return_value="work-jid"
                ), \
                mock.patch.object(
                    m42, "_close_journal_entry", return_value="closed-jid"
                ), \
                contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_close_ticket(args)

        self.assertEqual([body["State"] for body in state_bodies], [204])

    def test_close_stops_when_state_fallback_is_not_reviewed(self):
        class Client:
            tenant_profile = configured_profile({
                "behavior": {"state_close_fallback_families": []}
            })

            def single(self, *args, **kwargs):
                return {"ID": "11111111-1111-1111-1111-111111111111"}

            def request(self, method, path, **kwargs):
                raise m42.M42Error("endpoint rejected")

        client = Client()
        args = SimpleNamespace(
            confirm=True,
            ticket_number="INC123",
            comment="Summary",
            reason="solved",
            kb=None,
            notify_initiator=False,
            no_auto_recipient=True,
            work_minutes=0,
        )
        fragment_put = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(
                    m42,
                    "_ticket_common_fragment",
                    return_value={"State": 202, "CID": "c", "TimeStamp": "t"},
                ), \
                mock.patch.object(m42, "_closed_state_values", return_value={204}), \
                mock.patch.object(m42, "_resolve_semantic_state", return_value=220), \
                mock.patch.object(m42, "_record_close_work_time", return_value=None), \
                mock.patch.object(m42, "_fragment_put", fragment_put), \
                contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
            m42.cmd_close_ticket(args)

        self.assertIn("does not allow state-close fallback", stdout.getvalue())
        fragment_put.assert_not_called()

    def test_close_work_time_links_owner_after_create_and_verifies_it(self):
        activity_id = "11111111-1111-1111-1111-111111111111"
        owner_id = "22222222-2222-2222-2222-222222222222"
        entry_id = "33333333-3333-3333-3333-333333333333"
        user_id = "44444444-4444-4444-4444-444444444444"

        class Client:
            def __init__(self):
                self.requests = []
                self.time_entry_linked = False

            def single(self, dd, *args, **kwargs):
                if dd == m42.DD_TIME_TRACKING_CONFIG:
                    return {
                        "TicketsClosureActivityType": 4,
                        "SupportedActivityCiTypes": "SPSActivityTypeIncident",
                    }
                return None

            def fragments(self, dd, *args, **kwargs):
                if dd == m42.DD_TIME_ACTIVITY_TYPE:
                    return [{"Value": 4, "DisplayString": "Lösung"}]
                return []

            def request(self, method, path, **kwargs):
                self.requests.append((method, path, kwargs.get("body")))
                if method == "GET" and path.endswith(f"/{activity_id}"):
                    return {"UsedInTypeSPSActivityTypeIncident": owner_id}
                if method == "POST":
                    return entry_id
                if method == "GET" and path.endswith(f"/{entry_id}"):
                    if self.time_entry_linked:
                        return {
                            "TimeStamp": "updated-stamp",
                            "UsedInTypeSPSActivityTypeIncident": owner_id,
                        }
                    return {"TimeStamp": "created-stamp"}
                if method == "PUT":
                    body = kwargs.get("body") or {}
                    if "UsedInTypeSPSActivityTypeBase" in body:
                        raise m42.M42Error("FK violation: incident is not an activity-base row")
                    self.time_entry_linked = body == {
                        "ID": entry_id,
                        "TimeStamp": "created-stamp",
                        "UsedInTypeSPSActivityTypeIncident": owner_id,
                    }
                    return None
                return None

        client = Client()
        end = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(m42, "_current_identity", return_value=user_id):
            result = m42._record_close_work_time(
                client, activity_id, 15.5, end=end
            )

        self.assertEqual(result, entry_id)
        post = next(call for call in client.requests if call[0] == "POST")
        self.assertEqual(post[1], f"/api/data/fragments/{m42.DD_TIME_TRACKING}")
        self.assertEqual(
            post[2],
            {
                "CreatedDate": "2026-09-04T12:00:00Z",
                "Begin": "2026-09-04T11:44:30Z",
                "End": "2026-09-04T12:00:00Z",
                "Minutes": 15.5,
                "ActivityType": 4,
                "User": user_id,
            },
        )
        put = next(call for call in client.requests if call[0] == "PUT")
        self.assertEqual(
            put,
            (
                "PUT",
                f"/api/data/fragments/{m42.DD_TIME_TRACKING}",
                {
                    "ID": entry_id,
                    "TimeStamp": "created-stamp",
                    "UsedInTypeSPSActivityTypeIncident": owner_id,
                },
            ),
        )

    def test_zero_close_work_minutes_creates_no_fragment(self):
        client = mock.Mock()
        self.assertIsNone(m42._record_close_work_time(client, "activity", 0))
        client.assert_not_called()

    def test_close_cli_requires_work_minutes_answer(self):
        stderr = io.StringIO()
        argv = [
            "m42.py",
            "close-ticket",
            "--ticket-number",
            "INC123",
            "--reason",
            "solved",
            "--comment",
            "Summary",
            "--confirm",
        ]
        with mock.patch.object(m42.sys, "argv", argv), \
                mock.patch.object(m42, "load_client") as load_client, \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            m42.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--work-minutes", stderr.getvalue())
        load_client.assert_not_called()

    def test_close_entry_uses_family_specific_internal_action(self):
        journal_entry = mock.Mock(return_value="jid")
        client = object()

        with mock.patch.object(m42, "_gui_journal_entry", journal_entry):
            m42._close_journal_entry(
                client, "TSK123", "Task summary", close_reason=402,
                family="task",
            )
            m42._close_journal_entry(
                client, "INC123", "Incident summary", close_reason=402,
                family="incident",
            )

        self.assertEqual(
            journal_entry.call_args_list,
            [
                mock.call(
                    client,
                    "TSK123",
                    "close_task",
                    "Task summary",
                    0,
                    close_reason=402,
                ),
                mock.call(client, "INC123", "close", "Incident summary", 0),
            ],
        )

    def test_nonstandard_prefix_uses_configured_task_family(self):
        profile = configured_profile({"ticket_prefixes": {"JOB-": "task"}})
        client = SimpleNamespace(tenant_profile=profile)
        journal_entry = mock.Mock(return_value="jid")

        with mock.patch.object(m42, "_gui_journal_entry", journal_entry):
            m42._close_journal_entry(
                client, "JOB-17", "Task summary", close_reason=402
            )

        journal_entry.assert_called_once_with(
            client,
            "JOB-17",
            "close_task",
            "Task summary",
            0,
            close_reason=402,
        )

    def test_task_close_entry_includes_native_gui_reason_metadata(self):
        journal_id = "33333333-3333-3333-3333-333333333333"

        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.bodies = []

            def request(self, method, path, **kwargs):
                body = kwargs.get("body")
                self.bodies.append((method, path, body))
                if method == "POST":
                    return {"JournalId": journal_id}
                return None

        client = Client()
        with mock.patch.object(
            m42, "_journal_type_pair", return_value=("type-id", "used-in")
        ), mock.patch.object(
            m42, "_activity_id", return_value="activity-id"
        ), mock.patch.object(
            m42, "_journal_entry_belongs_to_ticket", return_value=True
        ):
            result = m42._close_journal_entry(
                client,
                "TSK123",
                "Task <summary>\n- done",
                close_reason=402,
            )

        self.assertEqual(result, journal_id)
        body = next(call[2] for call in client.bodies if call[0] == "PUT")
        self.assertEqual(body["ActivityAction"], 70)
        self.assertEqual(body["VisibleInPortal"], 0)
        self.assertEqual(body["OriginalSolution"], "Task <summary>\n- done")
        self.assertEqual(
            body["OriginalSolutionHtml"], "Task &lt;summary&gt;\n- done"
        )
        self.assertIn('name="closeReason"', body["SolutionParams"])
        self.assertIn('value="402"', body["SolutionParams"])
        self.assertNotIn("closeErrorType", body["SolutionParams"])
        self.assertNotIn("<p>", body["OriginalSolutionHtml"])

    def test_pause_entry_uses_recognizable_internal_activity_action(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.bodies = []

            def request(self, method, path, **kwargs):
                body = kwargs.get("body")
                self.bodies.append(body)
                if method == "POST":
                    return {"JournalId": "33333333-3333-3333-3333-333333333333"}
                return None

        client = Client()
        with mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(m42, "_activity_id", return_value="activity"), \
                mock.patch.object(
                    m42, "_journal_entry_belongs_to_ticket", return_value=True
                ):
            result = m42._gui_journal_entry(client, "INC123", "pause")

        self.assertEqual(result, "33333333-3333-3333-3333-333333333333")
        self.assertEqual(client.bodies[1]["ActivityAction"], 5)
        self.assertEqual(client.bodies[1]["VisibleInPortal"], 0)
        self.assertNotIn("OriginalSolutionHtml", client.bodies[1])

    def test_journal_writer_escapes_markup_without_adding_formatting_tags(self):
        class Client:
            tenant_profile = configured_profile()

            def __init__(self):
                self.bodies = []

            def request(self, method, path, **kwargs):
                self.bodies.append(kwargs.get("body"))
                if method == "POST":
                    return {"JournalId": "33333333-3333-3333-3333-333333333333"}
                return None

        client = Client()
        with mock.patch.object(m42, "_journal_type_pair", return_value=("type", "used")), \
                mock.patch.object(m42, "_activity_id", return_value="activity"), \
                mock.patch.object(
                    m42, "_journal_entry_belongs_to_ticket", return_value=True
                ):
            m42._gui_journal_entry(
                client,
                "INC123",
                "reopen",
                "Summary\r\n- fixed <script>alert(1)</script>",
            )

        self.assertEqual(
            client.bodies[1]["OriginalSolutionHtml"],
            "Summary\n- fixed &lt;script&gt;alert(1)&lt;/script&gt;",
        )

    def test_ownerless_account_is_not_used_as_person(self):
        class Client:
            def fragments(self, dd, *args, **kwargs):
                if dd == m42.DD_ACCOUNT:
                    return [{"ID": "11111111-1111-1111-1111-111111111111",
                             "OwnerId": None}]
                return []

        with self.assertRaisesRegex(m42.M42Error, "no usable Person owner"):
            m42._resolve_user_or_fail(Client(), "ownerless")

    def test_announcement_with_later_timestamp_today_remains_active(self):
        until = datetime.now(timezone.utc) + timedelta(hours=1)

        class Client:
            def fragments(self, *args, **kwargs):
                return [{
                    "ID": "a1",
                    "Subject": "Active",
                    "Visible": 1,
                    "VisibleFrom": None,
                    "VisibleUntil": until.isoformat(),
                }]

        stdout = io.StringIO()
        with mock.patch.object(m42, "load_client", return_value=Client()):
            with contextlib.redirect_stdout(stdout):
                m42.cmd_announcements(SimpleNamespace())
        self.assertEqual(json.loads(stdout.getvalue())["count"], 1)


if __name__ == "__main__":
    unittest.main()
