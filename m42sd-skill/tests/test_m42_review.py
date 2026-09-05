import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from test_m42 import STATE_ROWS, configured_profile, m42, unavailable_discovery


def update_args(**overrides):
    values = dict(
        ticket_number="CASE123", state=None, recipient=None,
        auto_recipient=False, no_auto_recipient=False, subject=None,
        urgency=None, priority=None, category=None, resume_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def close_args(**overrides):
    values = dict(
        ticket_number="INC123", confirm=True, comment="Test solution",
        reason="solved", work_minutes=15, kb=None,
        notify_initiator=False, no_auto_recipient=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ReviewTests(unittest.TestCase):
    def test_setup_can_explicitly_disable_unsupported_ticket_family(self):
        profile = configured_profile({"ticket_prefixes": {"CHANGE-": None}})
        discovery = unavailable_discovery()
        discovery["ticket_prefixes"] = {
            "available": True, "prefix_counts": {"INC": 1, "CHANGE-": 1},
        }
        m42._validate_setup_answers(profile, profile, discovery)
        with self.assertRaisesRegex(m42.M42Error, "no ticket family"):
            m42._ticket_family(SimpleNamespace(tenant_profile=profile), "CHANGE-17")
        del profile["ticket_prefixes"]["CHANGE-"]
        with self.assertRaisesRegex(m42.M42Error, "ticket prefixes"):
            m42._validate_setup_answers(profile, profile, discovery)

    def test_setup_rejects_role_when_readable_inventory_is_empty(self):
        discovery = unavailable_discovery()
        discovery["roles"] = {"available": True, "rows": []}
        with self.assertRaisesRegex(m42.M42Error, "not live forward roles"):
            m42._validate_profile_against_discovery(configured_profile(), discovery)

    def test_attachment_filter_quotes_custom_ticket_prefix(self):
        client = mock.Mock()
        client.single.return_value = {"ID": "activity"}
        client.fragments.return_value = []
        with mock.patch.object(m42, "load_client", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            m42.cmd_attachments(SimpleNamespace(ticket_number="O'CASE17"))
        self.assertEqual(
            client.fragments.call_args.kwargs["where"],
            "T(SPSActivityClassBase).TicketNumber='O''CASE17'",
        )

    def test_pagination_rejects_repeated_full_page(self):
        client = m42.Client("https://example.com", "token")
        page = [{"ID": "one"}, {"ID": "two"}]
        with mock.patch.object(client, "request", side_effect=[page, page]) as request:
            with self.assertRaisesRegex(m42.M42Error, "pagination.*progress"):
                client.fragments("Example", page_size=2, max_records=10)
        self.assertEqual(request.call_count, 2)

    def test_pagination_preserves_overlap_and_final_short_page(self):
        client = m42.Client("https://example.com", "token")
        with mock.patch.object(client, "request", side_effect=[
            [{"ID": "one"}, {"ID": "two"}],
            [{"ID": "two"}, {"ID": "three"}],
            [{"ID": "four"}],
        ]):
            rows = client.fragments("Example", page_size=2, max_records=10)
        self.assertEqual([row["ID"] for row in rows], ["one", "two", "three", "four"])

    def test_state_inventory_reused_only_within_client_and_group(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(client, "fragments", return_value=STATE_ROWS) as read:
            self.assertEqual(m42._closed_state_values(client), {204})
            self.assertEqual(m42._resolve_state_value(client, "in_progress"), 202)
            self.assertEqual(m42._semantic_for_state_value(client, 202), "in_progress")
            self.assertEqual(read.call_count, 1)
            client.tenant_profile["state_group"] = 9
            m42._closed_state_values(client)
            self.assertEqual(read.call_count, 2)
        other = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(other, "fragments", return_value=STATE_ROWS) as read:
            m42._closed_state_values(other)
            read.assert_called_once()

    def test_configured_state_semantic_wins_over_conflicting_live_label(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        rows = [*STATE_ROWS, {"ID": "other", "Value": 999, "DisplayString": "in progress"}]
        with mock.patch.object(client, "fragments", return_value=rows):
            self.assertEqual(m42._resolve_state_value(client, "in progress"), 202)

    def test_invalid_update_arguments_make_no_writes(self):
        for overrides in (
            {"recipient": "someone", "auto_recipient": True},
            {"auto_recipient": True, "no_auto_recipient": True},
            {"urgency": "unconfigured"},
            {"resume_at": "not-a-date"},
            {"category": "missing"},
        ):
            with self.subTest(overrides=overrides):
                client = m42.Client("https://example.com", "token", configured_profile())
                with mock.patch.object(m42, "load_client", return_value=client), \
                        mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                        mock.patch.object(client, "single", return_value={"ID": "act", "TimeStamp": "t"}), \
                        mock.patch.object(m42, "_ticket_common_fragment", return_value={"CID": "common", "State": 200, "TimeStamp": "t"}), \
                        mock.patch.object(m42, "_resolve_category_name", side_effect=m42.M42Error("missing category")), \
                        mock.patch.object(m42, "_current_identity", return_value="actor"), \
                        mock.patch.object(m42, "_fragment_put") as write, \
                        mock.patch.object(m42, "_gui_journal_entry") as journal, \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises((m42.M42Error, SystemExit)):
                        m42.cmd_update_ticket(update_args(state="in_progress", **overrides))
                    write.assert_not_called()
                    journal.assert_not_called()

    def test_update_combines_activity_fields_and_explicit_recipient(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": "act", "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment", return_value={"CID": "common", "State": 200, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_resolve_user_arg", return_value="selected-user"), \
                mock.patch.object(m42, "_current_identity") as identity, \
                mock.patch.object(m42, "_fragment_put") as write, \
                mock.patch.object(m42, "_gui_journal_entry", return_value="journal"), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            m42.cmd_update_ticket(update_args(
                state="in_progress", subject="Changed", urgency="medium", priority=0,
                recipient="selected", resume_at="clear",
            ))
        writes = [call.args[2] for call in write.call_args_list if call.args[1] == m42.DD_ACTIVITY]
        self.assertEqual(writes, [{
            "ID": "act", "TimeStamp": "t", "Subject": "Changed", "Urgency": 2,
            "Priority": 0, "Recipient": "selected-user", "ReminderDate": None,
        }])
        identity.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["applied"]["Recipient"], "selected")

    def test_failed_activity_update_does_not_claim_applied_fields(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": "act", "TimeStamp": "t"}), \
                mock.patch.object(m42, "_ticket_common_fragment", return_value={"CID": "common", "State": 200, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_resolve_user_arg", return_value="selected-user"), \
                mock.patch.object(m42, "_fragment_put", side_effect=m42.M42Error("rejected")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                m42.cmd_update_ticket(update_args(recipient="selected", resume_at="clear"))
        self.assertEqual(json.loads(stdout.getvalue())["applied"], [])

    def test_close_validates_reason_before_recording_work(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": "act"}), \
                mock.patch.object(m42, "_ticket_common_fragment", return_value={"CID": "common", "State": 200, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_record_close_work_time") as work:
            with self.assertRaises(m42.M42Error):
                m42.cmd_close_ticket(close_args(reason="unknown"))
        work.assert_not_called()

    def test_fallback_close_requires_state_readback_before_close_journal(self):
        client = m42.Client("https://example.com", "token", configured_profile())
        with mock.patch.object(m42, "load_client", return_value=client), \
                mock.patch.object(client, "fragments", return_value=STATE_ROWS), \
                mock.patch.object(client, "single", return_value={"ID": "act"}), \
                mock.patch.object(client, "request", side_effect=m42.M42Error("endpoint rejected")), \
                mock.patch.object(m42, "_ticket_common_fragment", return_value={"CID": "common", "State": 202, "TimeStamp": "t"}), \
                mock.patch.object(m42, "_record_close_work_time", return_value=None), \
                mock.patch.object(m42, "_fragment_put", return_value=None), \
                mock.patch.object(m42, "_gui_journal_entry", return_value="processed"), \
                mock.patch.object(m42, "_close_journal_entry") as journal, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                m42.cmd_close_ticket(close_args(work_minutes=0))
        journal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
