"""OpenAI function_tool wrappers for code-manipulation capabilities.

Gives OpenAI agents parity with Claude's built-in ``Read / Write / Edit /
Bash / Glob / Grep`` tools. The implementations are stdlib-only so they
work anywhere the spawnd runtime does.

Tools that modify state (``Write``, ``Edit``, ``Bash``) are skipped when
``write_allowed=False`` — the executor constructs its tool list by
calling :func:`build_code_tools` with the toolset's ``write_allowed``
flag.
"""
from __future__ import annotations
import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any
try:
    from agents import function_tool
except ImportError as err:
    raise ImportError("spawnd.tools.openai_code requires the openai-agents SDK. Install with: pip install 'spawnd.dev[openai]'") from err

def build_code_tools(cwd: Path, *, write_allowed: bool=True, env: dict[str, str] | None=None) -> list[Any]:
    """Return the list of code tools an OpenAI agent should have.

    Closes over ``cwd`` so each agent stays inside its own worktree.
    """
    shell_env = os.environ.copy()
    if env:
        _ = shell_env.update(env)

    @function_tool(name_override='Read')
    def read_file(path: str) -> str:
        """Read a UTF-8 text file and return its contents."""
        full = _resolve(cwd, path)
        return full.read_text(encoding='utf-8', errors='replace')

    @function_tool(name_override='Glob')
    def glob_tool(pattern: str) -> str:
        """Return newline-separated paths matching the glob pattern (from cwd)."""
        matches = sorted((str(p.relative_to(cwd)) for p in cwd.glob(pattern) if p.is_file()))
        return '\n'.join(matches) if matches else '(no matches)'

    @function_tool(name_override='Grep')
    def grep_tool(pattern: str, path: str | None=None) -> str:
        """Regex-search files for a pattern. Returns matching lines with file:line prefixes."""
        rx = re.compile(pattern)
        root = _resolve(cwd, path) if path else cwd
        hits: list[str] = []
        for f in _iter_files(root):
            try:
                for i, line in enumerate(f.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                    if rx.search(line):
                        _ = hits.append(f'{f.relative_to(cwd)}:{i}:{line}')
            except OSError:
                continue
            if len(hits) >= 500:
                _ = hits.append('… (truncated at 500 hits)')
                break
        return '\n'.join(hits) if hits else '(no matches)'
    tools: list[Any] = [read_file, glob_tool, grep_tool]
    if not write_allowed:
        return tools

    @function_tool(name_override='Write')
    def write_file(path: str, content: str) -> str:
        """Overwrite (or create) a UTF-8 text file at ``path``."""
        full = _resolve(cwd, path)
        _ = full.parent.mkdir(parents=True, exist_ok=True)
        _ = full.write_text(content, encoding='utf-8')
        return f'Wrote {len(content)} bytes to {path}'

    @function_tool(name_override='Edit')
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace ``old_string`` with ``new_string`` in ``path``. Must be unique."""
        full = _resolve(cwd, path)
        text = full.read_text(encoding='utf-8')
        occurrences = text.count(old_string)
        if occurrences == 0:
            return f'ERROR: old_string not found in {path}'
        if occurrences > 1:
            return f'ERROR: old_string appears {occurrences} times in {path}; must be unique'
        _ = full.write_text(text.replace(old_string, new_string), encoding='utf-8')
        return f'Edited {path} (1 replacement)'

    @function_tool(name_override='Bash')
    def bash_tool(command: str, timeout_seconds: int=120) -> str:
        """Run ``command`` in a shell rooted at the agent's worktree."""
        try:
            result = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, env=shell_env, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return f'ERROR: command timed out after {timeout_seconds}s'
        tail = f'{result.stdout}\n{result.stderr}'.strip()
        exit_note = '' if result.returncode == 0 else f'\n(exit code {result.returncode})'
        return tail + exit_note
    _ = tools.extend([write_file, edit_file, bash_tool])
    return tools

def _resolve(cwd: Path, path: str) -> Path:
    """Resolve ``path`` against ``cwd`` and forbid escaping the worktree."""
    candidate = (cwd / path).resolve()
    cwd_resolved = cwd.resolve()
    try:
        _ = candidate.relative_to(cwd_resolved)
    except ValueError as err:
        raise ValueError(f'path escapes worktree: {path}') from err
    return candidate

def _iter_files(root: Path):
    """Yield regular files under ``root``, skipping dot-directories."""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and (not fnmatch.fnmatch(d, '__pycache__'))]
        for name in filenames:
            yield (Path(dirpath) / name)
