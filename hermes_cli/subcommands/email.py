"""``hermes email`` subcommand parser.

Supports ``dispatch`` for testing email workflows from the CLI.
Extensible — more subcommands (``list``, ``status``, etc.) slot in later.
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_email_parser(subparsers, *, cmd_email: Callable) -> None:
    """Attach the ``email`` subcommand to ``subparsers``."""
    # =========================================================================
    # email command
    # =========================================================================
    email_parser = subparsers.add_parser(
        "email",
        help="Email workflow testing commands",
        description=(
            "Manage and test email-triggered workflows (audiobook, "
            "citation-review, citation-add) from the CLI without a running "
            "gateway or IMAP connection."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes email list                           Show configured email workflows
    hermes email dispatch audiobook paper.pdf   Run the audiobook workflow on a PDF
    hermes email dispatch citation-review draft.docx --sender me@example.com
""",
    )
    email_subparsers = email_parser.add_subparsers(dest="email_command")

    # --- email list ---
    email_subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="Show configured email workflows",
    )

    # --- email dispatch ---
    dispatch_parser = email_subparsers.add_parser(
        "dispatch",
        help="Run an email workflow against a local file",
        description=(
            "Start a new agent session identical to what happens when a "
            "real workflow-triggering email arrives. Same prompt template, "
            "same skill loading, same agent context — but sourced from a "
            "local file instead of an IMAP attachment, and output goes to "
            "stdout instead of an SMTP reply."
        ),
    )
    dispatch_parser.add_argument(
        "workflow",
        help="Workflow name from config (e.g. audiobook, citation-review)",
    )
    dispatch_parser.add_argument(
        "file",
        help="Path to the test file (PDF, .docx, etc.)",
    )
    dispatch_parser.add_argument(
        "--sender",
        default="test@local.dev",
        help="Synthetic sender email (default: test@local.dev)",
    )
    dispatch_parser.add_argument(
        "--model",
        default=None,
        help="Model override (default: config's default model)",
    )

    email_parser.set_defaults(func=cmd_email)
