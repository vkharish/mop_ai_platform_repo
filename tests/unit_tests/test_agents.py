"""
Layer 6 agent tests — PlannerAgent, ValidationAgent, ExecutionAgent, RecoveryAgent.

All tests use MockDriver (no real SSH) and an in-memory state store.
"""

from __future__ import annotations

import pytest
from typing import List

from models.canonical import (
    ActionType,
    ApprovalStatus,
    BlastRadius,
    CanonicalTestModel,
    CLICommand,
    ExecutionPolicy,
    ExecutionStatus,
    FailureStrategy,
    IdempotencyRule,
    StepTiming,
    StepType,
    TestStep,
    ValidationRule,
    DeviceRef,
)
from device_layer.device_driver import MockDriver
from execution_engine.dag_engine import DAGValidationError
from execution_engine.planner_agent import PlannerAgent
from execution_engine.validation_agent import ValidationAgent, ValidationResult
from execution_engine.recovery_agent import RecoveryAgent
from execution_engine.state_manager import state_manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step(
    step_id: str,
    seq: int,
    raw_cmd: str = "show version",
    action_type: ActionType = ActionType.EXECUTE,
    step_type: StepType = StepType.ACTION,
    dependencies: List[str] = None,
    transaction_group: str = None,
    approval_required: bool = False,
    blast_radius: BlastRadius = BlastRadius.LOW,
    vendor: str = "cisco",
    mode: str = "exec",
    expected_output: str = None,
    devices: List[str] = None,
) -> TestStep:
    cmd = CLICommand(
        raw=raw_cmd, normalized=raw_cmd.lower(),
        vendor=vendor, mode=mode,
    )
    device_refs = [DeviceRef(hostname=h) for h in (devices or [])]
    return TestStep(
        step_id=step_id,
        sequence=seq,
        step_type=step_type,
        action_type=action_type,
        description=f"Step {step_id}",
        raw_text=raw_cmd,
        commands=[cmd],
        dependencies=dependencies or [],
        transaction_group=transaction_group,
        approval_required=approval_required,
        blast_radius=blast_radius,
        expected_output=expected_output,
        devices=device_refs,
    )


def _make_model(steps: List[TestStep], failure_strategy=FailureStrategy.ABORT) -> CanonicalTestModel:
    return CanonicalTestModel(
        document_title="Test MOP",
        source_file="test.txt",
        source_format="txt",
        mop_structure="flat",
        steps=steps,
        failure_strategy=failure_strategy,
    )


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------

class TestPlannerAgent:

    def test_simple_plan_no_deps(self):
        steps = [_make_step("s1", 1), _make_step("s2", 2), _make_step("s3", 3)]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        # Without dependencies, all steps can run in wave 0
        assert len(plan.waves) == 1
        assert set(plan.waves[0]) == {"s1", "s2", "s3"}

    def test_sequential_deps(self):
        steps = [
            _make_step("s1", 1),
            _make_step("s2", 2, dependencies=["s1"]),
            _make_step("s3", 3, dependencies=["s2"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert len(plan.waves) == 3
        assert plan.waves[0] == ["s1"]
        assert plan.waves[1] == ["s2"]
        assert plan.waves[2] == ["s3"]

    def test_diamond_deps(self):
        # s1 → s2, s1 → s3, s2+s3 → s4
        steps = [
            _make_step("s1", 1),
            _make_step("s2", 2, dependencies=["s1"]),
            _make_step("s3", 3, dependencies=["s1"]),
            _make_step("s4", 4, dependencies=["s2", "s3"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert len(plan.waves) == 3
        assert plan.waves[0] == ["s1"]
        assert set(plan.waves[1]) == {"s2", "s3"}
        assert plan.waves[2] == ["s4"]

    def test_cycle_raises(self):
        steps = [
            _make_step("s1", 1, dependencies=["s2"]),
            _make_step("s2", 2, dependencies=["s1"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        with pytest.raises(DAGValidationError):
            PlannerAgent().plan(model, eid)

    def test_approval_required_high_blast(self):
        steps = [
            _make_step("s1", 1, blast_radius=BlastRadius.HIGH),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert plan.requires_approval is True
        assert len(plan.approval_reasons) > 0

    def test_no_approval_low_blast(self):
        steps = [_make_step("s1", 1, blast_radius=BlastRadius.LOW)]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert plan.requires_approval is False

    def test_transaction_groups(self):
        steps = [
            _make_step("s1", 1, transaction_group="bgp_cutover"),
            _make_step("s2", 2, transaction_group="bgp_cutover"),
            _make_step("s3", 3),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert "bgp_cutover" in plan.transaction_groups
        assert set(plan.transaction_groups["bgp_cutover"]) == {"s1", "s2"}

    def test_device_list(self):
        steps = [
            _make_step("s1", 1, devices=["PE1", "PE2"]),
            _make_step("s2", 2, devices=["PE1"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert plan.device_list == ["PE1", "PE2"]

    def test_critical_path_linear(self):
        steps = [
            _make_step("s1", 1),
            _make_step("s2", 2, dependencies=["s1"]),
            _make_step("s3", 3, dependencies=["s2"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        plan = PlannerAgent().plan(model, eid)
        assert plan.critical_path == ["s1", "s2", "s3"]


# ---------------------------------------------------------------------------
# ValidationAgent
# ---------------------------------------------------------------------------

class TestValidationAgent:

    def _make_verify_step(self, expected: str = None, rules: List[ValidationRule] = None):
        cmd = CLICommand(raw="show bgp summary", normalized="show bgp summary", vendor="cisco", mode="exec")
        return TestStep(
            step_id="v1",
            sequence=1,
            step_type=StepType.VERIFICATION,
            action_type=ActionType.VERIFY,
            description="Verify BGP",
            raw_text="show bgp summary",
            commands=[cmd],
            expected_output=expected,
            validation_rules=rules or [],
        )

    def test_passes_when_expected_substring_found(self):
        step = self._make_verify_step(expected="Established")
        agent = ValidationAgent()
        result = agent.validate(step, "BGP neighbor 10.0.0.1 Established", execution_id="test")
        assert result.passed is True

    def test_fails_when_expected_not_found(self):
        step = self._make_verify_step(expected="Established")
        agent = ValidationAgent()
        result = agent.validate(step, "BGP neighbor 10.0.0.1 Active")
        assert result.passed is False
        assert any("Expected output not found" in e for e in result.errors)

    def test_fails_on_error_indicator(self):
        step = self._make_verify_step()
        agent = ValidationAgent()
        result = agent.validate(step, "% Invalid command")
        assert result.passed is False
        assert any("error" in e.lower() or "invalid" in e.lower() for e in result.errors)

    def test_passes_empty_expected_no_error(self):
        step = self._make_verify_step(expected=None)
        agent = ValidationAgent()
        result = agent.validate(step, "Some valid output")
        assert result.passed is True

    def test_regex_expected_output(self):
        step = self._make_verify_step(expected=r"Estab\w+")
        agent = ValidationAgent()
        result = agent.validate(step, "BGP peer Established")
        assert result.passed is True

    def test_active_validation_rule_passes(self):
        step = self._make_verify_step(rules=[
            ValidationRule(cmd="show bgp neighbor", expect_pattern="Established")
        ])
        driver = MockDriver(
            hostname="PE1",
            responses={"show bgp neighbor": "BGP neighbor 10.0.0.1 Established"}
        )
        driver.connect()
        agent = ValidationAgent()
        result = agent.validate(step, "ok", driver=driver)
        assert result.passed is True

    def test_active_validation_rule_fails(self):
        step = self._make_verify_step(rules=[
            ValidationRule(cmd="show bgp neighbor", expect_pattern="Established")
        ])
        driver = MockDriver(
            hostname="PE1",
            responses={"show bgp neighbor": "BGP neighbor 10.0.0.1 Active"}
        )
        driver.connect()
        agent = ValidationAgent()
        result = agent.validate(step, "ok", driver=driver)
        assert result.passed is False

    def test_action_step_no_expected_check(self):
        """Action steps only get error-pattern checks, not expected_output matching."""
        cmd = CLICommand(raw="router bgp 100", normalized="router bgp 100", vendor="cisco", mode="config")
        step = TestStep(
            step_id="a1", sequence=1,
            step_type=StepType.ACTION, action_type=ActionType.EXECUTE,
            description="Configure BGP", raw_text="router bgp 100",
            commands=[cmd], expected_output="Established",  # should be ignored
        )
        agent = ValidationAgent()
        result = agent.validate(step, "BGP not yet established")
        # No error pattern, expected_output not checked for ACTION steps
        assert result.passed is True


# ---------------------------------------------------------------------------
# RecoveryAgent
# ---------------------------------------------------------------------------

class TestRecoveryAgent:

    def test_rollback_commands_config_cisco(self):
        step = _make_step("s1", 1, raw_cmd="router bgp 100", mode="config", vendor="cisco")
        agent = RecoveryAgent()
        cmds = agent._build_rollback_commands(step)
        assert "no router bgp 100" in cmds

    def test_rollback_commands_juniper_set(self):
        step = _make_step("s1", 1, raw_cmd="set protocols bgp group PEERS", mode="config", vendor="juniper")
        agent = RecoveryAgent()
        cmds = agent._build_rollback_commands(step)
        assert "delete protocols bgp group PEERS" in cmds

    def test_rollback_commands_huawei(self):
        step = _make_step("s1", 1, raw_cmd="bgp 100", mode="config", vendor="huawei")
        agent = RecoveryAgent()
        cmds = agent._build_rollback_commands(step)
        assert "undo bgp 100" in cmds

    def test_no_rollback_for_verify_step(self):
        step = _make_step("s1", 1, raw_cmd="show bgp", action_type=ActionType.VERIFY, mode="exec")
        agent = RecoveryAgent()
        cmds = agent._build_rollback_commands(step)
        assert cmds == []

    def test_no_rollback_for_exec_command(self):
        step = _make_step("s1", 1, raw_cmd="show version", mode="exec", vendor="cisco")
        agent = RecoveryAgent()
        cmds = agent._build_rollback_commands(step)
        assert cmds == []  # exec mode commands are not reversed

    def test_rollback_step_success(self):
        step = _make_step("s1", 1, raw_cmd="router bgp 100", mode="config", vendor="cisco",
                          devices=["PE1"])
        model = _make_model([step])
        eid = state_manager.create(model)
        # Set step to PASSED so rollback makes sense
        state_manager.transition_step(eid, "s1", ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s1", ExecutionStatus.PASSED)

        driver = MockDriver(hostname="PE1")
        driver.connect()
        agent = RecoveryAgent()
        ok = agent.rollback_step(step, driver, execution_id=eid)
        assert ok is True
        state = state_manager.get(eid)
        assert state.steps["s1"].status == ExecutionStatus.ROLLED_BACK

    def test_rollback_all_reverses_passed_steps(self):
        steps = [
            _make_step("s1", 1, raw_cmd="router bgp 100", mode="config", vendor="cisco", devices=["PE1"]),
            _make_step("s2", 2, raw_cmd="neighbor 10.0.0.1", mode="config", vendor="cisco", devices=["PE1"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model)
        # Mark s1 as passed, s2 as failed
        state_manager.transition_step(eid, "s1", ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s1", ExecutionStatus.PASSED)
        state_manager.transition_step(eid, "s2", ExecutionStatus.RUNNING)
        state_manager.transition_step(eid, "s2", ExecutionStatus.FAILED)

        driver = MockDriver(hostname="PE1")
        driver.connect()
        agent = RecoveryAgent()
        ok = agent.rollback_all(eid, model, {"PE1": driver})
        assert ok is True
        state = state_manager.get(eid)
        assert state.steps["s1"].status == ExecutionStatus.ROLLED_BACK

    def test_make_decision_retry_within_limit(self):
        step = _make_step("s1", 1)
        step.execution_policy = ExecutionPolicy(retry_count=3)
        model = _make_model([step])
        agent = RecoveryAgent()
        decision = agent.make_decision(step, "timeout", attempt=1, model=model)
        assert decision == "RETRY"

    def test_make_decision_abort_after_max_retries(self):
        step = _make_step("s1", 1)
        step.execution_policy = ExecutionPolicy(retry_count=3)
        model = _make_model([step], failure_strategy=FailureStrategy.ABORT)
        agent = RecoveryAgent()
        decision = agent.make_decision(step, "some error", attempt=3, model=model)
        assert decision == "ABORT"

    def test_make_decision_continue_strategy(self):
        step = _make_step("s1", 1)
        step.execution_policy = ExecutionPolicy(retry_count=1)
        model = _make_model([step], failure_strategy=FailureStrategy.CONTINUE)
        agent = RecoveryAgent()
        decision = agent.make_decision(step, "error", attempt=1, model=model)
        assert decision == "CONTINUE"

    def test_make_decision_fatal_error_escalate(self):
        step = _make_step("s1", 1)
        step.execution_policy = ExecutionPolicy(retry_count=1)
        model = _make_model([step])
        agent = RecoveryAgent()
        decision = agent.make_decision(step, "Authentication failed", attempt=1, model=model)
        assert decision == "ESCALATE"


# ---------------------------------------------------------------------------
# ExecutionAgent — end-to-end with MockDriver
# ---------------------------------------------------------------------------

class TestExecutionAgentDryRun:

    def test_simple_execution_passes(self):
        steps = [
            _make_step("s1", 1, devices=["PE1"]),
            _make_step("s2", 2, devices=["PE1"], dependencies=["s1"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model, dry_run=True)

        from execution_engine.execution_agent import ExecutionAgent
        driver = MockDriver(hostname="PE1", default_response="OK")
        driver.connect()
        agent = ExecutionAgent(dry_run=True)
        final = agent.run(eid, device_map={"PE1": driver})

        assert final == ExecutionStatus.PASSED
        state = state_manager.get(eid)
        assert state.steps["s1"].status == ExecutionStatus.PASSED
        assert state.steps["s2"].status == ExecutionStatus.PASSED

    def test_idempotency_skip(self):
        step = _make_step("s1", 1, devices=["PE1"])
        step.idempotency_rules = [
            IdempotencyRule(
                check_cmd="show bgp summary",
                skip_pattern="Established",
                description="BGP already up",
            )
        ]
        model = _make_model([step])
        eid = state_manager.create(model, dry_run=True)

        from execution_engine.execution_agent import ExecutionAgent
        driver = MockDriver(
            hostname="PE1",
            responses={"show bgp summary": "BGP neighbor 10.0.0.1 Established"},
        )
        driver.connect()
        agent = ExecutionAgent(dry_run=True)
        final = agent.run(eid, device_map={"PE1": driver})

        assert final == ExecutionStatus.PASSED
        state = state_manager.get(eid)
        assert state.steps["s1"].status == ExecutionStatus.SKIPPED

    def test_kill_switch_aborts(self):
        from execution_engine.kill_switch import kill_switch
        steps = [
            _make_step("s1", 1, devices=["PE1"]),
            _make_step("s2", 2, devices=["PE1"], dependencies=["s1"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model, dry_run=True)

        kill_switch.engage(reason="test")
        try:
            from execution_engine.execution_agent import ExecutionAgent
            agent = ExecutionAgent(dry_run=True)
            final = agent.run(eid, device_map={})
            assert final == ExecutionStatus.ABORTED
        finally:
            kill_switch.clear()

    def test_no_steps_raises(self):
        model = _make_model([])
        eid = state_manager.create(model, dry_run=True)

        from execution_engine.execution_agent import ExecutionAgent
        with pytest.raises(ValueError, match="no steps"):
            ExecutionAgent(dry_run=True).run(eid)

    def test_parallel_wave_execution(self):
        """Steps without dependencies should run in parallel (wave 0)."""
        steps = [
            _make_step("s1", 1, devices=["PE1"]),
            _make_step("s2", 2, devices=["PE2"]),
            _make_step("s3", 3, devices=["PE3"]),
        ]
        model = _make_model(steps)
        eid = state_manager.create(model, dry_run=True)

        from execution_engine.execution_agent import ExecutionAgent
        drivers = {
            "PE1": MockDriver(hostname="PE1", default_response="OK"),
            "PE2": MockDriver(hostname="PE2", default_response="OK"),
            "PE3": MockDriver(hostname="PE3", default_response="OK"),
        }
        for d in drivers.values():
            d.connect()

        agent = ExecutionAgent(dry_run=True)
        final = agent.run(eid, device_map=drivers)
        assert final == ExecutionStatus.PASSED
        state = state_manager.get(eid)
        for sid in ["s1", "s2", "s3"]:
            assert state.steps[sid].status == ExecutionStatus.PASSED
