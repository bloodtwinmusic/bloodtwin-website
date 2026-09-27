import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_diagnostics


class PipelineDiagnosticsTests(unittest.TestCase):
    def write_board(self, directory, observed, window_end, event_count=10):
        path = Path(directory) / "latest-analysis-board.json"
        path.write_text(
            json.dumps(
                {
                    "observed_at_utc": observed,
                    "collection_window_end_utc": window_end,
                    "event_count": event_count,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_morning_board_is_fresh_when_it_covers_next_ten_am_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self.write_board(
                tmpdir,
                "2026-09-28T08:42:00Z",  # 09:42 BST
                "2026-09-29T09:00:00Z",  # 10:00 BST next day
            )
            result = pipeline_diagnostics.board_freshness(
                board, "morning", datetime(2026, 9, 28, 8, 57, tzinfo=timezone.utc)
            )
        self.assertTrue(result["fresh"])
        self.assertEqual(result["reason"], "fresh_board_for_cycle")

    def test_previous_evening_board_does_not_suppress_morning_cycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self.write_board(
                tmpdir,
                "2026-09-27T19:04:23Z",
                "2026-09-28T09:00:00Z",
            )
            result = pipeline_diagnostics.board_freshness(
                board, "morning", datetime(2026, 9, 28, 8, 57, tzinfo=timezone.utc)
            )
        self.assertFalse(result["fresh"])
        self.assertEqual(result["reason"], "board_precedes_cycle")

    def test_evening_board_is_fresh_for_delayed_primary_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self.write_board(
                tmpdir,
                "2026-09-28T15:12:30Z",  # 16:12 BST backup
                "2026-09-29T09:00:00Z",
            )
            result = pipeline_diagnostics.board_freshness(
                board, "evening", datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
            )
        self.assertTrue(result["fresh"])

    def test_gate_suppresses_duplicate_without_provider_contact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self.write_board(
                tmpdir, "2026-09-28T08:42:00Z", "2026-09-29T09:00:00Z"
            )
            status = Path(tmpdir) / "status.json"
            output = Path(tmpdir) / "output.txt"
            args = SimpleNamespace(
                cycle="morning",
                board=str(board),
                status=str(status),
                github_output=str(output),
                now="2026-09-28T08:57:00Z",
                run_id="123",
                event="schedule",
                schedule="57 9 * * *",
                sha="abc",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline_diagnostics.gate(args), 0)
            diagnostic = json.loads(status.read_text(encoding="utf-8"))
            rendered_output = output.read_text(encoding="utf-8")
        self.assertFalse(diagnostic["collect_required"])
        self.assertFalse(diagnostic["provider_contacted"])
        self.assertFalse(diagnostic["paid_resources_consumed"])
        self.assertEqual(diagnostic["paid_resource_consumption_status"], "not_attempted")
        self.assertIn("collect_required=false", rendered_output)

    def test_failure_stages_map_to_required_forensic_categories(self):
        self.assertEqual(
            pipeline_diagnostics.failure_classification("provider_api_started"),
            "collector_started_provider_api_failed",
        )
        self.assertEqual(
            pipeline_diagnostics.failure_classification("normalization_started"),
            "collection_succeeded_normalization_failed",
        )
        self.assertEqual(
            pipeline_diagnostics.failure_classification("board_manifest_generation_started"),
            "normalization_succeeded_board_manifest_failed",
        )

    def test_finalize_distinguishes_commit_publish_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status = Path(tmpdir) / "status.json"
            pipeline_diagnostics.update_status(status, "board_manifest_generation_succeeded")
            args = SimpleNamespace(
                status=str(status),
                gate="success",
                collect_required="true",
                collector="success",
                archive="success",
                commit="failure",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline_diagnostics.finalize(args), 0)
            diagnostic = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(
            diagnostic["classification"],
            "collection_and_board_succeeded_commit_publish_failed",
        )


if __name__ == "__main__":
    unittest.main()
