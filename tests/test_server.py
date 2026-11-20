"""HTTP surface tests: freeze-before-analyze, revision impact, append validation."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from service_eval.server import make_server
from tests.test_analysis import _protocol, _tables


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sources = root / "sources"
        self.sources.mkdir()
        tables = _tables()
        for name, rows in tables.items():
            (self.sources / f"{name}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        self.tables = tables
        self.state = root / "state.json"
        self.server = make_server("127.0.0.1", 0, self.sources, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_analyze_before_freeze_is_rejected(self):
        status, body = self.call("/api/analyze", {"save": True})
        self.assertEqual(status, 409)
        self.assertIn("冻结", body["error"])

    def test_freeze_then_analyze_and_revise(self):
        spec = _protocol(self.tables)
        status, body = self.call("/api/protocol/freeze", {"protocol": spec, "reason": "初次冻结"})
        self.assertEqual(status, 201)
        status, body = self.call("/api/analyze", {"save": True})
        self.assertEqual(status, 200)
        first = body["report"]
        self.assertEqual(first["protocol_version"], 1)
        self.assertEqual(first["estimate"]["status"], "supported")

        spec2 = dict(spec, weighting="equal")
        status, body = self.call("/api/protocol/freeze", {"protocol": spec2, "reason": "改用等权"})
        self.assertEqual(status, 201)
        self.assertEqual(
            [c["field"] for c in body["protocol"]["changes_from_previous"]], ["weighting"]
        )
        status, body = self.call("/api/analyze", {"save": True})
        second = body["report"]
        self.assertEqual(second["protocol_version"], 2)
        self.assertTrue(second["previous"]["affected_parts"])

        status, impact = self.call("/api/report-impact?report=" + first["id"])
        self.assertEqual(status, 200)
        self.assertIn("impact", impact)

    def test_protocol_with_extra_switch_is_rejected(self):
        spec = dict(_protocol(self.tables), drop_grids=["T1"])
        status, body = self.call("/api/protocol/freeze", {"protocol": spec, "reason": "试图挑网格"})
        self.assertEqual(status, 400)
        self.assertIn("未允许字段", body["error"])

    def test_append_outcome_updates_sources(self):
        status, body = self.call(
            "/api/outcomes",
            {"grid": "T1", "month": "2026-07", "received": 10, "matured": 8, "timely": 6},
        )
        self.assertEqual(status, 201, body)
        rows = json.loads((self.sources / "outcomes.json").read_text(encoding="utf-8"))
        self.assertTrue(any(r["month"] == "2026-07" and r["grid"] == "T1" for r in rows))

    def test_duplicate_append_rejected(self):
        self.call(
            "/api/outcomes",
            {"grid": "T1", "month": "2026-07", "received": 10, "matured": 8, "timely": 6},
        )
        status, body = self.call(
            "/api/outcomes",
            {"grid": "T1", "month": "2026-07", "received": 1, "matured": 1, "timely": 1},
        )
        self.assertEqual(status, 400)
        self.assertIn("已有结果记录", body["error"])

    def test_append_negative_counts_rejected(self):
        status, body = self.call(
            "/api/outcomes",
            {"grid": "T1", "month": "2026-07", "received": -1, "matured": 1, "timely": 1},
        )
        self.assertEqual(status, 400)

    def test_shock_append_requires_known_intensity(self):
        status, _ = self.call(
            "/api/shocks",
            {"grid": "T1", "month": "2026-03", "type": "weather", "intensity": "extreme"},
        )
        self.assertEqual(status, 400)

    def test_static_page_and_assets(self):
        with urllib.request.urlopen(self.base + "/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("分阶段服务措施效果评估", html)
        with urllib.request.urlopen(self.base + "/app.js") as response:
            self.assertEqual(response.status, 200)

    def test_unknown_route(self):
        status, _ = self.call("/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
