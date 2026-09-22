"""Opt-in live tests; ordinary discovery never contacts Matrix42.

Set M42_LIVE_TICKET to an operator-authorized test ticket for read checks.
Also set M42_LIVE_WRITE=internal-comment to add exactly one internal test note.
The write test leaves that note as evidence and never retries or deletes it.
"""
import contextlib
import hashlib
import html
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from test_m42 import SCRIPT, m42


@unittest.skipUnless(os.environ.get("M42_LIVE_TICKET"), "live ticket not selected")
class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m42.load_client()
        cls.ticket_number, _ = m42.parse_ticket_number(os.environ["M42_LIVE_TICKET"])

    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload.get("ok"), payload.get("error"))
        return payload

    def test_ticket_and_owned_journal_read(self):
        payload = self.cli("get-ticket", "--ticket-number", self.ticket_number)
        self.assertEqual(payload["ticket"]["TicketNumber"], self.ticket_number)
        self.assertIsInstance(payload["journal"], list)
        owner_ci, owner_id = m42._activity_owner(self.client, payload["ticket"]["ID"])
        self.assertTrue(m42.is_guid(owner_id))
        type_id, journal_owner = m42._journal_type_pair(self.client, self.ticket_number)
        self.assertTrue(m42.is_guid(type_id))
        self.assertTrue(m42.is_guid(journal_owner))
        for row in payload["journal"][:1]:
            self.assertTrue(m42._journal_entry_belongs_to_ticket(
                self.client, row["id"], self.ticket_number,
            ))
        print(json.dumps({
            "live_read": self.ticket_number,
            "state": payload["ticket"].get("Status"),
            "journal_count": len(payload["journal"]),
            "owner_ci": owner_ci,
        }), flush=True)

    def test_setup_discovery_preserves_config(self):
        path = Path(m42.CONFIG_PATH)
        before = hashlib.sha256(path.read_bytes()).digest() if path.exists() else None
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            m42.cmd_setup(SimpleNamespace(
                base_url=self.client.base_url, token=self.client.api_token,
                profile_file=None, verify=False,
            ))
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["configured"])
        self.assertNotIn(self.client.api_token, output.getvalue())
        after = hashlib.sha256(path.read_bytes()).digest() if path.exists() else None
        self.assertEqual(before, after, "discovery changed credentials/config")
        inventory = payload["discovery"]
        self.assertTrue(inventory["states"]["available"], inventory["states"].get("error"))
        self.assertTrue(inventory["states"]["rows"])
        self.assertTrue(payload["questions"])
        self.assertTrue(all(v is None for v in payload["profile_template"]["states"].values()))
        print(json.dumps({
            "live_discovery": {
                name: {
                    "available": section["available"],
                    "count": len(section.get("rows", section.get("prefix_counts", {}))),
                }
                for name, section in inventory.items()
            },
            "config_unchanged": True,
        }), flush=True)

    @unittest.skipUnless(
        os.environ.get("M42_LIVE_WRITE") == "internal-comment",
        "internal comment write not enabled",
    )
    def test_internal_comment_write_and_readback(self):
        before = self.cli("get-ticket", "--ticket-number", self.ticket_number)
        marker = f"m42sd-test-{uuid.uuid4()}"
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Explicit language and visibility belong to this test, not tenant defaults.
        note = (
            f"Integrationstest m42sd [{marker}] {stamp}\n"
            "Interner Testeintrag: Text, Sichtbarkeit und Ticketzuordnung.\n"
            "Literalzeichen: <test> & Vergleich."
        )
        result = self.cli(
            "add-comment", "--ticket-number", self.ticket_number,
            "--internal", "--text", note,
        )
        journal_id = result.get("journal_id")
        # Report the created artifact even when a later assertion fails.
        print(json.dumps({"live_comment": marker, "journal_id": journal_id}), flush=True)
        self.assert_comment_readback(note, marker, journal_id, before)

    def assert_comment_readback(self, note, marker, journal_id, before):
        """Read-only verification, reusable after a failed assertion without another write."""
        after = self.cli("get-ticket", "--ticket-number", self.ticket_number)
        matches = [row for row in after["journal"] if marker in (row.get("text") or "")]
        self.assertEqual(len(matches), 1, "test note missing or duplicated; inspect ticket")
        entry = matches[0]
        if journal_id:
            self.assertEqual(entry["id"], journal_id)
        self.assertEqual(entry["activity_action"], m42.JOURNAL_COMMENT_ACTION)
        self.assertEqual(entry["visible_in_portal"], 0)
        self.assertEqual(entry["text"].replace("\r\n", "\n"), note)
        raw = self.client.request(
            "GET", f"/api/data/fragments/{m42.DD_JOURNAL}/{entry['id']}",
        )
        self.assertEqual(raw["OriginalSolutionHtml"].replace("\r\n", "\n"), html.escape(note, quote=False))
        self.assertTrue(m42._journal_entry_belongs_to_ticket(
            self.client, entry["id"], self.ticket_number,
        ))
        for field in ("Status", "Subject", "RecipientId", "Urgency", "ReminderDate", "WorkingTimeDisplayString"):
            self.assertEqual(before["ticket"].get(field), after["ticket"].get(field), field)
        print(json.dumps({
            "live_write_verified": self.ticket_number,
            "journal_id": entry["id"], "internal": True,
            "ticket_fields_unchanged": True,
        }), flush=True)


if __name__ == "__main__":
    unittest.main()
