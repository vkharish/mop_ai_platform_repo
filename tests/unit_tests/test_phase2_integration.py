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
