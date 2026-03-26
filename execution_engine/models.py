"""
Execution Engine Models — runtime state types for Phase 2.

These are distinct from models/canonical.py:
  canonical.py  = the MOP document model (Phase 1 contract)
  models.py     = execution runtime state (Phase 2 internal)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from models.canonical import CanonicalTestModel, ExecutionStatus, ApprovalStatus


class StepResult(BaseModel):
    step_id:           str
    status:            ExecutionStatus = ExecutionStatus.PENDING
    device:            Optional[str]   = None
    started_at:        Optional[str]   = None
    completed_at:      Optional[str]   = None
    attempts:          int             = 0
    actual_output:     Optional[str]   = None
    validation_passed: Optional[bool]  = None
    validation_errors: List[str]       = Field(default_factory=list)
    error_message:     Optional[str]   = None
    skipped_reason:    Optional[str]   = None
    duration_ms:       int             = 0


class TransitionRecord(BaseModel):
    timestamp:      str
    entity:         str            # "execution" | step_id
    from_status:    Optional[str]  = None
    to_status:      str
    agent:          str            # "planner"|"execution"|"validation"|"recovery"|"api"|"system"
    message:        str
    correlation_id: str


class ExecutionState(BaseModel):
    execution_id:       str
    canonical_model:    CanonicalTestModel
    status:             ExecutionStatus    = ExecutionStatus.PENDING
    created_at:         str
    started_at:         Optional[str]      = None
    completed_at:       Optional[str]      = None
    steps:              Dict[str, StepResult] = Field(default_factory=dict)
    history:            List[TransitionRecord] = Field(default_factory=list)
    kill_requested:     bool               = False
    paused:             bool               = False
    pause_reason:       Optional[str]      = None
    approval_status:    ApprovalStatus     = ApprovalStatus.NOT_REQUIRED
    approver_id:        Optional[str]      = None
    approved_at:        Optional[str]      = None
    correlation_id:     str                = ""
    notifications_sent: Dict[str, bool]    = Field(default_factory=dict)
    itsm_updated:       bool               = False
    dry_run:            bool               = False


@dataclass
class AgentMessage:
    """Inter-agent communication message (via shared queue, not direct calls)."""
    from_agent:     str
    to_agent:       str
    event:          str
    execution_id:   str
    step_id:        Optional[str]  = None
    payload:        Dict[str, Any] = field(default_factory=dict)
    message_id:     str            = field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp:      str            = field(default_factory=lambda: __import__('datetime').datetime.utcnow().isoformat())
    correlation_id: str            = ""


class DecisionRecord(BaseModel):
    """Written to decision.log by the Decision Engine."""
    timestamp:     str
    execution_id:  str
    step_id:       Optional[str]  = None
    device:        Optional[str]  = None
    decision:      str            # RETRY | ROLLBACK | CONTINUE | ESCALATE | SKIP
    reason:        str
    evidence:      str            = ""   # snippet of actual_output
    confidence:    float          = 1.0
    auto_decided:  bool           = True
    correlation_id: str           = ""


class ExecutionPlan(BaseModel):
    """Output of PlannerAgent — used internally by ExecutionAgent."""
    execution_id:       str
    waves:              List[List[str]]  = Field(default_factory=list)  # list of step_id lists
    transaction_groups: Dict[str, List[str]] = Field(default_factory=dict)  # group_name → [step_ids]
    critical_path:      List[str]        = Field(default_factory=list)
    requires_approval:  bool             = False
    approval_reasons:   List[str]        = Field(default_factory=list)
    device_list:        List[str]        = Field(default_factory=list)
    estimated_duration_s: float          = 0.0
