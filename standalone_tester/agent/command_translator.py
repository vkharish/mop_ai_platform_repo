"""
Command Translator

Translates vendor-agnostic test intents into vendor-specific CLI commands
using the Claude API (Haiku model for cost efficiency) with local caching.

Cache: standalone_tester/cache/command_cache.json
Key:   vendor|os|version|intent_id
Value: {command, success_pattern, error_pattern}

After first run per intent+vendor, subsequent calls are free (cache hit).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "cache" / "command_cache.json"
CACHE_PATH.parent.mkdir(exist_ok=True)


class TranslatedCommand:
    def __init__(self, command: str, success_pattern: str, error_pattern: str, from_cache: bool = False):
        self.command = command
        self.success_pattern = success_pattern
        self.error_pattern = error_pattern
        self.from_cache = from_cache

    def __repr__(self) -> str:
        src = "cache" if self.from_cache else "llm"
        return f"Command({self.command[:40]!r}, src={src})"


class CommandTranslator:
    """
    Translates test intents to vendor-specific commands.
    Uses Haiku (cheapest model) + file cache to minimise API cost.
    """

    def __init__(self, api_key: Optional[str] = None, mock: bool = False):
        self._mock = mock
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._cache = self._load_cache()

    def translate(
        self,
        intent: str,
        intent_id: str,
        vendor: str,
        os_type: str,
        version: str,
    ) -> TranslatedCommand:
        """Translate an intent to a vendor-specific command."""
        cache_key = f"{vendor}|{os_type}|{intent_id}"

        # Cache hit
        if cache_key in self._cache:
            c = self._cache[cache_key]
            logger.debug("Cache hit: %s", cache_key)
            return TranslatedCommand(
                command=c["command"],
                success_pattern=c["success_pattern"],
                error_pattern=c["error_pattern"],
                from_cache=True,
            )

        if self._mock:
            result = self._mock_translate(intent, vendor, os_type)
        else:
            result = self._llm_translate(intent, intent_id, vendor, os_type, version)

        self._cache[cache_key] = {
            "command": result.command,
            "success_pattern": result.success_pattern,
            "error_pattern": result.error_pattern,
        }
        self._save_cache()
        return result

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _llm_translate(
        self, intent: str, intent_id: str, vendor: str, os_type: str, version: str
    ) -> TranslatedCommand:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._api_key)
            prompt = (
                f"You are a network engineer expert in {vendor} {os_type} version {version}.\n\n"
                f"Test intent: {intent}\n\n"
                f"Return ONLY a JSON object with exactly these fields:\n"
                f'{{"command": "<exact CLI command>", '
                f'"success_pattern": "<regex or keyword that indicates success>", '
                f'"error_pattern": "<regex or keyword that indicates failure>"}}\n\n'
                f"Rules:\n"
                f"- command must be executable verbatim on {vendor} {os_type}\n"
                f"- success_pattern should match healthy output\n"
                f"- error_pattern should match failure/error output\n"
                f"- Return ONLY the JSON, no explanation"
            )
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Extract JSON from response
            import re
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return TranslatedCommand(
                    command=data.get("command", "show version"),
                    success_pattern=data.get("success_pattern", ""),
                    error_pattern=data.get("error_pattern", "Error|error"),
                )
        except Exception as e:
            logger.warning("LLM translation failed for '%s': %s — using fallback", intent_id, e)

        return self._mock_translate(intent, vendor, os_type)

    def _mock_translate(self, intent: str, vendor: str, os_type: str) -> TranslatedCommand:
        """Fallback heuristic translation when LLM is unavailable."""
        intent_l = intent.lower()
        # BGP
        if "bgp" in intent_l and "neighbor" in intent_l:
            cmds = {
                "cisco": "show bgp ipv4 unicast summary",
                "juniper": "show bgp summary",
                "nokia": "show router bgp summary",
                "arista": "show bgp summary",
                "huawei": "display bgp peer",
                "ericsson": "show bgp neighbors",
            }
            return TranslatedCommand(
                command=cmds.get(vendor, "show bgp summary"),
                success_pattern=r"Established|Establ",
                error_pattern=r"Idle|Active|Connect",
            )
        # IS-IS
        if "isis" in intent_l or "is-is" in intent_l:
            cmds = {
                "cisco": "show isis neighbors",
                "juniper": "show isis adjacency",
                "nokia": "show router isis adjacency",
                "arista": "show isis neighbors",
                "huawei": "display isis peer",
            }
            return TranslatedCommand(
                command=cmds.get(vendor, "show isis neighbors"),
                success_pattern=r"Up|UP",
                error_pattern=r"Down|Init|None",
            )
        # MPLS/LDP
        if "ldp" in intent_l or "mpls" in intent_l:
            cmds = {
                "cisco": "show mpls ldp neighbor",
                "juniper": "show ldp session",
                "nokia": "show router ldp session",
                "arista": "show mpls ldp neighbors",
                "huawei": "display mpls ldp session",
            }
            return TranslatedCommand(
                command=cmds.get(vendor, "show mpls ldp neighbor"),
                success_pattern=r"Operational|Oper",
                error_pattern=r"Non-Existent|Down",
            )
        # CPU
        if "cpu" in intent_l:
            cmds = {
                "cisco": "show processes cpu",
                "juniper": "show system processes extensive | head 5",
                "nokia": "show system cpu",
                "arista": "show processes top once | head 5",
                "huawei": "display cpu-usage",
            }
            return TranslatedCommand(
                command=cmds.get(vendor, "show processes cpu"),
                success_pattern=r"\d+",
                error_pattern=r"Error",
            )
        # Interfaces
        if "interface" in intent_l:
            cmds = {
                "cisco": "show interfaces brief",
                "juniper": "show interfaces terse",
                "nokia": "show port",
                "arista": "show interfaces status",
                "huawei": "display interface brief",
            }
            return TranslatedCommand(
                command=cmds.get(vendor, "show interfaces brief"),
                success_pattern=r"up|Up|UP",
                error_pattern=r"down|Down|DOWN|err",
            )
        # Default
        return TranslatedCommand(
            command="show version",
            success_pattern=r"Version|version",
            error_pattern=r"Error|error",
        )

    def _load_cache(self) -> dict:
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_cache(self) -> None:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2)
