"""Command-line entry point: ``python -m src.assistant --db PATH "<request>"``."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_PROPOSALS = Path("data/assistant/proposals.jsonl")
DEFAULT_AUDIT = Path("data/assistant/audit.jsonl")


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the assistant CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m src.assistant",
        description=(
            "Ask the question bank assistant. It reads the database (read-only) and can only "
            "propose changes, each after your y/N approval."
        ),
    )
    parser.add_argument("--db", type=Path, required=True, help="path to the SQLite question bank")
    parser.add_argument(
        "--proposals", type=Path, default=DEFAULT_PROPOSALS,
        help=f"JSONL file approved proposals are appended to (default: {DEFAULT_PROPOSALS})",
    )
    parser.add_argument(
        "--audit", type=Path, default=DEFAULT_AUDIT,
        help=f"JSONL audit log (default: {DEFAULT_AUDIT})",
    )
    parser.add_argument(
        "--yes-to-nothing", action="store_true",
        help="decline every proposal without asking (dry run)",
    )
    parser.add_argument("request", help="the request, in plain language")
    return parser


def interactive_confirm(name: str, tool_input: dict) -> bool:
    """Show a proposal and ask the operator for y/N."""
    print(f"\nProposed action: {name}")
    print(json.dumps(tool_input, ensure_ascii=False, indent=2))
    try:
        answer = input("Approve this proposal? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def decline_all(name: str, tool_input: dict) -> bool:
    """Confirm callback for ``--yes-to-nothing``."""
    print(f"\nDeclined (dry run): {name} {json.dumps(tool_input, ensure_ascii=False)}")
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one request; returns the process exit code."""
    args = build_parser().parse_args(argv)

    if not args.db.is_file():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 2

    try:
        import anthropic
    except ImportError:
        print(
            "The 'anthropic' package is not installed. Install the optional tooling with:\n"
            "  pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 2

    from src.assistant.agent import run_agent
    from src.assistant.tools import ToolContext, open_read_only

    conn = open_read_only(args.db)
    try:
        ctx = ToolContext(conn=conn, proposals_path=args.proposals, now=_now)
        confirm = decline_all if args.yes_to_nothing else interactive_confirm
        try:
            client = anthropic.Anthropic()
            result = run_agent(
                args.request, client=client, ctx=ctx, confirm=confirm, audit_path=args.audit
            )
        except anthropic.AuthenticationError:
            print("Anthropic authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr)
            return 2
        except anthropic.RateLimitError:
            print("Anthropic rate limit reached; try again shortly.", file=sys.stderr)
            return 2
        except anthropic.APIStatusError as exc:
            print(f"Anthropic API error {exc.status_code}: {exc.message}", file=sys.stderr)
            return 2
        except anthropic.APIConnectionError:
            print("Could not reach the Anthropic API; check the network connection.", file=sys.stderr)
            return 2
    finally:
        conn.close()

    print()
    print(result.answer)
    print()
    print(f"Skill: {result.skill or '-'}  |  status: {result.status}  |  "
          f"proposals written: {result.proposals}  |  steps: {result.steps}")
    if result.proposals:
        print(f"Proposals file: {args.proposals}")
    return 0 if result.status == "done" else 1
