"""
TOON Compressor and Prose Analyzer

TextCompressor: removes filler phrases, collapses whitespace, truncates prose.
ProseAnalyzer:  scores a paragraph for actionable significance (skip pure fluff),
                and extracts expected-output patterns.

These run on CPU with zero external dependencies.
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Filler phrase removal
# ---------------------------------------------------------------------------

_FILLER: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bplease note that\b",         re.I), ""),
    (re.compile(r"\bplease note:\s*",            re.I), ""),
    (re.compile(r"\bensure that\b",              re.I), ""),
    (re.compile(r"\bmake sure (to|that)\b",      re.I), ""),
    (re.compile(r"\bthe operator should\b",      re.I), ""),
    (re.compile(r"\bit is required to\b",        re.I), ""),
    (re.compile(r"\bit is recommended (to|that)\b", re.I), ""),
    (re.compile(r"\bplease\b",                   re.I), ""),
    (re.compile(r"\bin order to\b",              re.I), "to"),
    (re.compile(r"\bprior to\b",                 re.I), "before"),
    (re.compile(r"\bsubsequent to\b",            re.I), "after"),
    (re.compile(r"\bso as to\b",                 re.I), "to"),
    (re.compile(r"\bit should be noted that\b",  re.I), ""),
    (re.compile(r"\bfor the purpose of\b",       re.I), "for"),
    (re.compile(r"\bat this point in time\b",    re.I), "now"),
    (re.compile(r"\bbe sure to\b",               re.I), ""),
    (re.compile(r"\bkindly\b",                   re.I), ""),
    (re.compile(r"\bonce completed?,?\s*",       re.I), ""),
    (re.compile(r"\bwhen done,?\s*",             re.I), ""),
    (re.compile(r"\bas (per|mentioned above),?\s*", re.I), ""),
    (re.compile(r"\bif applicable,?\s*",         re.I), ""),
    (re.compile(r"\bfor example,?\s*",           re.I), "e.g. "),
    (re.compile(r"\bnote:\s*",                   re.I), ""),
]


class TextCompressor:
    """Strip filler phrases from MOP prose and truncate to a target length."""

    @staticmethod
    def compress(text: str) -> str:
        """Remove filler phrases and collapse whitespace."""
        for pattern, replacement in _FILLER:
            text = pattern.sub(replacement, text)
        text = re.sub(r"\s+", " ", text).strip()
        # Fix double spaces around punctuation that filler removal may create
        text = re.sub(r"\s+([,.:;])", r"\1", text)
        return text

    @staticmethod
    def truncate(text: str, max_chars: int = 120) -> str:
        """Truncate at a word boundary close to max_chars."""
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.75:
            return truncated[:last_space] + "…"
        return truncated + "…"

    @classmethod
    def compress_and_truncate(cls, text: str, max_chars: int = 120) -> str:
        return cls.truncate(cls.compress(text), max_chars)


# ---------------------------------------------------------------------------
# Prose significance scorer
# ---------------------------------------------------------------------------

_ACTION_VERBS = re.compile(
    r"\b(configure|verify|check|enable|disable|install|connect|validate|"
    r"test|ping|traceroute|remove|add|apply|deploy|activate|deactivate|"
    r"restart|reboot|reload|shutdown|bring up|bring down|set|get|"
    r"confirm|ensure|establish|clear|reset|delete|create|modify|update|"
    r"copy|paste|save|commit|rollback|revert|undo|display|show)\b",
    re.IGNORECASE,
)
_IP_ADDR  = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?\b")
_IPV6     = re.compile(r"\b[0-9a-fA-F]{1,4}(:[0-9a-fA-F]{0,4}){2,7}\b")
_IFACE    = re.compile(
    r"\b(GigabitEthernet|TenGigabitEthernet|HundredGigE|FastEthernet|"
    r"Ethernet|Bundle-Ether|ae\d+|xe-[\d/]+|et-[\d/]+|ge-[\d/]+|"
    r"te[\d/]+|gi[\d/]+|fa[\d/]+|lo\d+|tun\d+|Loopback|Tunnel|"
    r"Port-channel|management|mgmt|vlan|BDI|BVI|IRB)\d*[\d/.:-]*\b",
    re.IGNORECASE,
)
_AS_NUM   = re.compile(r"\b(AS|ASN)\s*\d+\b", re.IGNORECASE)
_VLAN     = re.compile(r"\bvlan\s*\d+\b",      re.IGNORECASE)
_PREFIX   = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}\b")
_HOSTNAME = re.compile(r"\bPE\d+|CE\d+|P\d+-\w+|R\d+\b")  # typical MOP device names


class ProseAnalyzer:
    """Score a paragraph for actionable significance."""

    @staticmethod
    def score(text: str) -> int:
        """
        Score a text block for actionable content.
        0 = pure boilerplate, higher = more likely to contain a real step.
        """
        score = 0
        score += min(3, len(_ACTION_VERBS.findall(text)))  # cap at 3 per block
        score += min(2, len(_IP_ADDR.findall(text)))
        score += min(2, len(_IPV6.findall(text)))
        score += min(2, len(_IFACE.findall(text)))
        score += min(1, len(_AS_NUM.findall(text)))
        score += min(1, len(_VLAN.findall(text)))
        score += min(1, len(_PREFIX.findall(text)))
        score += min(1, len(_HOSTNAME.findall(text)))
        return score

    @staticmethod
    def extract_expected(text: str) -> Optional[str]:
        """
        Extract expected-output descriptions from step text.

        Looks for patterns like:
          "Expected: all neighbors Established"
          "Verify that the state shows FULL"
          "should see state is Up"
        """
        patterns = [
            re.compile(r"[Ee]xpected[:\s]+(.{10,120})",           re.IGNORECASE),
            re.compile(r"[Vv]erify\s+that\s+(.{10,120})",         re.IGNORECASE),
            re.compile(r"[Ss]hould\s+show\s+(.{10,120})",         re.IGNORECASE),
            re.compile(r"[Ss]hould\s+see\s+(.{10,120})",          re.IGNORECASE),
            re.compile(r"[Oo]utput\s+should\s+(be\s+)?(.{10,120})", re.IGNORECASE),
            re.compile(r"[Cc]onfirm\s+(?:that\s+)?(.{10,120})",   re.IGNORECASE),
            re.compile(r"[Ss]tate\s+(?:is|should be)\s+(.{5,80})", re.IGNORECASE),
            re.compile(r"[Rr]esult[:\s]+(.{10,120})",              re.IGNORECASE),
        ]
        for p in patterns:
            m = p.search(text)
            if m:
                # Take the last group (handles patterns with optional groups)
                val = m.group(m.lastindex or 1).strip().rstrip(".")
                if len(val) >= 5:
                    return val[:120]
        return None
