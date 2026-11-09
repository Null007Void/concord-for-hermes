"""Concord for Hermes - product plugin.

Three registrations, one product:

  * ``concord_github_write`` - the one supported action. The handler does the
    admission, the provider write, the read-back and the settlement in a single
    frame (architecture B, per the C1 verdict): there is no framework seam
    between the decision and the write, so a coordinator failure is a refusal.
  * ``hermes concord`` - the human surface (status / reconcile / cancel / export).
  * the ``write-file`` skill - the agent-facing instructions that explain when to
    reach for the tool and how to read its five outcomes.

Registration only wires these up. It opens no store, runs no ``gh`` and reads no
credential, so plugin loading (and the catalog validator's isolated probe) has no
side effect beyond registering.
"""

from __future__ import annotations

from pathlib import Path

try:  # imported as part of the plugin package
    from . import concord_cli, concord_tool
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_cli
    import concord_tool

SKILL_PATH = Path(__file__).resolve().parent / "skills" / "write-file" / "SKILL.md"

__all__ = ["register", "SKILL_PATH"]


def register(ctx) -> None:
    """Called once by the real PluginManager."""
    ctx.register_tool(
        name=concord_tool.TOOL_NAME,
        toolset=concord_tool.TOOLSET,
        schema=concord_tool.SCHEMA,
        handler=lambda args, **kwargs: concord_tool.handle_write(args, ctx=ctx),
        description="Coordinated GitHub file update: admission, conditional write, read-back.",
    )
    ctx.register_cli_command(
        name="concord",
        help="Inspect and recover Concord's coordinated GitHub writes",
        setup_fn=concord_cli.build_parser,
        handler_fn=concord_cli.make_handler(ctx),
        description=concord_cli.DESCRIPTION,
    )
    ctx.register_skill(
        "write-file",
        SKILL_PATH,
        description="When and how to use concord_github_write, and how to read its outcomes.",
    )
