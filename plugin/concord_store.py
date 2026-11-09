"""Where Concord keeps its state, and how a call is configured.

Two things live here so the tool, the CLI and the tests resolve them one way:

  * the **store path** - ``<hermes home>/plugin-data/concord-for-hermes/
    coordinator.db`` by default. That directory is the documented plugin data
    root: it survives ``hermes plugins update`` and is *not* deleted by
    ``hermes plugins remove``, which is exactly why a disable or removal is a
    documented, recoverable operation rather than a silent data loss.
  * the **settings** - an explicit repository allowlist, the ``gh`` executable
    prefix and the per-call timeout. The allowlist is configuration; no model
    argument can widen it.

``CONCORD_DB`` overrides the store path. It exists for operators who keep the
store somewhere else and for the test harness; it is the only environment
override and it cannot change *what* is protected, only *where* the record is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

#: Must equal ``name`` in plugin.yaml: the data directory is derived from it, and
#: tests assert the two agree so a rename cannot silently orphan a store.
PLUGIN_NAME = "concord-for-hermes"
STORE_FILENAME = "coordinator.db"

DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Settings:
    allowed_repositories: tuple[tuple[str, str], ...] = ()
    gh_command: tuple[str, ...] = ("gh",)
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS
    store_path: Path = field(default_factory=lambda: Path("."))

    @property
    def allowed_slugs(self) -> list[str]:
        return sorted(f"{owner}/{repo}" for owner, repo in self.allowed_repositories)


def data_dir() -> Path:
    """The sanctioned per-plugin data root for the active profile."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def store_path(*, create: bool = True) -> Path:
    """The coordinator store. ``create=False`` resolves the same path without
    creating the data directory, so a read-only command leaves no trace."""
    override = (os.environ.get("CONCORD_DB") or "").strip()
    if override:
        return Path(override)
    if not create:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "plugin-data" / PLUGIN_NAME / STORE_FILENAME
    return data_dir() / STORE_FILENAME


def _pairs(raw: Any) -> tuple[tuple[str, str], ...]:
    """Read owner/repo entries, refusing anything that is not exactly that."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [part for part in raw.replace(",", " ").split() if part]
    if not isinstance(raw, Iterable):
        return ()
    out: list[tuple[str, str]] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        owner, sep, repo = text.partition("/")
        if not sep or not owner or not repo or "/" in repo:
            continue
        out.append((owner, repo))
    return tuple(sorted(set(out)))


def _command(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = [part for part in raw.split() if part] or ["gh"]
        return tuple(parts)
    if isinstance(raw, Iterable):
        parts = tuple(str(part) for part in raw if str(part or "").strip())
        return parts or ("gh",)
    return ("gh",)


def resolve_settings(
    config: Optional[Mapping[str, Any]] = None,
    *,
    store: Optional[Path] = None,
) -> Settings:
    """Build settings from a plain mapping of plugin settings.

    ``config`` is what ``ctx.get_config`` returns for this plugin, or an explicit
    mapping in tests. Missing keys fall back to the safe default: an empty
    allowlist refuses every call rather than guessing a target.
    """
    config = dict(config or {})
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError):
        timeout_s = float(DEFAULT_TIMEOUT_SECONDS)
    if timeout_s <= 0:
        timeout_s = float(DEFAULT_TIMEOUT_SECONDS)
    return Settings(
        allowed_repositories=_pairs(config.get("allowed_repositories")),
        gh_command=_command(config.get("gh_command")),
        timeout_s=timeout_s,
        store_path=Path(store) if store is not None else store_path(),
    )


def settings_from_ctx(ctx: Any, *, store: Optional[Path] = None) -> Settings:
    """Read the plugin's own settings namespace, tolerating a bare context."""
    config: dict[str, Any] = {}
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        for key in ("allowed_repositories", "gh_command", "timeout_seconds"):
            value = getter(key, None)
            if value is not None:
                config[key] = value
    return resolve_settings(config, store=store)


try:  # imported as part of the plugin package
    from . import concord_gh_adapter as A
except ImportError:  # imported flat from the installed plugin directory (tests)
    import concord_gh_adapter as A


def build_adapter(settings: Settings):
    """The one adapter construction site. Raises when the allowlist is empty."""
    return A.GithubContentsAdapter(
        allowed_repositories=settings.allowed_repositories,
        gh_command=settings.gh_command,
        timeout_s=settings.timeout_s,
    )
