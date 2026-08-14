"""Shell completion scripts: generation, verified install/uninstall, status.

Script *generation* stays pure. Installation reuses the service module's
installed-files discipline: the atomic verified write, the owner-only
manifest of exact content digests, and the verified fd-relative removal —
so an edited or foreign completion file is refused, never deleted blindly.
"""

from __future__ import annotations

import os
from pathlib import Path

from ._service import (
    ServiceError,
    _installed_file_state,
    _record_installed_file,
    _remove_recorded_installed_file,
    _write_service_file,
)

__all__ = [
    "completion_script",
    "install_completion",
    "uninstall_completion",
    "completion_status",
]


_COMMANDS = (
    "new list path keep extend rm sweep prune recover watch shell-init "
    "install-service uninstall-service service doctor explain menu completion"
)

# Shells with a conventional per-user completion location. PowerShell has no
# file ephemdir could own (completions live in the user's profile script), so
# it stays print-only via `completion show`.
_INSTALLABLE_SHELLS = ("bash", "zsh", "fish")


def completion_script(shell: str) -> str:
    shell = shell.lower()
    if shell == "bash":
        return f"""\
_ephemdir_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{_COMMANDS}" -- "$cur") )
  fi
}}
complete -F _ephemdir_complete ephemdir
"""
    if shell == "zsh":
        commands = " ".join(f"{command}\\:{command}" for command in _COMMANDS.split())
        return f"""\
#compdef ephemdir
_ephemdir() {{
  local -a commands
  commands=({commands})
  if (( CURRENT == 2 )); then
    _describe 'ephemdir command' commands
  fi
}}
_ephemdir "$@"
"""
    if shell == "fish":
        return f"complete -c ephemdir -f -n '__fish_use_subcommand' -a '{_COMMANDS}'\n"
    if shell in {"powershell", "pwsh"}:
        return f"""\
Register-ArgumentCompleter -Native -CommandName ephemdir -ScriptBlock {{
  param($wordToComplete, $commandAst, $cursorPosition)
  "{_COMMANDS}".Split(" ") | Where-Object {{ $_ -like "$wordToComplete*" }}
}}
"""
    raise ValueError("shell must be bash, zsh, fish, or powershell")


def _completion_target(shell: str) -> Path:
    """Per-user completion file location for ``shell``."""
    home = Path.home()
    raw_data = os.environ.get("XDG_DATA_HOME")
    raw_config = os.environ.get("XDG_CONFIG_HOME")
    candidate_data = Path(raw_data).expanduser() if raw_data else None
    candidate_config = Path(raw_config).expanduser() if raw_config else None
    # The XDG base-directory spec requires absolute paths. Ignoring relative
    # values also keeps manifest identities stable across cwd changes.
    xdg_data = (
        candidate_data
        if candidate_data is not None and candidate_data.is_absolute()
        else home / ".local" / "share"
    )
    xdg_config = (
        candidate_config
        if candidate_config is not None and candidate_config.is_absolute()
        else home / ".config"
    )
    if shell == "bash":
        return xdg_data / "bash-completion" / "completions" / "ephemdir"
    if shell == "zsh":
        return xdg_data / "zsh" / "site-functions" / "_ephemdir"
    if shell == "fish":
        return xdg_config / "fish" / "completions" / "ephemdir.fish"
    raise ServiceError(
        f"completion install is not supported for {shell}; "
        "use `ephemdir completion show` and source the output yourself"
    )


def _activation_hint(shell: str, target: Path) -> str | None:
    if shell == "bash":
        return "loaded automatically by the bash-completion package"
    if shell == "zsh":
        return f"add `fpath+={target.parent}` before `compinit` in your zshrc"
    return None  # fish autoloads ~/.config/fish/completions


def install_completion(shell: str) -> str:
    """Atomically install ephemdir's own completion file for ``shell``.

    The file is written through the verified-directory atomic writer and its
    exact digest is recorded in the installed-files manifest, so a later
    uninstall can prove it still removes ephemdir's file.
    """
    script = completion_script(shell)
    target = _completion_target(shell)
    _write_service_file(target, script)
    _record_installed_file(target, script)
    message = f"installed {shell} completion at {target}"
    hint = _activation_hint(shell, target)
    return f"{message} ({hint})" if hint else message


def uninstall_completion(shell: str) -> str:
    """Remove ephemdir's completion file for ``shell`` after verification."""
    target = _completion_target(shell)
    if not os.path.lexists(target):
        return f"no {shell} completion installed"
    _remove_recorded_installed_file(target)
    return f"removed {shell} completion {target}"


def completion_status(shell: str | None = None) -> list[dict[str, str]]:
    """Read-only per-shell completion report; never creates or writes.

    ``status`` is :func:`ephemdir._service._installed_file_state` extended
    with the current expected script, so an ``installed`` verdict also means
    the file matches what this ephemdir version would generate now, while
    ``outdated`` means it is our recorded file from an older version.
    """
    shells = (shell,) if shell else _INSTALLABLE_SHELLS
    report = []
    for name in shells:
        target = _completion_target(name)
        report.append(
            {
                "shell": name,
                "path": str(target),
                "status": _installed_file_state(target, completion_script(name)),
            }
        )
    return report
