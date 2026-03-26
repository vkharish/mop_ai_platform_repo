"""
Execution Report Builder — generates a structured report from ExecutionState.

Output:
  - JSON (always generated)
  - HTML (via Jinja2 template if available, otherwise inline template)

Usage:
    from reporting.execution_report import ExecutionReportBuilder
    report = ExecutionReportBuilder.build(execution_id)
    html = ExecutionReportBuilder.render_html(report)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from execution_engine.state_manager import state_manager
from models.canonical import ExecutionStatus

logger = logging.getLogger(__name__)


class ExecutionReportBuilder:

    @classmethod
    def build(cls, execution_id: str) -> Dict[str, Any]:
        """Build a complete JSON report dict from ExecutionState."""
        state = state_manager.get(execution_id)
        model = state.canonical_model

        # Per-step summary
        per_step = []
        for step in model.steps:
            result = state.steps.get(step.step_id)
            cmds = [c.raw for c in step.commands]
            snippet = ""
            duration = 0
            retries = 0
            val_errors: List[str] = []
            status = "pending"

            if result:
                snippet = (result.actual_output or "")[:300]
                duration = result.duration_ms / 1000.0 if result.duration_ms else 0
                retries = max(0, result.attempts - 1)
                val_errors = result.validation_errors or []
                status = result.status.value

            per_step.append({
                "step_id": step.step_id,
                "sequence": step.sequence,
                "section": step.section,
                "description": step.description,
                "device": (step.devices[0].hostname if step.devices else ""),
                "status": status,
                "commands_executed": cmds,
                "actual_output_snippet": snippet,
                "duration_s": round(duration, 2),
                "retries": retries,
                "validation_errors": val_errors,
            })

        # Per-device summary
        per_device: Dict[str, Dict] = {}
        for entry in per_step:
            device = entry["device"] or "(none)"
            if device not in per_device:
                per_device[device] = {"steps_run": 0, "steps_passed": 0,
                                       "steps_failed": 0, "durations": []}
            per_device[device]["steps_run"] += 1
            if entry["status"] == "passed":
                per_device[device]["steps_passed"] += 1
            elif entry["status"] == "failed":
                per_device[device]["steps_failed"] += 1
            per_device[device]["durations"].append(entry["duration_s"])

        per_device_summary = {}
        for dev, stats in per_device.items():
            durs = stats["durations"]
            per_device_summary[dev] = {
                "steps_run": stats["steps_run"],
                "steps_passed": stats["steps_passed"],
                "steps_failed": stats["steps_failed"],
                "avg_duration_s": round(sum(durs) / len(durs), 2) if durs else 0,
            }

        # Compute total duration
        total_duration_s = 0.0
        if state.started_at and state.completed_at:
            try:
                start = datetime.fromisoformat(state.started_at)
                end = datetime.fromisoformat(state.completed_at)
                total_duration_s = round((end - start).total_seconds(), 2)
            except Exception:
                pass

        # Decision log summary
        decision_log_summary = cls._read_decision_log(execution_id)

        # Timeline from history
        timeline = [
            {
                "timestamp": h.timestamp,
                "event_type": f"{h.entity} → {h.to_status}",
                "step_id": h.entity if h.entity != "execution" else None,
                "agent": h.agent,
                "message": h.message,
            }
            for h in state.history
        ]

        steps_list = list(state.steps.values())
        report = {
            "execution_id": execution_id,
            "document_title": model.document_title,
            "change_ticket": model.change_ticket.model_dump() if model.change_ticket else None,
            "started_at": state.started_at,
            "completed_at": state.completed_at,
            "total_duration_s": total_duration_s,
            "overall_status": state.status.value,
            "dry_run": state.dry_run,
            "steps_total": len(per_step),
            "steps_passed": sum(1 for s in steps_list if s.status == ExecutionStatus.PASSED),
            "steps_failed": sum(1 for s in steps_list if s.status == ExecutionStatus.FAILED),
            "steps_skipped": sum(1 for s in steps_list if s.status == ExecutionStatus.SKIPPED),
            "steps_rolled_back": sum(1 for s in steps_list if s.status == ExecutionStatus.ROLLED_BACK),
            "per_device_summary": per_device_summary,
            "per_step": per_step,
            "timeline": timeline,
            "notifications_sent": state.notifications_sent,
            "itsm_updated": state.itsm_updated,
            "decision_log_summary": decision_log_summary,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return report

    @classmethod
    def render_html(cls, report: Dict[str, Any]) -> str:
        """Render the report as HTML. Uses Jinja2 if available, else inline."""
        try:
            import jinja2
            template_path = Path(__file__).parent / "templates" / "report.html"
            if template_path.exists():
                env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_path.parent)))
                tmpl = env.get_template("report.html")
                return tmpl.render(**report)
        except ImportError:
            pass
        return cls._render_html_inline(report)

    @classmethod
    def save(cls, execution_id: str, output_dir: str = "output") -> Dict[str, str]:
        """Generate report, save JSON and HTML files, return paths."""
        report = cls.build(execution_id)
        out = Path(output_dir) / "reports"
        out.mkdir(parents=True, exist_ok=True)

        json_path = out / f"{execution_id}_report.json"
        html_path = out / f"{execution_id}_report.html"

        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        html_path.write_text(cls.render_html(report), encoding="utf-8")

        logger.info("Execution report saved: %s, %s", json_path, html_path)
        return {"json": str(json_path), "html": str(html_path)}

    @classmethod
    def _read_decision_log(cls, execution_id: str) -> List[Dict]:
        """Read decision.log entries for this execution_id."""
        log_path = Path("output") / "decision.log"
        if not log_path.exists():
            return []
        records = []
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("execution_id") == execution_id:
                            records.append(record)
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        return records

    @classmethod
    def _render_html_inline(cls, report: Dict[str, Any]) -> str:
        status_color = {"passed": "#2d7d2d", "failed": "#b00020", "rolled_back": "#ff8800"}.get(
            report.get("overall_status", ""), "#555"
        )
        step_rows = ""
        for s in report.get("per_step", []):
            sc = {"passed": "#e8f5e9", "failed": "#ffebee", "skipped": "#fff3e0",
                  "rolled_back": "#fff8e1"}.get(s["status"], "#fff")
            step_rows += (
                f"<tr style='background:{sc}'>"
                f"<td>{s['sequence']}</td><td>{s['step_id']}</td>"
                f"<td>{s['section']}</td><td>{s['description'][:60]}</td>"
                f"<td>{s['device']}</td><td><b>{s['status']}</b></td>"
                f"<td>{s['duration_s']}s</td><td>{s['retries']}</td>"
                f"<td style='font-size:11px'>{'; '.join(s.get('validation_errors','')[:2])}</td>"
                f"</tr>"
            )
        device_rows = ""
        for dev, stats in report.get("per_device_summary", {}).items():
            device_rows += (
                f"<tr><td>{dev}</td><td>{stats['steps_run']}</td>"
                f"<td>{stats['steps_passed']}</td><td>{stats['steps_failed']}</td>"
                f"<td>{stats['avg_duration_s']}s</td></tr>"
            )
        return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>MOP Execution Report — {report.get('execution_id')}</title>
<style>
  body {{font-family:Arial,sans-serif; margin:20px; color:#333}}
  h1 {{color:{status_color}}} table {{border-collapse:collapse; width:100%; margin-bottom:20px}}
  th,td {{border:1px solid #ddd; padding:6px 10px; text-align:left; font-size:13px}}
  th {{background:#f5f5f5}} .badge {{padding:3px 8px; border-radius:4px; color:#fff; background:{status_color}}}
</style></head><body>
<h1>MOP Execution Report</h1>
<p><span class='badge'>{report.get('overall_status','').upper()}</span>
{"&nbsp;&nbsp;[DRY RUN]" if report.get('dry_run') else ""}</p>
<table>
  <tr><th>Field</th><th>Value</th></tr>
  <tr><td>Execution ID</td><td>{report.get('execution_id')}</td></tr>
  <tr><td>Document</td><td>{report.get('document_title')}</td></tr>
  <tr><td>Started</td><td>{report.get('started_at','—')}</td></tr>
  <tr><td>Completed</td><td>{report.get('completed_at','—')}</td></tr>
  <tr><td>Duration</td><td>{report.get('total_duration_s','—')}s</td></tr>
  <tr><td>Steps</td><td>
    Total: {report.get('steps_total',0)} |
    Passed: {report.get('steps_passed',0)} |
    Failed: {report.get('steps_failed',0)} |
    Skipped: {report.get('steps_skipped',0)} |
    Rolled back: {report.get('steps_rolled_back',0)}
  </td></tr>
</table>
<h2>Per-Device Summary</h2>
<table>
  <tr><th>Device</th><th>Steps Run</th><th>Passed</th><th>Failed</th><th>Avg Duration</th></tr>
  {device_rows or '<tr><td colspan=5>No device data</td></tr>'}
</table>
<h2>Step Details</h2>
<table>
  <tr><th>#</th><th>ID</th><th>Section</th><th>Description</th><th>Device</th>
      <th>Status</th><th>Duration</th><th>Retries</th><th>Validation Errors</th></tr>
  {step_rows or '<tr><td colspan=9>No steps</td></tr>'}
</table>
<p style='color:#999;font-size:11px'>Generated: {report.get('generated_at')}</p>
</body></html>"""
