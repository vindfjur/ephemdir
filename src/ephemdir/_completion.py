"""Shell completion scripts. This module is intentionally read-only."""

from __future__ import annotations

__all__ = ["completion_script"]


_COMMANDS = (
    "new list path keep extend rm sweep prune recover watch shell-init "
    "install-service uninstall-service doctor explain menu completion"
)


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
