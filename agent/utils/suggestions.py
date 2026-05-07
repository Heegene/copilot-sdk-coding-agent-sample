"""GitHub Suggested Changes formatter for inline PR review comments."""

from __future__ import annotations

import re


def format_suggestion(
    file_path: str,
    start_line: int,
    end_line: int,
    original_code: str,
    suggested_code: str,
    explanation: str,
) -> dict:
    """Format a code suggestion for GitHub PR review API.

    Returns a dict compatible with GitHub's pull request review comments API:
    {
        "path": "src/app.py",
        "line": 42,
        "side": "RIGHT",
        "body": "explanation\\n\\n```suggestion\\nsuggested_code\\n```"
    }
    """
    body = f"{explanation}\n\n```suggestion\n{suggested_code}\n```"

    comment: dict = {
        "path": file_path,
        "line": end_line,
        "side": "RIGHT",
        "body": body,
    }
    # Multi-line suggestions need a start_line
    if start_line != end_line:
        comment["start_line"] = start_line
        comment["start_side"] = "RIGHT"

    return comment


def format_review_body_with_suggestions(findings: list[dict]) -> str:
    """Format multiple findings into a review body with suggestion blocks.

    Each finding dict has: file_path, line, original, suggested, explanation, severity
    Returns markdown with ``suggestion`` blocks that GitHub renders as Apply buttons.
    """
    if not findings:
        return "No actionable code suggestions found."

    severity_icons = {
        "critical": "🔴",
        "high": "🟡",
        "medium": "🟢",
        "low": "💬",
    }

    parts: list[str] = ["## 🔧 Suggested Changes\n"]

    for i, f in enumerate(findings, 1):
        icon = severity_icons.get(f.get("severity", "").lower(), "💬")
        parts.append(
            f"### {i}. {icon} `{f['file_path']}` (L{f['line']})\n"
            f"{f['explanation']}\n\n"
            f"```suggestion\n{f['suggested']}\n```\n"
        )

    return "\n".join(parts)


def parse_suggestion_response(raw_text: str) -> list[dict]:
    """Parse the structured suggestion response from the LLM.

    Expects blocks in the format:
        ### FILE: <path>
        ### LINE: <number>
        ### SEVERITY: Critical|High|Medium|Low
        ### EXPLANATION: <text>
        ### ORIGINAL:
        ```
        <code>
        ```
        ### SUGGESTED:
        ```
        <code>
        ```

    Returns a list of finding dicts.
    """
    findings: list[dict] = []

    # Split into blocks starting with "### FILE:"
    blocks = re.split(r"(?=^### FILE:)", raw_text, flags=re.MULTILINE)

    for block in blocks:
        block = block.strip()
        if not block.startswith("### FILE:"):
            continue

        file_match = re.search(r"### FILE:\s*(.+)", block)
        line_match = re.search(r"### LINE:\s*(\d+)", block)
        severity_match = re.search(r"### SEVERITY:\s*(\w+)", block)
        explanation_match = re.search(r"### EXPLANATION:\s*(.+)", block)

        # Extract code blocks for ORIGINAL and SUGGESTED
        code_blocks = re.findall(r"```\n?(.*?)```", block, re.DOTALL)

        if not (file_match and line_match and len(code_blocks) >= 2):
            continue

        findings.append({
            "file_path": file_match.group(1).strip().strip("`"),
            "line": int(line_match.group(1)),
            "severity": (severity_match.group(1) if severity_match else "Medium"),
            "explanation": (
                explanation_match.group(1).strip()
                if explanation_match
                else "Suggested improvement"
            ),
            "original": code_blocks[-2].strip(),
            "suggested": code_blocks[-1].strip(),
        })

    return findings
