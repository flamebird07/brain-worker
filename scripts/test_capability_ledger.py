"""Offline checks for pending model identity and exactly-once scoring."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import capability_ledger as ledger


class PendingScoreTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.summaries = []

        def records(table, _bearer):
            return list(self.events if table == ledger.EVENTS else self.summaries)

        def request(method, path, _bearer=None, body=None, params=None):
            if method != "POST" or ledger.EVENTS not in path:
                self.fail(f"Unexpected API call: {method} {path}")
            self.events.append({"fields": body["fields"].copy()})
            return {}

        self.patches = [
            patch.object(ledger, "local_lock", lambda: contextlib.nullcontext()),
            patch.object(ledger, "token", lambda: "fake"),
            patch.object(ledger, "records", records),
            patch.object(ledger, "request", request),
            patch.object(ledger, "summary_upsert", lambda *_args: None),
            patch.object(ledger, "check_legacy_drift", lambda _events: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *args):
        with io.StringIO() as output, contextlib.redirect_stdout(output):
            ledger.main(list(args))
            return json.loads(output.getvalue())

    def pending(self):
        return self.run_cli(
            "record", "--event-id", "phase-11", "--agent", "Xiaomi MiMo",
            "--model", "未知", "--task", "广告", "--stage", "11",
            "--evaluated-at", "2026-09-28T20:00:00+08:00", "--outcome", "轻微问题",
            "--evidence", "report.md", "--issue", "result mismatch",
        )

    def resolve(self, model="MiMo V2.6 Pro"):
        return self.run_cli(
            "resolve", "--pending-event-id", "phase-11", "--agent", "Xiaomi MiMo",
            "--model", model, "--identity-evidence", "user confirmed",
            "--identity-confirmed",
        )

    def test_pending_then_resolve_once(self):
        pending = self.pending()
        self.assertTrue(pending["pendingIdentity"])
        self.assertEqual(pending["拟评分变化"], -5)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["fields"]["事件类型"], "identity_pending")
        self.assertEqual(self.events[0]["fields"]["主脑结论"], "轻微问题")
        self.assertEqual(ledger.state(self.events, "Xiaomi MiMo / MiMo V2.6 Pro")["累计样本数"], 0)

        resolved = self.resolve()
        self.assertEqual((resolved["共享评分"], resolved["累计样本数"]), (45, 1))
        self.assertEqual(self.events[-1]["fields"]["事件ID"], "resolve:phase-11")
        self.assertIn("user confirmed", self.events[-1]["fields"]["证据"])
        length = len(self.events)
        self.assertTrue(self.resolve()["idempotent"])
        self.assertEqual(len(self.events), length)

    def test_changed_model_cannot_score_twice(self):
        self.pending()
        self.resolve()
        with self.assertRaisesRegex(RuntimeError, "已归属其他模型"):
            self.resolve("MiMo V2.7 Pro")

    def test_old_neutral_assessment_is_not_resolvable(self):
        self.events.append({"fields": {"事件ID": "old", "事件类型": "assessment", "主脑结论": "证据不足"}})
        with self.assertRaisesRegex(RuntimeError, "旧版证据不足"):
            self.run_cli("resolve", "--pending-event-id", "old", "--agent", "Xiaomi MiMo",
                         "--model", "MiMo V2.6 Pro", "--identity-evidence", "user confirmed",
                         "--identity-confirmed")

    def test_unknown_model_cannot_receive_direct_score(self):
        with self.assertRaisesRegex(RuntimeError, "身份不明确"):
            self.run_cli("record", "--event-id", "bad", "--agent", "Xiaomi MiMo",
                         "--model", "未知", "--task", "广告", "--stage", "12",
                         "--evaluated-at", "2026-09-28T20:00:00+08:00", "--outcome", "无问题",
                         "--evidence", "report.md", "--identity-confirmed")


if __name__ == "__main__":
    unittest.main()
