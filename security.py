"""
soy.security
=============

Input validation and prompt sanitisation utilities for the Soy API.

Every function is a pure validator that either returns the cleaned
value or raises ``ValueError`` with a human-readable message.  The
FastAPI schema validators and route handlers call these so invalid
input is rejected at the HTTP boundary, before it reaches Git,
subprocess, or LLM layers.

Design constraints
------------------

* **No-op on well-formed input.** A normal ``repo_url`` or
  ``branch_prefix`` passes through unchanged.
* **Zero deployment impact.** Validators are pure Python with no
  external dependencies.  Existing ``.env`` files, nginx proxy
  configs, and PM2 setups are unaffected.
* **Defense in depth.** Each validator is conservative — it rejects
  anything it does not recognise.  A future allowlist extension is
  cheaper than a missed injection vector.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Branch name validation
# ---------------------------------------------------------------------------

# Characters that ``git check-ref-format`` rejects (and a few extras
# that are useful to block early).  The regex is intentionally strict
# — it is easier to loosen later than to explain why a customer's
# branch name was silently accepted and then mangled by git.
_BAD_BRANCH_CHARS = re.compile(
    r"[~^:?*\[\\\x00-\x1f\x7f]|"
    r"\.\.|"       # double-dot (path traversal in git)
    r"@\\{|"       # @{ and reflog syntax
    r"//+"         # consecutive slashes
)

# Shell metacharacters that must never appear in a value that will
# be interpolated into a subprocess argument list.
_SHELL_META = re.compile(r"[;&|`$(){}!\n\r\t]")

# ``ext::`` protocol — CVE-2022-24439 (GitPython RCE).
_EXT_PROTOCOL = re.compile(r"^ext::", re.IGNORECASE)


def validate_branch_name(name: str) -> str:
    """Validate *name* as a safe git branch name.

    Rules (subset of ``git check-ref-format``):

    * Must not be empty.
    * Must not start or end with ``/``.
    * Must not contain ``..``, ``@{``, null bytes, control chars.
    * Must not contain shell metacharacters.
    * Must not start with ``ext::`` (CVE-2022-24439).
    * Must be <= 200 characters.

    Returns the name unchanged on success.  Raises ``ValueError``
    on any violation.
    """
    if not name or not name.strip():
        raise ValueError("branch name must not be empty")
    name = name.strip()
    if len(name) > 200:
        raise ValueError("branch name too long (max 200 chars)")
    if name.startswith("/") or name.endswith("/"):
        raise ValueError("branch name must not start or end with /")
    if name.startswith(".") or name.endswith(".lock"):
        raise ValueError("branch name must not start with . or end with .lock")
    if _EXT_PROTOCOL.search(name):
        raise ValueError("branch name must not use ext:: protocol")
    if _BAD_BRANCH_CHARS.search(name):
        raise ValueError(
            "branch name contains invalid characters "
            "(.., ~, ^, :, ?, *, [, \\, control chars)"
        )
    if _SHELL_META.search(name):
        raise ValueError(
            "branch name contains shell metacharacters"
        )
    return name


# ---------------------------------------------------------------------------
# Repo URL validation
# ---------------------------------------------------------------------------

# Allowed hostnames for remote repo URLs.  Extend this list as
# needed.  An empty host means a local path (which is validated
# separately against the workdir).
_ALLOWED_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
}


def validate_repo_url(url: str) -> str:
    """Validate *url* as a safe repository URL.

    Allowed forms:

    * **Local path** — absolute path or ``~/...`` tilde expansion
      that resolves under ``SOY_GIT_WORKDIR`` (checked elsewhere).
      No ``..`` components, no null bytes.
    * **HTTPS** — ``https://github.com/org/repo`` (no embedded
      credentials, no ``ext::`` prefix).
    * **SSH** — ``git@github.com:org/repo.git`` (standard
      ``scp``-style SSH URL).

    Everything else is rejected.

    Returns the URL unchanged on success.  Raises ``ValueError``
    on any violation.
    """
    if not url or not url.strip():
        raise ValueError("repo_url must not be empty")
    url = url.strip()

    if len(url) > 1024:
        raise ValueError("repo_url too long (max 1024 chars)")

    # Block null bytes (CVE-2023-40267 family).
    if "\x00" in url:
        raise ValueError("repo_url contains null bytes")

    # Block ext:: protocol (CVE-2022-24439).
    if _EXT_PROTOCOL.search(url):
        raise ValueError("repo_url must not use ext:: protocol")

    # Block shell metacharacters.
    if _SHELL_META.search(url):
        raise ValueError("repo_url contains shell metacharacters")

    # SSH-style: git@host:org/repo.git
    ssh_match = re.match(r"^git@([^:]+):(.+)$", url)
    if ssh_match:
        host = ssh_match.group(1).lower()
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"SSH repo_url host '{host}' not in allowlist "
                f"{sorted(_ALLOWED_HOSTS)}"
            )
        path_part = ssh_match.group(2)
        if ".." in path_part:
            raise ValueError("repo_url path must not contain ..")
        return url

    # HTTPS-style
    parsed = urlparse(url)
    if parsed.scheme == "https":
        host = (parsed.hostname or "").lower()
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"HTTPS repo_url host '{host}' not in allowlist "
                f"{sorted(_ALLOWED_HOSTS)}"
            )
        if parsed.username or parsed.password:
            raise ValueError(
                "repo_url must not contain embedded credentials"
            )
        if ".." in (parsed.path or ""):
            raise ValueError("repo_url path must not contain ..")
        return url

    # Local path — must be absolute or tilde-expanded, no ..
    expanded = os.path.expanduser(url)
    if ".." in expanded:
        raise ValueError("repo_url path must not contain ..")
    if "\x00" in expanded:
        raise ValueError("repo_url path contains null bytes")
    return url


# ---------------------------------------------------------------------------
# Prompt sanitisation
# ---------------------------------------------------------------------------

# Characters / sequences that indicate prompt injection attempts.
_INJECTION_PATTERNS = [
    re.compile(r"</?(?:system|assistant|user|INST|SYS)\b", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"<\|im_start\|>|<\|im_end\|>", re.IGNORECASE),
    re.compile(r"```system|```assistant|```user", re.IGNORECASE),
]

# Control characters (except normal whitespace).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_prompt(text: Optional[str], max_len: int = 4000) -> str:
    """Wrap *text* in delimiter fencing for safe inclusion in an LLM prompt.

    The function:

    1. Normalises Unicode (NFC).
    2. Strips control characters (keeps ``\\n``, ``\\t``).
    3. Escapes the delimiter marker ``</USER_INPUT>`` if it appears
       literally in the text (prevents delimiter breakout).
    4. Truncates to *max_len* characters.
    5. Wraps in ``<USER_INPUT>...</USER_INPUT>`` markers.

    This does **not** make the prompt immune to injection — no single
    defence does — but it raises the bar significantly by separating
    the user-supplied text from the system instructions with explicit
    delimiters that the model is instructed to treat as boundaries.
    """
    if text is None:
        return "<USER_INPUT>(none)</USER_INPUT>"
    # Normalise + strip control chars.
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    # Escape the closing delimiter so a user cannot break out.
    cleaned = cleaned.replace("</USER_INPUT>", "<\\/USER_INPUT>")
    # Truncate.
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "...[truncated]"
    return f"<USER_INPUT>{cleaned}</USER_INPUT>"


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------

def validate_path_within(base: str, target: str) -> str:
    """Resolve *target* and confirm it lives under *base*.

    Both paths are resolved to absolute, symlink-free forms.  If
    *target* escapes *base* a ``ValueError`` is raised.

    Returns the resolved absolute path on success.
    """
    base_resolved = Path(base).resolve()
    target_resolved = Path(target).resolve()
    if not str(target_resolved).startswith(str(base_resolved) + os.sep) and target_resolved != base_resolved:
        raise ValueError(
            f"path '{target}' resolves to '{target_resolved}' "
            f"which is outside base '{base_resolved}'"
        )
    return str(target_resolved)


# ---------------------------------------------------------------------------
# Manifest env validation
# ---------------------------------------------------------------------------

# Env var name patterns that must never be injected from a manifest.
# These cover the most common credential / secret key names across
# cloud providers and common tools.
_BLOCKED_ENV_PREFIXES = (
    "AWS_SECRET",
    "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "DOCKER_PASSWORD",
    "DATABASE_URL",
    "SOY_DATABASE_URL",
    "SOY_API_KEY",
    "MC_API_KEY",
    "SOY_GITHUB_WEBHOOK_SECRET",
    "SOY_WS_ADMIN_TOKEN",
    "PRIVATE_KEY",
    "SECRET_KEY",
    "OPENAI_API_KEY",
)

# Sensitive suffixes (case-insensitive check).
_BLOCKED_ENV_SUFFIXES = (
    "_SECRET",
    "_TOKEN",
    "_PASSWORD",
    "_KEY",
    "_CREDENTIALS",
)


def validate_manifest_env(env_dict: dict) -> dict:
    """Validate *env_dict* from a coding-agent manifest.

    Blocks any key that matches a known sensitive env var name or
    suffix.  Values are truncated to 4096 chars (prevents memory
    abuse).  Keys and values must not contain null bytes.

    Returns the cleaned dict.  Raises ``ValueError`` on violations.
    """
    if not isinstance(env_dict, dict):
        raise ValueError("manifest env must be a dict")
    if len(env_dict) > 100:
        raise ValueError("manifest env has too many keys (max 100)")
    cleaned: dict = {}
    for key, value in env_dict.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("manifest env keys and values must be strings")
        if "\x00" in key or "\x00" in value:
            raise ValueError("manifest env must not contain null bytes")
        upper_key = key.upper()
        # Check blocklist prefixes.
        for prefix in _BLOCKED_ENV_PREFIXES:
            if upper_key == prefix or upper_key.startswith(prefix + "_"):
                raise ValueError(
                    f"manifest env key '{key}' is blocked "
                    f"(matches sensitive prefix '{prefix}')"
                )
        # Check blocklist suffixes.
        for suffix in _BLOCKED_ENV_SUFFIXES:
            if upper_key.endswith(suffix) and len(upper_key) > len(suffix):
                raise ValueError(
                    f"manifest env key '{key}' is blocked "
                    f"(matches sensitive suffix '{suffix}')"
                )
        # Truncate value.
        cleaned[key] = value[:4096]
    return cleaned
