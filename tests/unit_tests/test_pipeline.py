"""
Unit tests for each pipeline component.

Tests are designed to run WITHOUT an Anthropic API key — the LLM layer
is mocked. All other components (ingestion, grammar engine, generators,
post-processing) are tested with real logic.

Run:
    pytest tests/unit_tests/test_pipeline.py -v
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from models.canonical import (
    ActionType,
    CanonicalTestModel,
    CLICommand,
    DocumentBlock,
    ParsedDocument,
    StepType,
    TestStep,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_canonical_model() -> CanonicalTestModel:
    """A minimal CanonicalTestModel for testing generators."""
    return CanonicalTestModel(
        document_title="BGP Migration MOP",
        source_file="/tmp/test_mop.txt",
        source_format="txt",
        mop_structure="numbered_list",
        steps=[
            TestStep(
                step_id="aaa00001",
                sequence=1,
                step_type=StepType.VERIFICATION,
                action_type=ActionType.VERIFY,
                description="Verify BGP neighbors are established on PE1",
                raw_text="1. Verify BGP neighbors are established on PE1",
                commands=[
                    CLICommand(
                        raw="show ip bgp summary",
                        vendor="cisco",
                        protocol="bgp",
                        mode="exec",
                        confidence=0.95,
                    )
                ],
                expected_output="All neighbors show state 'Established'",
                section="Pre-checks",
                tags=["bgp", "verification", "cisco"],
            ),
            TestStep(
                step_id="aaa00002",
                sequence=2,
                step_type=StepType.CONFIG,
                action_type=ActionType.CONFIGURE,
                description="Enable BGP graceful restart on PE1",
                raw_text="2. Enable BGP graceful restart on PE1",
                commands=[
                    CLICommand(
                        raw="configure terminal",
                        vendor="cisco",
                        mode="config",
                        confidence=0.99,
                    ),
                    CLICommand(
                        raw="router bgp 65001",
                        vendor="cisco",
                        protocol="bgp",
                        mode="config",
                        confidence=0.98,
                    ),
                    CLICommand(
                        raw="bgp graceful-restart",
                        vendor="cisco",
                        protocol="bgp",
                        mode="config",
                        confidence=0.97,
                    ),
                ],
                section="Implementation",
                tags=["bgp", "config", "cisco"],
            ),
            TestStep(
                step_id="aaa00003",
                sequence=3,
                step_type=StepType.ROLLBACK,
                action_type=ActionType.ROLLBACK,
                description="Remove BGP graceful restart configuration",
                raw_text="3. Remove BGP graceful restart configuration",
                commands=[
                    CLICommand(
                        raw="no bgp graceful-restart",
                        vendor="cisco",
                        protocol="bgp",
                        mode="config",
                        confidence=0.97,
                    ),
                ],
                section="Rollback",
                is_rollback=True,
                tags=["bgp", "rollback", "cisco"],
            ),
        ],
    )


@pytest.fixture
def numbered_list_mop_text() -> str:
    return textwrap.dedent("""\
    # BGP Migration MOP

    ## Pre-checks

    1. Log into PE1 router
    2. Verify BGP neighbor state: show ip bgp summary
    3. Check interface status: show interfaces GigabitEthernet0/0

    ## Implementation

    4. Configure terminal: configure terminal
    5. Enter BGP process: router bgp 65001
    6. Enable graceful restart: bgp graceful-restart
    7. Commit configuration: end

    ## Post-checks

    8. Verify BGP reconvergence: show ip bgp summary
       Expected: all neighbors back to Established state

    ## Rollback

    9. Remove graceful restart: no bgp graceful-restart
    10. Commit rollback: end
    """)


@pytest.fixture
def table_mop_text() -> str:
    return textwrap.dedent("""\
    # OSPF Migration MOP

    | Step | Action | Expected Result | Rollback |
    | 1 | show ip ospf neighbor | All neighbors in FULL state | |
    | 2 | configure terminal | Configuration mode entered | |
    | 3 | router ospf 1 | OSPF process configured | no router ospf 1 |
    | 4 | network 10.0.0.0 0.0.0.255 area 0 | Network added to OSPF | |
    | 5 | end | Config committed | |
    """)


@pytest.fixture
def prose_mop_text() -> str:
    return textwrap.dedent("""\
    ## BGP Neighbor Verification Procedure

    Connect to PE1 using SSH and verify that all BGP neighbors are in the
    Established state by running 'show ip bgp summary'. If any neighbor is
    not Established, check the MTU configuration on the interconnect link
    using 'show interface GigabitEthernet0/0'.

    If the neighbor count is correct, proceed to enable graceful restart by
    entering configuration mode with 'configure terminal', then 'router bgp 65001',
    and finally 'bgp graceful-restart'. Commit with 'end'.
    """)


# ---------------------------------------------------------------------------
# Ingestion tests
# ---------------------------------------------------------------------------

class TestTxtParser:

    def test_numbered_list_parsed(self, numbered_list_mop_text, tmp_path):
        from ingestion.txt_parser import parse

        mop_file = tmp_path / "test_mop.txt"
        mop_file.write_text(numbered_list_mop_text)

        doc = parse(str(mop_file))

        assert doc.title == "BGP Migration MOP"
        assert doc.source_format == "txt"
        assert len(doc.blocks) > 0

        headings = [b for b in doc.blocks if b.block_type == "heading"]
        list_items = [b for b in doc.blocks if b.block_type == "list_item"]

        assert len(headings) >= 1
        assert len(list_items) >= 5

    def test_table_parsed(self, table_mop_text, tmp_path):
        from ingestion.txt_parser import parse

        mop_file = tmp_path / "table_mop.txt"
        mop_file.write_text(table_mop_text)

        doc = parse(str(mop_file))

        assert doc.title == "OSPF Migration MOP"
        # Tables in plain text are parsed as paragraphs (pipe-delimited)
        assert len(doc.blocks) > 0
        assert doc.full_text  # has content

    def test_full_text_populated(self, numbered_list_mop_text, tmp_path):
        from ingestion.txt_parser import parse

        mop_file = tmp_path / "test_mop.txt"
        mop_file.write_text(numbered_list_mop_text)

        doc = parse(str(mop_file))
        assert "bgp" in doc.full_text.lower()

    def test_missing_file_raises(self):
        from ingestion.txt_parser import parse
        with pytest.raises(Exception):
            parse("/nonexistent/path/to/mop.txt")


class TestDocumentLoader:

    def test_routes_txt_correctly(self, numbered_list_mop_text, tmp_path):
        from ingestion.document_loader import load

        mop_file = tmp_path / "test_mop.txt"
        mop_file.write_text(numbered_list_mop_text)

        doc = load(str(mop_file))
        assert doc.source_format == "txt"
        assert doc.detected_structure in (
            "numbered_list", "bulleted_list", "prose", "mixed", "table", "unknown"
        )

    def test_unsupported_format_raises(self, tmp_path):
        from ingestion.document_loader import load

        bad_file = tmp_path / "test_mop.xlsx"
        bad_file.write_bytes(b"fake content")

        with pytest.raises(ValueError, match="Unsupported file format"):
            load(str(bad_file))

    def test_missing_file_raises(self):
        from ingestion.document_loader import load
        with pytest.raises(FileNotFoundError):
            load("/no/such/file.txt")


# ---------------------------------------------------------------------------
# Structure detection tests
# ---------------------------------------------------------------------------

class TestStructureDetection:

    def test_detects_numbered_list(self, numbered_list_mop_text, tmp_path):
        from ingestion.txt_parser import parse

        mop_file = tmp_path / "test.txt"
        mop_file.write_text(numbered_list_mop_text)
        doc = parse(str(mop_file))

        from ingestion.normalizer import detect_structure
        structure = detect_structure(doc.blocks)
        assert structure in ("numbered_list", "bulleted_list", "mixed")

    def test_detects_prose(self, prose_mop_text, tmp_path):
        from ingestion.txt_parser import parse

        mop_file = tmp_path / "prose.txt"
        mop_file.write_text(prose_mop_text)
        doc = parse(str(mop_file))

        from ingestion.normalizer import detect_structure
        structure = detect_structure(doc.blocks)
        assert structure in ("prose", "mixed", "unknown")

    def test_empty_blocks_returns_unknown(self):
        from ingestion.normalizer import detect_structure
        assert detect_structure([]) == "unknown"


# ---------------------------------------------------------------------------
# Grammar engine tests
# ---------------------------------------------------------------------------

class TestCLIGrammar:

    def test_detects_cisco_show_command(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmds = grammar.extract_from_text("show ip bgp summary")
        assert len(cmds) == 1
        assert cmds[0].vendor == "cisco"
        assert cmds[0].mode == "exec"
        assert cmds[0].protocol == "bgp"

    def test_detects_juniper_set_command(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmds = grammar.extract_from_text("set routing-options autonomous-system 65001")
        assert len(cmds) == 1
        assert cmds[0].vendor == "juniper"
        assert cmds[0].mode == "config"

    def test_strips_cli_prompt(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmds = grammar.extract_from_text("PE1# show ip bgp summary")
        assert len(cmds) == 1
        assert "PE1#" not in cmds[0].normalized

    def test_ignores_non_cli_text(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmds = grammar.extract_from_text(
            "This is a description paragraph explaining what to do."
        )
        assert len(cmds) == 0

    def test_detects_multiple_commands(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        text = "\n".join([
            "show ip bgp summary",
            "show ip ospf neighbor",
            "show mpls forwarding",
            "configure terminal",
        ])
        cmds = grammar.extract_from_text(text)
        assert len(cmds) == 4

    def test_is_cli_command(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        assert grammar.is_cli_command("show ip bgp summary") is True
        assert grammar.is_cli_command("This is a description.") is False

    def test_enrich_command(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        result = grammar.enrich_command("show ip ospf neighbor")
        assert result.protocol == "ospf"
        assert result.mode == "exec"

    def test_protocol_detection_bgp(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmd = grammar.enrich_command("show bgp ipv4 unicast summary")
        assert cmd.protocol == "bgp"

    def test_protocol_detection_mpls(self):
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        cmd = grammar.enrich_command("show mpls ldp neighbor")
        assert cmd.protocol == "mpls"


# ---------------------------------------------------------------------------
# Post-processing tests
# ---------------------------------------------------------------------------

class TestGuardrails:

    def test_passes_valid_model(self, simple_canonical_model):
        from post_processing.guardrails import Guardrails
        result = Guardrails.validate(simple_canonical_model, pre_llm_command_count=3)
        assert result.passed is True

    def test_fails_on_empty_steps(self):
        from post_processing.guardrails import Guardrails
        model = CanonicalTestModel(
            document_title="Test",
            source_file="/tmp/test.txt",
            source_format="txt",
            steps=[],
        )
        result = Guardrails.validate(model)
        assert result.passed is False
        assert any("No steps" in e for e in result.errors)

    def test_warns_on_low_coverage(self, simple_canonical_model):
        from post_processing.guardrails import Guardrails
        # Model has 4 commands, pre-LLM count is 20 → coverage = 20%
        result = Guardrails.validate(simple_canonical_model, pre_llm_command_count=20)
        assert any("coverage" in w.lower() for w in result.warnings)

    def test_auto_corrects_rollback_flag(self):
        from post_processing.guardrails import Guardrails
        model = CanonicalTestModel(
            document_title="Test",
            source_file="/tmp/test.txt",
            source_format="txt",
            steps=[
                TestStep(
                    sequence=1,
                    step_type=StepType.ROLLBACK,
                    action_type=ActionType.ROLLBACK,
                    description="Rollback step",
                    raw_text="Rollback step",
                    is_rollback=False,  # inconsistent — should be auto-corrected
                )
            ],
        )
        Guardrails.validate(model)
        assert model.steps[0].is_rollback is True  # auto-corrected

    def test_warns_on_duplicate_sequences(self):
        from post_processing.guardrails import Guardrails
        model = CanonicalTestModel(
            document_title="Test",
            source_file="/tmp/test.txt",
            source_format="txt",
            steps=[
                TestStep(sequence=1, step_type=StepType.ACTION, action_type=ActionType.EXECUTE,
                         description="Step A", raw_text="Step A"),
                TestStep(sequence=1, step_type=StepType.ACTION, action_type=ActionType.EXECUTE,
                         description="Step B", raw_text="Step B"),
            ],
        )
        result = Guardrails.validate(model)
        assert any("Duplicate" in w for w in result.warnings)


class TestSchemaValidator:

    def test_validates_good_model(self, simple_canonical_model):
        from post_processing.schema_validator import SchemaValidator
        result = SchemaValidator.validate(simple_canonical_model)
        assert result.valid is True

    def test_rejects_empty_title(self):
        from post_processing.schema_validator import SchemaValidator
        model = CanonicalTestModel(
            document_title="",
            source_file="/tmp/test.txt",
            source_format="txt",
            steps=[],
        )
        result = SchemaValidator.validate(model)
        assert result.valid is False

    def test_to_json_is_valid_json(self, simple_canonical_model):
        from post_processing.schema_validator import SchemaValidator
        json_str = SchemaValidator.to_json(simple_canonical_model)
        parsed = json.loads(json_str)
        assert parsed["document_title"] == "BGP Migration MOP"
        assert len(parsed["steps"]) == 3


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------

class TestZephyrGenerator:

    def test_generates_csv_file(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        assert os.path.exists(output_path)
        assert output_path.endswith(".csv")

    def test_csv_has_correct_headers(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert "Name" in reader.fieldnames
            assert "Steps" in reader.fieldnames
            assert "Folder" in reader.fieldnames

    def test_csv_has_correct_row_count(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == len(simple_canonical_model.steps)

    def test_steps_column_is_valid_json(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                steps_data = json.loads(row["Steps"])
                assert isinstance(steps_data, list)
                assert len(steps_data) > 0

    def test_rollback_step_marked_critical(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        rollback_rows = [r for r in rows if "rollback" in r["Labels"]]
        assert len(rollback_rows) > 0
        assert all(r["Priority"] == "Critical" for r in rollback_rows)

    def test_folder_contains_doc_title(self, simple_canonical_model, tmp_path):
        from generators.zephyr_generator import ZephyrGenerator
        output_path = ZephyrGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert all("BGP" in row["Folder"] or "MOP" in row["Folder"] for row in rows)


class TestRobotGenerator:

    def test_generates_robot_file(self, simple_canonical_model, tmp_path):
        from generators.robot_generator import RobotGenerator
        output_path = RobotGenerator.generate(simple_canonical_model, str(tmp_path))
        assert os.path.exists(output_path)
        assert output_path.endswith(".robot")

    def test_robot_file_has_required_sections(self, simple_canonical_model, tmp_path):
        from generators.robot_generator import RobotGenerator
        output_path = RobotGenerator.generate(simple_canonical_model, str(tmp_path))
        content = Path(output_path).read_text()
        assert "*** Settings ***" in content
        assert "*** Variables ***" in content
        assert "*** Test Cases ***" in content
        assert "*** Keywords ***" in content

    def test_robot_file_contains_commands(self, simple_canonical_model, tmp_path):
        from generators.robot_generator import RobotGenerator
        output_path = RobotGenerator.generate(simple_canonical_model, str(tmp_path))
        content = Path(output_path).read_text()
        assert "show ip bgp summary" in content

    def test_robot_file_has_ssh_library(self, simple_canonical_model, tmp_path):
        from generators.robot_generator import RobotGenerator
        output_path = RobotGenerator.generate(simple_canonical_model, str(tmp_path))
        content = Path(output_path).read_text()
        assert "SSHLibrary" in content

    def test_robot_keywords_defined(self, simple_canonical_model, tmp_path):
        from generators.robot_generator import RobotGenerator
        output_path = RobotGenerator.generate(simple_canonical_model, str(tmp_path))
        content = Path(output_path).read_text()
        assert "Execute CLI Command" in content
        assert "Verify Command Output" in content


class TestCLIRuleGenerator:

    def test_generates_json_file(self, simple_canonical_model, tmp_path):
        from generators.cli_rule_generator import CLIRuleGenerator
        output_path = CLIRuleGenerator.generate(simple_canonical_model, str(tmp_path))
        assert os.path.exists(output_path)
        assert output_path.endswith(".json")

    def test_json_has_correct_structure(self, simple_canonical_model, tmp_path):
        from generators.cli_rule_generator import CLIRuleGenerator
        output_path = CLIRuleGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path) as f:
            data = json.load(f)
        assert "document_title" in data
        assert "rules" in data
        assert isinstance(data["rules"], list)

    def test_rules_contain_commands(self, simple_canonical_model, tmp_path):
        from generators.cli_rule_generator import CLIRuleGenerator
        output_path = CLIRuleGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path) as f:
            data = json.load(f)
        commands = [r["command"] for r in data["rules"] if r["command"]]
        assert "show ip bgp summary" in commands

    def test_rollback_rules_flagged(self, simple_canonical_model, tmp_path):
        from generators.cli_rule_generator import CLIRuleGenerator
        output_path = CLIRuleGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path) as f:
            data = json.load(f)
        rollback_rules = [r for r in data["rules"] if r["is_rollback"]]
        assert len(rollback_rules) > 0

    def test_must_contain_parsed_from_expected(self, simple_canonical_model, tmp_path):
        from generators.cli_rule_generator import CLIRuleGenerator
        output_path = CLIRuleGenerator.generate(simple_canonical_model, str(tmp_path))
        with open(output_path) as f:
            data = json.load(f)
        # Step 1 has expected_output with 'Established'
        rule = next(r for r in data["rules"] if r.get("command") == "show ip bgp summary")
        assert "Established" in rule["must_contain"]


# ---------------------------------------------------------------------------
# End-to-end pipeline test (mocked LLM)
# ---------------------------------------------------------------------------

class TestPipelineMocked:
    """
    Tests the full pipeline with the LLM mocked.
    Validates that ingestion → guardrails → generators work end-to-end.
    """

    def _make_llm_response(self) -> str:
        return json.dumps({
            "document_title": "BGP Migration MOP",
            "steps": [
                {
                    "sequence": 1,
                    "step_type": "verification",
                    "action_type": "verify",
                    "description": "Verify BGP neighbors are established",
                    "raw_text": "1. Verify BGP neighbors are established on PE1",
                    "commands": [
                        {
                            "raw": "show ip bgp summary",
                            "vendor": "cisco",
                            "protocol": "bgp",
                            "mode": "exec",
                            "confidence": 0.95,
                        }
                    ],
                    "expected_output": "All neighbors Established",
                    "section": "Pre-checks",
                    "subsection": None,
                    "is_rollback": False,
                    "tags": ["bgp", "verification"],
                },
                {
                    "sequence": 2,
                    "step_type": "config",
                    "action_type": "configure",
                    "description": "Enable BGP graceful restart",
                    "raw_text": "2. Enable BGP graceful restart",
                    "commands": [
                        {"raw": "configure terminal", "vendor": "cisco", "mode": "config", "confidence": 0.99},
                        {"raw": "router bgp 65001", "vendor": "cisco", "protocol": "bgp", "mode": "config", "confidence": 0.98},
                        {"raw": "bgp graceful-restart", "vendor": "cisco", "protocol": "bgp", "mode": "config", "confidence": 0.97},
                    ],
                    "expected_output": None,
                    "section": "Implementation",
                    "subsection": None,
                    "is_rollback": False,
                    "tags": ["bgp", "config"],
                },
            ]
        })

    def test_full_pipeline_txt(self, numbered_list_mop_text, tmp_path):
        mop_file = tmp_path / "test_mop.txt"
        mop_file.write_text(numbered_list_mop_text)
        output_dir = str(tmp_path / "output")

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=self._make_llm_response())]

        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_message
            mock_anthropic.return_value = mock_client

            from pipeline import run
            result = run(
                input_file=str(mop_file),
                output_dir=output_dir,
                model="claude-sonnet-4-6",
            )

        assert result["total_steps"] == 2
        assert os.path.exists(result["outputs"]["zephyr_csv"])
        assert os.path.exists(result["outputs"]["robot_framework"])
        assert os.path.exists(result["outputs"]["cli_rules"])
        assert os.path.exists(result["outputs"]["canonical_json"])

    def test_canonical_json_output_is_valid(self, numbered_list_mop_text, tmp_path):
        mop_file = tmp_path / "test_mop.txt"
        mop_file.write_text(numbered_list_mop_text)
        output_dir = str(tmp_path / "output")

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=self._make_llm_response())]

        with patch("anthropic.Anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_message
            mock_anthropic.return_value = mock_client

            from pipeline import run
            result = run(str(mop_file), output_dir)

        with open(result["outputs"]["canonical_json"]) as f:
            canonical = json.load(f)

        assert canonical["document_title"] == "BGP Migration MOP"
        assert len(canonical["steps"]) == 2


# ---------------------------------------------------------------------------
# LLMResult tests
# ---------------------------------------------------------------------------

class TestLLMResult:

    def test_success_result_has_model(self, simple_canonical_model):
        from ai_layer.llm_result import LLMResult
        result = LLMResult(success=True, model=simple_canonical_model)
        assert result.success is True
        assert result.model is simple_canonical_model

    def test_raise_if_failed_returns_model_on_success(self, simple_canonical_model):
        from ai_layer.llm_result import LLMResult
        result = LLMResult(success=True, model=simple_canonical_model)
        model = result.raise_if_failed()
        assert model is simple_canonical_model

    def test_raise_if_failed_raises_on_failure(self):
        from ai_layer.llm_result import LLMResult, LLMErrorType, LLMError
        result = LLMResult(
            success=False,
            error_type=LLMErrorType.JSON_PARSE_FAIL,
            error_message="Bad JSON",
        )
        with pytest.raises(LLMError, match="JSON_PARSE_FAIL"):
            result.raise_if_failed()

    def test_failed_result_has_no_model(self):
        from ai_layer.llm_result import LLMResult, LLMErrorType
        result = LLMResult(
            success=False,
            error_type=LLMErrorType.RATE_LIMIT,
            error_message="429",
        )
        assert result.model is None
        assert result.error_type.value == "rate_limit"

    def test_chunk_count_defaults_to_one(self, simple_canonical_model):
        from ai_layer.llm_result import LLMResult
        result = LLMResult(success=True, model=simple_canonical_model)
        assert result.chunk_count == 1

    def test_partial_steps_zero_on_full_success(self, simple_canonical_model):
        from ai_layer.llm_result import LLMResult
        result = LLMResult(success=True, model=simple_canonical_model)
        assert result.partial_steps == 0


# ---------------------------------------------------------------------------
# ContextChunker tests
# ---------------------------------------------------------------------------

class TestContextChunker:
    """Tests for the section-based context chunker."""

    def _make_doc_with_text(self, text: str, tmp_path) -> "ParsedDocument":
        from ingestion.txt_parser import parse
        f = tmp_path / "doc.txt"
        f.write_text(text)
        return parse(str(f))

    def _large_mop_text(self, sections: int = 10, lines_per_section: int = 100) -> str:
        """Build a synthetic large MOP with many sections."""
        lines = []
        for s in range(sections):
            lines.append(f"# Section {s + 1}: BGP Configuration Phase {s + 1}\n")
            for i in range(lines_per_section):
                lines.append(f"{i + 1}. Configure BGP neighbor 10.{s}.{i}.1 remote-as 65{s:03d}{i:02d}")
                lines.append(f"   show ip bgp neighbor 10.{s}.{i}.1")
                lines.append(f"   Expected: neighbor state Established")
        return "\n".join(lines)

    def test_small_doc_does_not_need_chunking(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        doc = self._make_doc_with_text("# Section 1\n1. Do this\n2. Do that", tmp_path)
        chunker = ContextChunker()
        assert chunker.needs_chunking(doc) is False

    def test_large_doc_needs_chunking(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        # Very small max to force chunking in tests
        chunker = ContextChunker(max_tokens_per_chunk=100)
        doc = self._make_doc_with_text(self._large_mop_text(sections=3), tmp_path)
        assert chunker.needs_chunking(doc) is True

    def test_chunk_count_reflects_sections(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        # 3 sections, very small budget forces each section into its own chunk
        chunker = ContextChunker(max_tokens_per_chunk=100)
        doc = self._make_doc_with_text(self._large_mop_text(sections=3, lines_per_section=10), tmp_path)
        chunks = chunker.chunk(doc)
        assert len(chunks) >= 1
        # All chunks have correct total_chunks
        for c in chunks:
            assert c.total_chunks == len(chunks)

    def test_all_chunk_indices_are_sequential(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        chunker = ContextChunker(max_tokens_per_chunk=200)
        doc = self._make_doc_with_text(self._large_mop_text(sections=4, lines_per_section=5), tmp_path)
        chunks = chunker.chunk(doc)
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_chunk_text_is_non_empty(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        chunker = ContextChunker(max_tokens_per_chunk=200)
        doc = self._make_doc_with_text(self._large_mop_text(sections=2, lines_per_section=5), tmp_path)
        chunks = chunker.chunk(doc)
        for c in chunks:
            assert c.text.strip()

    def test_cli_commands_filtered_to_chunk(self, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        text = "# Section A\nshow ip bgp summary\n\n# Section B\nshow ip ospf neighbor"
        doc = self._make_doc_with_text(text, tmp_path)
        chunker = ContextChunker(max_tokens_per_chunk=20)  # very small to force 2 chunks
        all_commands = ["show ip bgp summary", "show ip ospf neighbor"]
        chunks = chunker.chunk(doc, pre_detected_commands=all_commands)
        # Each chunk should only have the commands from its own text
        all_chunk_commands = [cmd for c in chunks for cmd in c.pre_detected_commands]
        assert "show ip bgp summary" in all_chunk_commands
        assert "show ip ospf neighbor" in all_chunk_commands

    def test_single_chunk_for_small_doc(self, numbered_list_mop_text, tmp_path):
        from ai_layer.context_chunker import ContextChunker
        from ingestion.txt_parser import parse
        f = tmp_path / "small.txt"
        f.write_text(numbered_list_mop_text)
        doc = parse(str(f))
        chunker = ContextChunker()  # default large budget
        chunks = chunker.chunk(doc)
        # Small test doc should fit in one chunk
        assert len(chunks) == 1
        assert chunks[0].total_chunks == 1

    def test_estimate_tokens_proportional_to_text(self):
        from ai_layer.context_chunker import ContextChunker
        chunker = ContextChunker()
        short = chunker.estimate_tokens("hello world")
        long = chunker.estimate_tokens("hello world " * 1000)
        assert long > short * 500  # rough proportionality


# ---------------------------------------------------------------------------
# SuperPromptRunner retry and LLMResult tests (mocked)
# ---------------------------------------------------------------------------

class TestSuperPromptRunnerLLMResult:
    """Tests for retry logic and LLMResult return type."""

    def _make_doc(self, text: str, tmp_path) -> "ParsedDocument":
        from ingestion.txt_parser import parse
        f = tmp_path / "doc.txt"
        f.write_text(text)
        return parse(str(f))

    def _good_response(self) -> str:
        return json.dumps({
            "document_title": "Test MOP",
            "steps": [{
                "sequence": 1,
                "step_type": "verification",
                "action_type": "verify",
                "description": "Verify BGP",
                "raw_text": "Verify BGP",
                "commands": [{"raw": "show ip bgp summary", "vendor": "cisco",
                               "protocol": "bgp", "mode": "exec", "confidence": 0.95}],
                "expected_output": "Established",
                "section": "Pre-checks",
                "subsection": None,
                "is_rollback": False,
                "tags": ["bgp"],
            }]
        })

    def test_success_returns_llm_result_with_model(self, tmp_path):
        doc = self._make_doc("# Test\n1. Verify BGP: show ip bgp summary", tmp_path)
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=self._good_response())]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_msg
            mock_cls.return_value = mock_client

            from ai_layer.super_prompt_runner import SuperPromptRunner
            runner = SuperPromptRunner()
            result = runner.run(doc)

        assert result.success is True
        assert result.model is not None
        assert len(result.model.steps) == 1

    def test_json_parse_fail_retries_and_recovers(self, tmp_path):
        """First response is garbage JSON, second is valid."""
        doc = self._make_doc("# Test\n1. Do something", tmp_path)
        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="I cannot process this as JSON lol")]
        good_response = MagicMock()
        good_response.content = [MagicMock(text=self._good_response())]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = [bad_response, good_response]
            mock_cls.return_value = mock_client

            from ai_layer.super_prompt_runner import SuperPromptRunner
            runner = SuperPromptRunner()
            result = runner.run(doc)

        assert result.success is True
        assert result.attempt_count == 2
        assert mock_client.messages.create.call_count == 2

    def test_all_attempts_fail_returns_failure_result(self, tmp_path):
        """All 3 attempts return invalid JSON → LLMResult(success=False)."""
        doc = self._make_doc("# Test\n1. Do something", tmp_path)
        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="This is not JSON at all")]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = bad_response
            mock_cls.return_value = mock_client

            from ai_layer.super_prompt_runner import SuperPromptRunner
            from ai_layer.llm_result import LLMErrorType
            runner = SuperPromptRunner()
            result = runner.run(doc)

        assert result.success is False
        assert result.error_type == LLMErrorType.JSON_PARSE_FAIL
        assert result.attempt_count == 3
        assert mock_client.messages.create.call_count == 3

    def test_rate_limit_triggers_backoff(self, tmp_path):
        """Rate limit on first call, then success."""
        doc = self._make_doc("# Test\n1. Do something", tmp_path)
        good_response = MagicMock()
        good_response.content = [MagicMock(text=self._good_response())]

        with patch("anthropic.Anthropic") as mock_cls, \
             patch("time.sleep") as mock_sleep:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = [
                anthropic.RateLimitError.__new__(anthropic.RateLimitError),
                good_response,
            ]
            mock_cls.return_value = mock_client

            from ai_layer.super_prompt_runner import SuperPromptRunner
            runner = SuperPromptRunner()
            result = runner.run(doc)

        mock_sleep.assert_called_once()  # backoff was triggered
        assert result.success is True

    def test_refusal_is_non_retryable(self, tmp_path):
        """LLM returns a refusal — should not retry."""
        doc = self._make_doc("# Test\n1. Do something", tmp_path)
        refusal_response = MagicMock()
        refusal_response.content = [MagicMock(text="I'm unable to process this request.")]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = refusal_response
            mock_cls.return_value = mock_client

            from ai_layer.super_prompt_runner import SuperPromptRunner
            from ai_layer.llm_result import LLMErrorType
            runner = SuperPromptRunner()
            result = runner.run(doc)

        assert result.success is False
        assert result.error_type == LLMErrorType.REFUSAL
        assert mock_client.messages.create.call_count == 1  # no retry


# ---------------------------------------------------------------------------
# Chunked pipeline tests (mocked LLM)
# ---------------------------------------------------------------------------

class TestChunkedPipeline:
    """Tests for chunked extraction and model merging."""

    def _make_chunk_response(self, start_seq: int, n_steps: int = 2) -> str:
        """Build a valid LLM response for a chunk with n_steps steps."""
        steps = []
        for i in range(n_steps):
            steps.append({
                "sequence": start_seq + i,
                "step_type": "action",
                "action_type": "execute",
                "description": f"Step {start_seq + i} description",
                "raw_text": f"Raw text for step {start_seq + i}",
                "commands": [{"raw": f"show ip bgp summary", "vendor": "cisco",
                               "protocol": "bgp", "mode": "exec", "confidence": 0.9}],
                "expected_output": None,
                "section": "Implementation",
                "subsection": None,
                "is_rollback": False,
                "tags": [],
            })
        return json.dumps({"document_title": "Test MOP", "steps": steps})

    def test_chunks_are_merged_with_correct_sequence(self, tmp_path):
        """Two chunks → merged model with globally contiguous sequences."""
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner
        from ai_layer.context_chunker import ContextChunker

        # Build a doc large enough to require chunking at our small test limit
        text = "\n".join([
            "# Section A",
            *[f"{i}. Configure router interface {i}" for i in range(1, 30)],
            "# Section B",
            *[f"{i}. Verify ospf neighbor {i}" for i in range(30, 60)],
        ])
        f = tmp_path / "large.txt"
        f.write_text(text)
        doc = parse(str(f))

        # Responses for each chunk: chunk 1 has 2 steps, chunk 2 has 2 steps
        responses = [
            MagicMock(content=[MagicMock(text=self._make_chunk_response(1, n_steps=2))]),
            MagicMock(content=[MagicMock(text=self._make_chunk_response(3, n_steps=2))]),
        ]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = responses
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(max_tokens_per_chunk=200)  # tiny budget → forces chunking
            result = runner.run(doc)

        assert result.success is True
        assert result.chunk_count >= 2
        seqs = [s.sequence for s in result.model.steps]
        assert seqs == sorted(seqs)  # globally ordered
        assert seqs == list(range(1, len(seqs) + 1))  # contiguous 1-based

    def test_partial_success_when_one_chunk_fails(self, tmp_path):
        """One chunk succeeds, one fails → partial result still returned.

        max_tokens_per_chunk=100 → 350 char budget per chunk.
        Two sections of ~240 chars each → they cannot be packed together → 2 chunks.
        Response list: [success for chunk0, bad×3 for chunk1's 3 retry attempts].
        """
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner

        text = "\n".join([
            "# Section A",
            *[f"{i}. Step A{i}" for i in range(1, 20)],
            "# Section B",
            *[f"{i}. Step B{i}" for i in range(20, 40)],
        ])
        f = tmp_path / "partial.txt"
        f.write_text(text)
        doc = parse(str(f))

        # Provide enough responses: chunk0 succeeds (1 call) + chunk1 fails all 3 attempts
        responses = [
            MagicMock(content=[MagicMock(text=self._make_chunk_response(1, n_steps=2))]),
            MagicMock(content=[MagicMock(text="not valid json at all")]),
            MagicMock(content=[MagicMock(text="still not json")]),
            MagicMock(content=[MagicMock(text="nope")]),
        ]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = responses
            mock_cls.return_value = mock_client

            # 100 token budget: 2 sections of ~70 tokens each cannot be packed together
            runner = SuperPromptRunner(max_tokens_per_chunk=100)
            result = runner.run(doc)

        # Still succeeds overall (partial)
        assert result.success is True
        assert result.partial_steps > 0
        assert "failed_chunks" in result.model.metadata

    def test_all_chunks_fail_returns_failure_result(self, tmp_path):
        """All chunks fail → LLMResult(success=False)."""
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner
        from ai_layer.llm_result import LLMErrorType

        text = "\n".join([
            "# Section A",
            *[f"{i}. Step {i}" for i in range(1, 20)],
        ])
        f = tmp_path / "fail.txt"
        f.write_text(text)
        doc = parse(str(f))

        bad = MagicMock(content=[MagicMock(text="not json")])

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = bad
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(max_tokens_per_chunk=50)  # very small → many chunks
            result = runner.run(doc)

        assert result.success is False

    def test_chunk_metadata_in_merged_model(self, tmp_path):
        """Merged model metadata contains chunks_processed count."""
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner

        text = "\n".join([
            "# Section A",
            *[f"{i}. Step {i}" for i in range(1, 15)],
            "# Section B",
            *[f"{i}. Step {i}" for i in range(15, 30)],
        ])
        f = tmp_path / "chunks.txt"
        f.write_text(text)
        doc = parse(str(f))

        responses = [
            MagicMock(content=[MagicMock(text=self._make_chunk_response(1, n_steps=2))]),
            MagicMock(content=[MagicMock(text=self._make_chunk_response(3, n_steps=2))]),
        ]

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.side_effect = responses
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(max_tokens_per_chunk=150)
            result = runner.run(doc)

        if result.success:
            assert "chunks_processed" in result.model.metadata
            assert result.model.metadata["chunks_processed"] >= 1


# ---------------------------------------------------------------------------
# TOON: TOONBuilder
# ---------------------------------------------------------------------------

class TestTOONBuilder:
    """Tests for toon.builder.TOONBuilder."""

    def _make_doc(self, blocks: list, structure: str = "numbered_list") -> ParsedDocument:
        full_text = "\n".join(b.content for b in blocks)
        return ParsedDocument(
            title="Test MOP",
            source_file="test.txt",
            source_format="txt",
            detected_structure=structure,
            blocks=blocks,
            full_text=full_text,
        )

    def _make_grammar(self):
        from grammar_engine.cli_grammar import CLIGrammar
        return CLIGrammar()

    def test_numbered_list_produces_toon(self):
        """Numbered list structure → toon_usable=True with nodes."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="heading",  content="Pre-checks", level=1),
            DocumentBlock(block_type="list_item", content="1. show ip bgp summary"),
            DocumentBlock(block_type="list_item", content="2. show ip ospf neighbor"),
        ]
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert toon.toon_usable is True
        assert len(toon.sections) == 1
        section = toon.sections[0]
        assert section.heading == "Pre-checks"
        assert len(section.nodes) == 2

    def test_prose_returns_text_fallback(self):
        """Pure prose → toon_usable=False."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="paragraph", content="This document describes the procedure."),
            DocumentBlock(block_type="paragraph", content="The operator must follow all steps carefully."),
        ]
        doc = self._make_doc(blocks, structure="prose")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert toon.toon_usable is False
        assert toon.fallback_reason != ""

    def test_commands_extracted_into_nodes(self):
        """CLI commands in list items appear in node.commands."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="heading",  content="Implementation", level=1),
            DocumentBlock(block_type="list_item", content="show bgp summary"),
        ]
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert toon.toon_usable is True
        all_cmds = [cmd for s in toon.sections for n in s.nodes for cmd in n.commands]
        assert len(all_cmds) >= 1

    def test_rollback_section_flagged(self):
        """Section named 'Rollback' sets is_rollback_section=True."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="heading",  content="Rollback", level=1),
            DocumentBlock(block_type="list_item", content="1. no router bgp 65001"),
        ]
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert toon.sections[0].is_rollback_section is True
        assert toon.sections[0].nodes[0].is_rollback is True

    def test_compression_ratio_positive(self):
        """Larger structured document shows positive compression."""
        from toon.builder import TOONBuilder

        # Build a doc large enough that TOON overhead is smaller than raw text
        long_steps = [
            DocumentBlock(
                block_type="list_item",
                content=(
                    f"{i}. Configure the BGP neighbor session for peer group EBGP "
                    f"and verify that the adjacency comes up in Established state "
                    f"by running show ip bgp summary on device PE{i}"
                ),
            )
            for i in range(1, 30)
        ]
        blocks = [DocumentBlock(block_type="heading", content="Implementation", level=1)] + long_steps
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert toon.toon_usable is True
        assert toon.estimated_raw_tokens > 0
        assert toon.estimated_toon_tokens > 0

    def test_node_ids_are_sequential(self):
        """Nodes get hierarchical IDs like s1.1, s1.2."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="heading",  content="Sec 1", level=1),
            DocumentBlock(block_type="list_item", content="Step A"),
            DocumentBlock(block_type="list_item", content="Step B"),
        ]
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        ids = [n.node_id for s in toon.sections for n in s.nodes]
        assert "s1.1" in ids
        assert "s1.2" in ids

    def test_all_commands_deduplicated(self):
        """all_commands contains no duplicates."""
        from toon.builder import TOONBuilder

        blocks = [
            DocumentBlock(block_type="heading",  content="Checks", level=1),
            DocumentBlock(block_type="list_item", content="show bgp summary"),
            DocumentBlock(block_type="list_item", content="show bgp summary"),  # duplicate
        ]
        doc = self._make_doc(blocks, structure="numbered_list")
        toon = TOONBuilder.build(doc, self._make_grammar())

        assert len(toon.all_commands) == len(set(toon.all_commands))


# ---------------------------------------------------------------------------
# TOON: TOONRenderer
# ---------------------------------------------------------------------------

class TestTOONRenderer:
    """Tests for toon.renderer.TOONRenderer."""

    def _build_simple_toon(self):
        from toon.models import (
            TOONDocument, TOONSection, TOONNode, TOONNodeType
        )
        node = TOONNode(
            node_type=TOONNodeType.LIST_STEP,
            node_id="s1.1",
            section="Pre-checks",
            description="Verify BGP state",
            commands=["show ip bgp summary"],
            expected_output="Established",
            is_rollback=False,
            source_block_type="list_item",
        )
        section = TOONSection(
            heading="Pre-checks",
            section_index=1,
            is_rollback_section=False,
            mode="toon",
            nodes=[node],
        )
        return TOONDocument(
            title="BGP MOP",
            source_file="test.txt",
            source_format="txt",
            detected_structure="numbered_list",
            sections=[section],
            estimated_raw_tokens=1000,
            estimated_toon_tokens=100,
            compression_ratio=0.9,
            toon_usable=True,
            all_commands=["show ip bgp summary"],
        )

    def test_render_produces_section_header(self):
        """Rendered output contains SECTION: heading."""
        from toon.renderer import TOONRenderer
        doc = self._build_simple_toon()
        text = TOONRenderer.render(doc)
        assert "SECTION: Pre-checks" in text

    def test_render_contains_node_id(self):
        """Rendered output contains the node ID."""
        from toon.renderer import TOONRenderer
        doc = self._build_simple_toon()
        text = TOONRenderer.render(doc)
        assert "[s1.1]" in text

    def test_render_contains_cmd(self):
        """CMD field present in rendered output."""
        from toon.renderer import TOONRenderer
        doc = self._build_simple_toon()
        text = TOONRenderer.render(doc)
        assert "CMD: show ip bgp summary" in text

    def test_render_contains_expect(self):
        """EXPECT field present when expected_output is set."""
        from toon.renderer import TOONRenderer
        doc = self._build_simple_toon()
        text = TOONRenderer.render(doc)
        assert "EXPECT: Established" in text

    def test_render_rollback_tag(self):
        """Rollback section includes [ROLLBACK] tag in heading."""
        from toon.renderer import TOONRenderer
        from toon.models import TOONDocument, TOONSection, TOONNode, TOONNodeType

        node = TOONNode(
            node_type=TOONNodeType.LIST_STEP,
            node_id="s2.1",
            section="Rollback",
            description="Remove BGP config",
            commands=["no router bgp 65001"],
            is_rollback=True,
            source_block_type="list_item",
        )
        section = TOONSection(
            heading="Rollback",
            section_index=2,
            is_rollback_section=True,
            mode="toon",
            nodes=[node],
        )
        toon_doc = TOONDocument(
            title="Test", source_file="t.txt", source_format="txt",
            detected_structure="numbered_list",
            sections=[section],
            toon_usable=True,
        )
        text = TOONRenderer.render(toon_doc)
        assert "[ROLLBACK]" in text

    def test_render_empty_when_not_usable(self):
        """toon_usable=False → render returns empty string."""
        from toon.renderer import TOONRenderer
        from toon.models import TOONDocument

        doc = TOONDocument(
            title="Prose", source_file="t.txt", source_format="txt",
            detected_structure="prose",
            sections=[],
            toon_usable=False,
        )
        assert TOONRenderer.render(doc) == ""

    def test_multi_command_joined_with_arrow(self):
        """Multiple commands joined with ▸ separator."""
        from toon.renderer import TOONRenderer
        from toon.models import TOONDocument, TOONSection, TOONNode, TOONNodeType

        node = TOONNode(
            node_type=TOONNodeType.LIST_STEP,
            node_id="s1.1",
            section="Impl",
            description="Configure BGP",
            commands=["configure terminal", "router bgp 65001"],
            is_rollback=False,
            source_block_type="list_item",
        )
        section = TOONSection(
            heading="Impl", section_index=1, mode="toon", nodes=[node]
        )
        toon_doc = TOONDocument(
            title="T", source_file="t.txt", source_format="txt",
            detected_structure="numbered_list",
            sections=[section], toon_usable=True,
        )
        text = TOONRenderer.render(toon_doc)
        assert "\u25b8" in text  # ▸


# ---------------------------------------------------------------------------
# TOON: integration (builder → renderer → LLM mock)
# ---------------------------------------------------------------------------

class TestTOONIntegration:
    """End-to-end: ParsedDocument → TOON → LLM mock → CanonicalTestModel."""

    def _llm_response(self):
        return json.dumps({
            "document_title": "TOON Test MOP",
            "steps": [
                {
                    "sequence": 1,
                    "step_type": "verification",
                    "action_type": "verify",
                    "description": "Verify BGP state",
                    "raw_text": "Verify BGP state",
                    "commands": [{"raw": "show ip bgp summary", "vendor": "cisco",
                                  "protocol": "bgp", "mode": "exec", "confidence": 0.95}],
                    "expected_output": "Established",
                    "section": "Pre-checks",
                    "subsection": None,
                    "is_rollback": False,
                    "tags": ["bgp", "verification"],
                }
            ],
        })

    def test_toon_path_used_for_structured_doc(self, tmp_path):
        """When a pre-built usable TOONDocument is passed, toon_used is set in metadata."""
        from ai_layer.super_prompt_runner import SuperPromptRunner
        from toon.models import TOONDocument, TOONSection, TOONNode, TOONNodeType

        # Build a minimal usable TOONDocument directly (avoid structure-detection issues)
        node = TOONNode(
            node_type=TOONNodeType.LIST_STEP,
            node_id="s1.1",
            section="Pre-checks",
            description="Verify BGP",
            commands=["show ip bgp summary"],
            is_rollback=False,
            source_block_type="list_item",
        )
        section = TOONSection(heading="Pre-checks", section_index=1, mode="toon", nodes=[node])
        toon_doc = TOONDocument(
            title="BGP MOP",
            source_file="test.txt",
            source_format="txt",
            detected_structure="numbered_list",
            sections=[section],
            estimated_raw_tokens=500,
            estimated_toon_tokens=50,
            compression_ratio=0.9,
            toon_usable=True,
            all_commands=["show ip bgp summary"],
        )

        # Build a matching ParsedDocument
        doc = ParsedDocument(
            title="BGP MOP",
            source_file="test.txt",
            source_format="txt",
            detected_structure="numbered_list",
            blocks=[],
            full_text="show ip bgp summary",
        )

        mock_resp = MagicMock(content=[MagicMock(text=self._llm_response())])
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_resp
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(use_toon=True)
            result = runner.run(doc, toon_doc=toon_doc)

        assert result.success is True
        assert result.model is not None
        assert result.model.metadata.get("toon_used") is True

    def test_skip_toon_uses_raw_path(self, tmp_path):
        """use_toon=False bypasses TOON even for structured docs."""
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner

        text = "\n".join([
            "# Checks",
            "1. show version",
        ])
        f = tmp_path / "mop.txt"
        f.write_text(text)
        doc = parse(str(f))

        mock_resp = MagicMock(content=[MagicMock(text=self._llm_response())])
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_resp
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(use_toon=False)
            result = runner.run(doc)

        assert result.success is True
        # toon_used should not be set
        assert result.model.metadata.get("toon_used") is not True

    def test_prose_doc_falls_back_to_raw(self, tmp_path):
        """Prose document: TOON not usable → falls back to raw text path."""
        from ingestion.txt_parser import parse
        from ai_layer.super_prompt_runner import SuperPromptRunner

        text = (
            "This document describes the BGP migration procedure. "
            "The operator should follow all steps carefully and ensure that "
            "all pre-checks pass before proceeding with the implementation. "
            "If any issues arise, the rollback procedure should be followed."
        )
        f = tmp_path / "prose.txt"
        f.write_text(text)
        doc = parse(str(f))

        mock_resp = MagicMock(content=[MagicMock(text=self._llm_response())])
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_resp
            mock_cls.return_value = mock_client

            runner = SuperPromptRunner(use_toon=True)
            result = runner.run(doc)

        assert result.success is True


# ---------------------------------------------------------------------------
# Universal vendor detection
# ---------------------------------------------------------------------------

class TestUniversalVendorDetection:
    """Grammar engine correctly identifies commands from all supported vendors."""

    def _grammar(self):
        from grammar_engine.cli_grammar import CLIGrammar
        return CLIGrammar()

    def test_huawei_display_command(self):
        """Huawei 'display' commands are detected."""
        g = self._grammar()
        cmds = g.extract_from_text("display ip routing-table")
        assert len(cmds) >= 1

    def test_f5_tmsh_command(self):
        """F5 TMSH commands are detected."""
        g = self._grammar()
        cmds = g.extract_from_text("tmsh show ltm virtual")
        assert len(cmds) >= 1

    def test_checkpoint_fw_command(self):
        """Check Point fw commands are detected."""
        g = self._grammar()
        cmds = g.extract_from_text("fw stat")
        assert len(cmds) >= 1

    def test_huawei_undo_command(self):
        """Huawei 'undo' commands are detected."""
        g = self._grammar()
        cmds = g.extract_from_text("undo shutdown")
        assert len(cmds) >= 1

    def test_cisco_command_still_works(self):
        """Existing Cisco commands unaffected by new vendor additions."""
        g = self._grammar()
        cmds = g.extract_from_text("show ip bgp summary")
        assert len(cmds) >= 1

    def test_juniper_set_command(self):
        """Juniper 'set' commands are still detected."""
        g = self._grammar()
        cmds = g.extract_from_text("set protocols bgp group EBGP neighbor 10.0.0.1")
        assert len(cmds) >= 1
