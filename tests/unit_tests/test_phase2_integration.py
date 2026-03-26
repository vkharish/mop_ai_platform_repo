"""
Phase 2 integration tests — notifications, ITSM, reporting, API v2, concurrency.
All tests use dry-run / mock mode (no real network devices or external services).
"""

from __future__ import annotations

import pytest
from models.canonical import (
    ActionType, CanonicalTestModel, CLICommand, ExecutionStatus,
    FailureStrategy, StepType, TestStep, DeviceRef, BlastRadius,
    ExecutionPolicy
)
from device_layer.device_driver import MockDriver
from execution_engine.state_manager import state_manager


def _step(sid, seq, cmd="show version", vendor="cisco", mode="exec",
          action=ActionType.EXECUTE, devices=None):
    c = CLICommand(raw=cmd, normalized=cmd.lower(), vendor=vendor, mode=mode)
    return TestStep(
        step_id=sid, sequence=seq, step_type=StepType.ACTION,
        action_type=action, description=f"Step {sid}", raw_text=cmd,
        commands=[c],
        devices=[DeviceRef(hostname=h) for h in (devices or [])],
    )


def _model(steps, strategy=FailureStrategy.ABORT):
    return CanonicalTestModel(
        document_title="Integration Test MOP", source_file="test.txt",
        source_format="txt", mop_structure="flat",
        steps=steps, failure_strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Concurrency Controller
# ---------------------------------------------------------------------------

class TestConcurrencyController:

    def test_acquire_and_release(self):
        from execution_engine.concurrency_controller import ConcurrencyController
        ctrl = ConcurrencyController(max_concurrent_devices=2)
        with ctrl.acquire_device("PE1"):
            assert ctrl.active_device_count() >= 1
        # After release, slot should be free
        with ctrl.acquire_device("PE1"):
            pass  # should not timeout

    def test_timeout_raises(self):
        from execution_engine.concurrency_controller import ConcurrencyController
        ctrl = ConcurrencyController(max_concurrent_devices=1, queue_timeout_s=0.1)
        import threading
        held = threading.Event()
        released = threading.Event()

        def _hold():
            with ctrl.acquire_device("PE1"):
                held.set()
                released.wait(timeout=2)

        t = threading.Thread(target=_hold, daemon=True)
        t.start()
        held.wait(timeout=1)

        with pytest.raises(RuntimeError, match="Concurrency timeout"):
            with ctrl.acquire_device("PE1", timeout_s=0.05):
                pass

        released.set()
        t.join(timeout=1)

    def test_different_devices_not_blocked(self):
        from execution_engine.concurrency_controller import ConcurrencyController
        ctrl = ConcurrencyController(max_concurrent_devices=2)
        with ctrl.acquire_device("PE1"):
            with ctrl.acquire_device("PE2"):
                pass  # should not block


# ---------------------------------------------------------------------------
# Notifications — dry-run mode (no env vars)
# ---------------------------------------------------------------------------

class TestNotificationsDryRun:

    def test_slack_dry_run(self):
        from notifications.slack_notifier import SlackNotifier
        notifier = SlackNotifier()
        # No SLACK_WEBHOOK_URL set → dry run returns False
        result = notifier.send("execution_started", execution_id="test123", title="Test MOP", steps=5)
        assert result is False  # dry run

    def test_pagerduty_dry_run(self):
        from notifications.pagerduty_notifier import PagerDutyNotifier
        notifier = PagerDutyNotifier()
        result = notifier.send("execution_failed", execution_id="test123")
        assert result is False

    def test_email_dry_run(self):
        from notifications.email_notifier import EmailNotifier
        notifier = EmailNotifier()
        result = notifier.send("execution_passed", execution_id="test123", title="Test")
        assert result is False

    def test_router_sends_to_all(self):
        from notifications.notification_router import NotificationRouter
        router = NotificationRouter()
        results = router.send("execution_started", execution_id="test123", title="Test", steps=3)
        assert "SlackNotifier" in results
        assert "EmailNotifier" in results
        assert "PagerDutyNotifier" in results

    def test_router_non_critical_event(self):
        from notifications.notification_router import NotificationRouter
        router = NotificationRouter()
        # rollback_started should not raise
        results = router.send("rollback_started", execution_id="abc", scope="group_1")
        assert isinstance(results, dict)

    def test_router_does_not_propagate_notifier_error(self):
        """NotificationRouter must catch notifier exceptions."""
        from notifications.notification_router import NotificationRouter
        import unittest.mock as mock

        router = NotificationRouter()
        with mock.patch.object(router._notifiers[0], "send", side_effect=RuntimeError("oops")):
            results = router.send("execution_started", execution_id="x")
        assert results.get(router._notifiers[0].__class__.__name__) is False


# ---------------------------------------------------------------------------
# ITSM — dry-run mode
# ---------------------------------------------------------------------------

class TestITSMDryRun:

    def _make_ticket(self):
        from models.canonical import ITSMRef
        return ITSMRef(system="servicenow", ticket_id="CHG0012345", webhook_url="")

    def test_servicenow_dry_run(self):
        from itsm.servicenow_adapter import ServiceNowAdapter
        adapter = ServiceNowAdapter()
        result = adapter.add_comment("CHG001", "", "test comment")
        assert result is False  # dry run

    def test_jira_dry_run(self):
        from itsm.jira_adapter import JiraAdapter
        adapter = JiraAdapter()
        result = adapter.add_comment("PROJ-123", "", "test comment")
        assert result is False

    def test_itsm_client_unknown_system(self):
        from itsm.itsm_client import ITSMClient
        from models.canonical import ITSMRef
        client = ITSMClient()
        ticket = ITSMRef(system="unknown_system", ticket_id="T1", webhook_url="")
        result = client.comment(ticket, "test")
        assert result is False  # error caught

    def test_itsm_client_notify_methods(self):
        from itsm.itsm_client import ITSMClient
        client = ITSMClient()
        ticket = self._make_ticket()
        # All should return False in dry-run (no credentials)
        assert client.notify_execution_started(ticket, "exec-1") is False
        assert client.notify_step_failed(ticket, "s1", "PE1", "error") is False
        assert client.notify_execution_passed(ticket, "exec-1", 42.0) is False
        assert client.notify_execution_failed(ticket, "exec-1", ["s1", "s2"]) is False


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestExecutionReport:

    def _create_completed_execution(self):
        steps = [
            _step("s1", 1, devices=["PE1"]),
            _step("s2", 2, devices=["PE1"]),
        ]
        model = _model(steps)
        eid = state_manager.create(model, dry_run=True)
        state_manager.transition_execution(eid, ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s1", ExecutionStatus.RUNNING, actual_output="OK")
        state_manager.transition_step(eid, "s1", ExecutionStatus.PASSED)
        state_manager.transition_step(eid, "s2", ExecutionStatus.RUNNING, actual_output="OK")
        state_manager.transition_step(eid, "s2", ExecutionStatus.PASSED)
        state_manager.transition_execution(eid, ExecutionStatus.PASSED)
        return eid

    def test_build_report_structure(self):
        from reporting.execution_report import ExecutionReportBuilder
        eid = self._create_completed_execution()
        report = ExecutionReportBuilder.build(eid)
        assert report["execution_id"] == eid
        assert report["overall_status"] == "passed"
        assert report["steps_total"] == 2
        assert report["steps_passed"] == 2
        assert report["steps_failed"] == 0
        assert "per_step" in report
        assert "per_device_summary" in report
        assert "timeline" in report

    def test_render_html_inline(self):
        from reporting.execution_report import ExecutionReportBuilder
        eid = self._create_completed_execution()
        report = ExecutionReportBuilder.build(eid)
        html = ExecutionReportBuilder.render_html(report)
        assert "<html" in html
        assert eid in html
        assert "passed" in html.lower()

    def test_save_generates_files(self, tmp_path):
        from reporting.execution_report import ExecutionReportBuilder
        eid = self._create_completed_execution()
        paths = ExecutionReportBuilder.save(eid, output_dir=str(tmp_path))
        assert "json" in paths and "html" in paths
        import json, pathlib
        assert pathlib.Path(paths["json"]).exists()
        data = json.loads(pathlib.Path(paths["json"]).read_text())
        assert data["execution_id"] == eid

    def test_per_device_summary(self):
        from reporting.execution_report import ExecutionReportBuilder
        eid = self._create_completed_execution()
        report = ExecutionReportBuilder.build(eid)
        device_summary = report["per_device_summary"]
        assert "PE1" in device_summary
        assert device_summary["PE1"]["steps_passed"] == 2

    def test_failed_execution_report(self):
        from reporting.execution_report import ExecutionReportBuilder
        steps = [_step("s1", 1, devices=["PE1"])]
        model = _model(steps)
        eid = state_manager.create(model, dry_run=True)
        state_manager.transition_execution(eid, ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s1", ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s1", ExecutionStatus.FAILED, error_message="Timeout")
        state_manager.transition_execution(eid, ExecutionStatus.FAILED)
        report = ExecutionReportBuilder.build(eid)
        assert report["overall_status"] == "failed"
        assert report["steps_failed"] == 1


# ---------------------------------------------------------------------------
# Blocked Commands
# ---------------------------------------------------------------------------

class TestBlockedCommands:

    def test_blocked_command_raises(self):
        """Planner should reject executions with blocked commands."""
        from execution_engine.planner_agent import PlannerAgent
        import unittest.mock as mock

        # Mock the config to return a blocked pattern
        blocked_config = {"blocked_commands": [r"^reload\b"]}

        steps = [_step("s1", 1, cmd="reload", vendor="cisco", mode="exec")]
        model = _model(steps)
        eid = state_manager.create(model)

        with mock.patch("builtins.open", mock.mock_open(read_data="")):
            with mock.patch("yaml.safe_load", return_value=blocked_config):
                with pytest.raises(ValueError, match="blocked"):
                    PlannerAgent().plan(model, eid)

    def test_non_blocked_command_passes(self):
        """Non-blocked commands should pass normally."""
        from execution_engine.planner_agent import PlannerAgent
        steps = [_step("s1", 1, cmd="show version", vendor="cisco", mode="exec")]
        model = _model(steps)
        eid = state_manager.create(model)
        # Should not raise
        plan = PlannerAgent().plan(model, eid)
        assert len(plan.waves) == 1


# ---------------------------------------------------------------------------
# Decision Log
# ---------------------------------------------------------------------------

class TestDecisionLog:

    def test_write_decision_record(self, tmp_path):
        from execution_engine.recovery_agent import RecoveryAgent
        import unittest.mock as mock, pathlib

        agent = RecoveryAgent()
        step = _step("s1", 1)
        step.execution_policy = ExecutionPolicy(retry_count=3)

        log_path = tmp_path / "decision.log"
        with mock.patch("pathlib.Path", side_effect=lambda *args: tmp_path / args[-1] if args else tmp_path):
            pass  # just verify method exists and doesn't crash
        # Direct call
        with mock.patch("builtins.open", mock.mock_open()) as mock_file:
            with mock.patch("pathlib.Path.mkdir"):
                agent._write_decision_record(step, "RETRY", "within retry limit", "", 1.0, "exec-1")
        mock_file.assert_called()


# ---------------------------------------------------------------------------
# Quality Scorer
# ---------------------------------------------------------------------------

class TestQualityScorer:

    def _full_model(self):
        """Model with pre-checks, implementation, verification, rollback."""
        from models.canonical import FailureStrategy
        steps = [
            TestStep(step_id="p1", sequence=1, step_type=StepType.VERIFICATION,
                     action_type=ActionType.VERIFY, description="Pre-check BGP",
                     raw_text="show bgp sum", section="Pre-checks",
                     commands=[CLICommand(raw="show bgp summary", confidence=0.95)],
                     expected_output="BGP neighbors established"),
            TestStep(step_id="i1", sequence=2, step_type=StepType.ACTION,
                     action_type=ActionType.CONFIGURE, description="Apply config",
                     raw_text="router bgp 65000", section="Implementation",
                     commands=[CLICommand(raw="router bgp 65000", confidence=0.9)]),
            TestStep(step_id="i2", sequence=3, step_type=StepType.ACTION,
                     action_type=ActionType.CONFIGURE, description="Apply neighbor",
                     raw_text="neighbor 10.0.0.1", section="Implementation",
                     commands=[CLICommand(raw="neighbor 10.0.0.1 remote-as 65001", confidence=0.9)]),
            TestStep(step_id="v1", sequence=4, step_type=StepType.VERIFICATION,
                     action_type=ActionType.VERIFY, description="Verify BGP",
                     raw_text="show bgp sum", section="Verification",
                     commands=[CLICommand(raw="show bgp summary", confidence=0.95)],
                     expected_output="BGP neighbors established"),
            TestStep(step_id="r1", sequence=5, step_type=StepType.ROLLBACK,
                     action_type=ActionType.ROLLBACK, description="Rollback",
                     raw_text="no router bgp", section="Rollback",
                     is_rollback=True,
                     commands=[CLICommand(raw="no router bgp 65000", confidence=0.9)]),
        ]
        return CanonicalTestModel(
            document_title="Test MOP", source_file="test.pdf",
            source_format="pdf", mop_structure="prose",
            steps=steps, failure_strategy=FailureStrategy.ROLLBACK_ALL,
        )

    def test_high_score_full_model(self):
        from quality.quality_scorer import QualityScorer
        qs = QualityScorer.score(self._full_model())
        assert qs.band == "HIGH"
        assert qs.score >= 8

    def test_low_score_empty_model(self):
        from quality.quality_scorer import QualityScorer
        model = CanonicalTestModel(
            document_title="Empty MOP", source_file="e.pdf",
            source_format="pdf", mop_structure="unknown",
            steps=[TestStep(step_id="x1", sequence=1, step_type=StepType.INFO,
                            action_type=ActionType.OBSERVE, description="No ops",
                            raw_text="")],
        )
        qs = QualityScorer.score(model)
        assert qs.band == "LOW"
        assert qs.score < 4

    def test_medium_score_no_rollback(self):
        from quality.quality_scorer import QualityScorer
        model = self._full_model()
        model.steps = [s for s in model.steps if not s.is_rollback]
        qs = QualityScorer.score(model)
        # Loses 2 rollback points and 1 failure strategy point
        assert qs.band in ("MEDIUM", "HIGH")
        assert "rollback" in " ".join(qs.warnings + qs.recommendations).lower()

    def test_breakdown_keys_present(self):
        from quality.quality_scorer import QualityScorer
        qs = QualityScorer.score(self._full_model())
        for key in ("commands_detected", "rollback_steps", "pre_checks",
                    "verification_section", "expected_output_coverage",
                    "command_confidence", "section_diversity", "failure_strategy"):
            assert key in qs.breakdown

    def test_summary_line_format(self):
        from quality.quality_scorer import QualityScorer
        qs = QualityScorer.score(self._full_model())
        line = qs.summary_line()
        assert "Quality:" in line
        assert qs.band in line

    def test_percentage_calculation(self):
        from quality.quality_scorer import QualityScorer
        qs = QualityScorer.score(self._full_model())
        assert 0 <= qs.percentage <= 100
        assert qs.percentage == round(qs.score / qs.max_score * 100)


# ---------------------------------------------------------------------------
# Diff Engine
# ---------------------------------------------------------------------------

class TestDiffEngine:

    def test_identical_outputs_no_change(self):
        from reporting.diff_engine import DiffEngine
        result = DiffEngine.diff_text("show bgp\nEstablished", "show bgp\nEstablished", label="bgp")
        assert result.is_identical
        assert not result.changed
        assert result.added_lines == []
        assert result.removed_lines == []

    def test_added_line_detected(self):
        from reporting.diff_engine import DiffEngine
        result = DiffEngine.diff_text(
            "Neighbor  State\n10.0.0.1  Established",
            "Neighbor  State\n10.0.0.1  Established\n10.0.0.2  Established",
            label="bgp neighbors",
        )
        assert result.changed
        assert any("10.0.0.2" in l for l in result.added_lines)

    def test_removed_line_detected(self):
        from reporting.diff_engine import DiffEngine
        result = DiffEngine.diff_text(
            "Neighbor  State\n10.0.0.1  Established\n10.0.0.2  Idle",
            "Neighbor  State\n10.0.0.1  Established",
            label="bgp neighbors",
        )
        assert result.changed
        assert any("10.0.0.2" in l for l in result.removed_lines)

    def test_timestamp_stripping(self):
        from reporting.diff_engine import DiffEngine
        baseline = "BGP uptime: 01:23:45\nEstablished"
        current  = "BGP uptime: 02:00:00\nEstablished"
        result = DiffEngine.diff_text(baseline, current, ignore_timestamps=True)
        assert result.is_identical  # timestamps stripped, rest identical

    def test_summary_no_change(self):
        from reporting.diff_engine import DiffEngine
        result = DiffEngine.diff_text("same", "same", label="cmd")
        assert "NO CHANGE" in result.summary()

    def test_summary_changed(self):
        from reporting.diff_engine import DiffEngine
        result = DiffEngine.diff_text("old line", "new line", label="cmd")
        assert "CHANGED" in result.summary()

    def test_step_diff_added(self):
        from reporting.diff_engine import DiffEngine
        m1 = CanonicalTestModel(
            document_title="T", source_file="f", source_format="pdf",
            mop_structure="prose",
            steps=[TestStep(step_id="s1", sequence=1, step_type=StepType.ACTION,
                            action_type=ActionType.EXECUTE, description="old",
                            raw_text="cmd", commands=[CLICommand(raw="show version")])],
        )
        m2 = CanonicalTestModel(
            document_title="T", source_file="f", source_format="pdf",
            mop_structure="prose",
            steps=[
                TestStep(step_id="s1", sequence=1, step_type=StepType.ACTION,
                         action_type=ActionType.EXECUTE, description="old",
                         raw_text="cmd", commands=[CLICommand(raw="show version")]),
                TestStep(step_id="s2", sequence=2, step_type=StepType.ACTION,
                         action_type=ActionType.EXECUTE, description="new step",
                         raw_text="cmd2", commands=[CLICommand(raw="show bgp")]),
            ],
        )
        diff = DiffEngine.diff_steps(m1, m2)
        assert diff.has_changes
        assert len(diff.added_steps) == 1

    def test_comparison_report_format(self):
        from reporting.diff_engine import DiffEngine
        results = [
            ("show bgp", DiffEngine.diff_text("Established", "Established")),
            ("show ospf", DiffEngine.diff_text("FULL", "DOWN")),
        ]
        report = DiffEngine.build_comparison_report(results)
        assert "PRE / POST" in report
        assert "unchanged" in report.lower()
        assert "CHANGED" in report


# ---------------------------------------------------------------------------
# Dry Run Plan (pipeline integration)
# ---------------------------------------------------------------------------

class TestDryRunPlan:

    def test_dry_run_creates_file(self, tmp_path):
        from quality.quality_scorer import QualityScorer
        import pipeline as pl
        model = CanonicalTestModel(
            document_title="Test MOP", source_file="test.pdf",
            source_format="pdf", mop_structure="prose",
            steps=[TestStep(step_id="s1", sequence=1, step_type=StepType.ACTION,
                            action_type=ActionType.EXECUTE, description="Run BGP check",
                            raw_text="show bgp", section="Implementation",
                            commands=[CLICommand(raw="show bgp summary")])],
        )
        qs = QualityScorer.score(model)
        out_path = pl._print_dry_run_plan(model, str(tmp_path), qs)
        assert out_path.endswith("_dryrun.txt")
        content = open(out_path).read()
        assert "DRY RUN" in content
        assert "show bgp summary" in content
        assert "Test MOP" in content

    def test_dry_run_includes_rollback(self, tmp_path):
        from quality.quality_scorer import QualityScorer
        import pipeline as pl
        model = CanonicalTestModel(
            document_title="Rollback MOP", source_file="r.pdf",
            source_format="pdf", mop_structure="prose",
            steps=[
                TestStep(step_id="s1", sequence=1, step_type=StepType.ACTION,
                         action_type=ActionType.EXECUTE, description="Apply",
                         raw_text="cmd", section="Implementation",
                         commands=[CLICommand(raw="router bgp 100")]),
                TestStep(step_id="r1", sequence=2, step_type=StepType.ROLLBACK,
                         action_type=ActionType.ROLLBACK, description="Undo",
                         raw_text="no cmd", section="Rollback", is_rollback=True,
                         commands=[CLICommand(raw="no router bgp 100")]),
            ],
        )
        qs = QualityScorer.score(model)
        out_path = pl._print_dry_run_plan(model, str(tmp_path), qs)
        content = open(out_path).read()
        assert "Rollback Procedure" in content
        assert "no router bgp 100" in content
