"""``hermes email dispatch`` — run email workflows from the CLI.

Creates a new agent session identical to what happens when a real
workflow-triggering email arrives, but sourced from a local file.
"""

from __future__ import annotations

import os
import sys


def email_dispatch(args) -> int:
    """Entry point for ``hermes email dispatch`` and ``hermes email list``.

    Returns an exit code suitable for ``sys.exit()``.
    """
    cmd = getattr(args, "email_command", None)

    if cmd in ("list", "ls"):
        return _list_workflows()

    if cmd != "dispatch":
        print("Usage: hermes email {list,dispatch} ...")
        print("Run 'hermes email --help' for details.")
        return 1

    return _do_dispatch(args)


# ── list ─────────────────────────────────────────────────────────────────────


def _list_workflows() -> int:
    """Print configured email workflows."""
    from hermes_cli.config import load_config

    cfg = load_config()
    workflows = _get_workflows(cfg)

    if not workflows:
        print("No email workflows configured.")
        print("Add them under 'platforms.email.extra.workflows' in config.yaml.")
        return 1

    print("Configured email workflows:")
    print()
    for wf_id, wf_cfg in sorted(workflows.items()):
        prefix = wf_cfg.get("subject_prefix", wf_id)
        desc = wf_cfg.get("description", "(no description)")
        skill = wf_cfg.get("skill", wf_id)
        senders = wf_cfg.get("allowed_senders", [])
        sender_count = len(senders) if senders else 0
        print(f"  {wf_id}")
        print(f"    subject: \"{prefix}\"")
        print(f"    skill:   {skill}")
        print(f"    senders: {sender_count} allowed")
        print(f"    task:    {desc}")
        print()
    return 0


# ── dispatch ─────────────────────────────────────────────────────────────────


def _do_dispatch(args) -> int:
    """Core dispatch logic."""
    from hermes_cli.config import load_config

    cfg = load_config()
    workflows = _get_workflows(cfg)

    if not workflows:
        print("No email workflows found in config.yaml.")
        print("Add them under 'platforms.email.extra.workflows'.")
        return 1

    # ── 1. Resolve workflow ──────────────────────────────────────────────
    wf_name = args.workflow.strip().lower()
    wf_config = workflows.get(wf_name)
    if not wf_config:
        print(f"Unknown workflow: '{wf_name}'")
        print()
        _list_workflows_found(workflows)
        return 1

    # ── 2. Validate file ─────────────────────────────────────────────────
    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        print(f"File not found: {file_path}")
        return 1

    # ── 3. Build the workflow prompt ────────────────────────────────────
    sender = args.sender or "test@local.dev"
    prompt = _build_workflow_prompt(wf_config, wf_name, file_path, sender)

    # ── 4. Run the agent session ────────────────────────────────────────
    # Reuse _run_agent from oneshot.py — handles model resolution, provider
    # detection, toolset loading, session DB, clarify callback, and all
    # the interactive-approval bypasses.
    print(f"[dispatch] Starting /{wf_name} workflow with {os.path.basename(file_path)} ...", file=sys.stderr)
    print(f"[dispatch] Sender: {sender}", file=sys.stderr)
    print(file=sys.stderr)

    from hermes_cli.oneshot import _run_agent

    response, result = _run_agent(
        prompt=prompt,
        model=getattr(args, "model", None),
    )

    # Print the agent's final response to stdout (exit code conventions
    # match hermes -z: 0 = success, 1 = failed/partial).
    if response:
        print(response)
        if not response.endswith("\n"):
            print()

    failed = result.get("failed") or result.get("partial")
    if failed and not (response or "").strip():
        return 2

    return 0 if (response or "").strip() else 1


# ── helpers ──────────────────────────────────────────────────────────────────


def _get_workflows(cfg: dict) -> dict:
    """Extract the workflows dict from config, or return empty dict."""
    try:
        return cfg.get("platforms", {}).get("email", {}).get("extra", {}).get("workflows", {}) or {}
    except Exception:
        return {}


def _list_workflows_found(workflows: dict) -> None:
    """Print available workflow names."""
    print("Available workflows:")
    for name in sorted(workflows.keys()):
        prefix = workflows[name].get("subject_prefix", name)
        desc = workflows[name].get("description", "")
        print(f"  {name}  (subject: \"{prefix}\")  — {desc}")


def _build_workflow_prompt(
    wf_config: dict,
    wf_name: str,
    file_path: str,
    sender: str,
) -> str:
    """Build the workflow prompt.

    Mirrors the template in ``email/adapter.py`` ``_dispatch_message()``
    (lines 1029-1046), adapted for CLI dispatch: output goes to stdout
    instead of an SMTP reply, and the file is a local path rather than an
    attachment.
    """
    # If the workflow has a custom prompt override, use it directly.
    custom_prompt = wf_config.get("prompt", "").strip()
    if custom_prompt:
        return custom_prompt

    wf_id = wf_name
    skill_name = wf_config.get("skill", wf_name)
    description = wf_config.get("description", "")
    subject_prefix = wf_config.get("subject_prefix", wf_name)
    filename = os.path.basename(file_path)

    original_subject = f"{subject_prefix} (test dispatch)"
    body_snippet = f"[Test dispatch from CLI — file: {file_path}]"

    return (
        f"You are running the /{wf_id} workflow sent by {sender}.\n\n"
        f"Original subject: {original_subject}\n"
        f"Original body: {body_snippet}\n\n"
        f"{description}\n\n"
        f"1. Load the `/{skill_name}` skill and follow its instructions exactly.\n"
        f"2. Process the file at: {file_path}\n"
        f"3. When done, produce the output (it will be printed to stdout).\n"
        f"   - If the workflow produces a file, include MEDIA:/path/to/output in your response.\n"
        f"   - Include a brief summary of what was done.\n"
        f"\n"
        f"CRITICAL: Work silently — your response is the deliverable. "
        f"Do NOT write chain-of-thought, reasoning, or intermediate status. "
        f"Only respond when you have a complete deliverable. "
        f"Work silently throughout the pipeline."
    )
