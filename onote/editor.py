"""Editor thin-shell — wraps clitools.editor with onote-specific env/config.

Resolution priority (see clitools.editor):
    $ONOTE_EDITOR > $EDITOR > config["editor"] > nvim > vim > auto-install

The actual logic lives in clitools.editor; this file wraps it with
onote-specific env vars + reads config["editor"] so sync.py stays clean.

Tests patch ``onote.editor.edit_with_fallback`` to bypass real nvim.
"""
from clitools.config import load_config
from clitools.editor import (  # noqa: F401
    EditorConfigError,
    ensure_editor,
    run_editor,
)
from clitools.editor import edit_with_fallback as _clitools_edit


def edit_with_fallback(file_path):
    """Open file in onote's editor (env-driven, config-overridable)."""
    return _clitools_edit(
        file_path,
        env_vars=("ONOTE_EDITOR", "EDITOR"),
        config_editor=load_config().get("editor", ""),
    )