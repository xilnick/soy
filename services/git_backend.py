"""
soy.services.git_backend
========================

Git backend abstraction for mission workflow.

Provides local-only git operations (branch, commit, merge) and optional
remote operations (push, PR). The backend is selected at runtime via
``SOY_GIT_BACKEND`` env var: ``local`` (default) or ``remote``.

Local backend:
- Creates feature branches in a working repo
- Commits changes via git CLI
- Merges branches directly (squash merge)

Remote backend (extends local):
- Pushes branches to origin
- Opens PRs via ``gh`` CLI
- Merges PRs via ``gh pr merge``
"""

from __future__ import annotations

import logging
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger("soy.services.git_backend")


class GitBackend(ABC):
    """Base class: local git operations."""

    def __init__(self, *, repo_path: str) -> None:
        self.repo_path = repo_path

    @abstractmethod
    def create_branch(self, branch_name: str, base: str = "HEAD") -> str:
        """Create a feature branch from base.

        Returns the branch name.
        Raises RuntimeError if git command fails.
        """
        ...

    @abstractmethod
    def commit(
        self,
        message: str,
        files: list[str] | None = None,
        *,
        author_name: str = "Soy Bot",
        author_email: str = "soy-bot@piperoni.local",
    ) -> str:
        """Stage files and commit.

        If ``files`` is None, stages all changes (``git add -A``).
        Returns the commit SHA.
        Raises RuntimeError if commit fails.
        """
        ...

    @abstractmethod
    def merge_branch(
        self,
        branch_name: str,
        target: str = "main",
        strategy: str = "squash",
    ) -> str:
        """Merge a feature branch into target.

        ``strategy``: ``"squash"`` (default) or ``"merge"``.
        Returns the merge commit SHA.
        Raises RuntimeError if merge fails.
        """
        ...

    @abstractmethod
    def delete_branch(self, branch_name: str) -> None:
        """Delete a local branch after merge."""
        ...

    def run_git(self, args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        """Run a git command and return the result."""
        cmd = ["git"] + args
        result = subprocess.run(
            cmd,
            cwd=cwd or self.repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result


class LocalGitBackend(GitBackend):
    """Local-only git operations (no remote)."""

    def create_branch(self, branch_name: str, base: str = "HEAD") -> str:
        from soy.security import validate_branch_name
        validate_branch_name(branch_name)
        self.run_git(["checkout", "-b", branch_name, base])
        logger.info("local-git: created branch %s from %s", branch_name, base)
        return branch_name

    def commit(
        self,
        message: str,
        files: list[str] | None = None,
        *,
        author_name: str = "Soy Bot",
        author_email: str = "soy-bot@piperoni.local",
    ) -> str:
        if files:
            self.run_git(["add"] + files)
        else:
            self.run_git(["add", "-A"])
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = author_name
        env["GIT_AUTHOR_EMAIL"] = author_email
        env["GIT_COMMITTER_NAME"] = author_name
        env["GIT_COMMITTER_EMAIL"] = author_email
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git commit failed: {result.stderr.strip()}"
            )
        # Get the commit SHA
        sha_result = self.run_git(["rev-parse", "HEAD"])
        sha = sha_result.stdout.strip()
        logger.info("local-git: committed %s: %s", sha[:8], message)
        return sha

    def merge_branch(
        self,
        branch_name: str,
        target: str = "main",
        strategy: str = "squash",
    ) -> str:
        from soy.security import validate_branch_name
        validate_branch_name(branch_name)
        validate_branch_name(target)
        self.run_git(["checkout", target])
        if strategy == "squash":
            self.run_git(["merge", "--squash", branch_name])
            env = dict(os.environ)
            env["GIT_AUTHOR_NAME"] = "Soy Bot"
            env["GIT_AUTHOR_EMAIL"] = "soy-bot@piperoni.local"
            env["GIT_COMMITTER_NAME"] = "Soy Bot"
            env["GIT_COMMITTER_EMAIL"] = "soy-bot@piperoni.local"
            result = subprocess.run(
                ["git", "commit", "-m", f"Merge branch into {target} (squash)"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git squash merge commit failed: {result.stderr.strip()}"
                )
        else:
            self.run_git(["merge", branch_name])
        sha_result = self.run_git(["rev-parse", "HEAD"])
        sha = sha_result.stdout.strip()
        logger.info(
            "local-git: merged %s into %s: %s",
            branch_name, target, sha[:8],
        )
        return sha

    def delete_branch(self, branch_name: str) -> None:
        self.run_git(["branch", "-d", branch_name])
        logger.info("local-git: deleted branch %s", branch_name)


class RemoteGitBackend(LocalGitBackend):
    """Extends local backend with remote operations (push, PR)."""

    def push(self, branch_name: str) -> None:
        """Push branch to origin."""
        self.run_git(["push", "origin", branch_name])
        logger.info("remote-git: pushed %s to origin", branch_name)

    def open_pr(
        self,
        branch: str,
        title: str,
        body: str,
        *,
        base: str = "main",
    ) -> tuple[int, str]:
        """Create a pull request via ``gh pr create``.

        Returns ``(pr_number, pr_url)``.
        Raises RuntimeError if ``gh`` is not installed or the
        command fails.
        """
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", base,
                "--head", branch,
                "--title", title,
                "--body", body,
                "--json", "number,url",
            ],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {result.stderr.strip()}")

        import json
        data = json.loads(result.stdout)
        pr_number = data["number"]
        pr_url = data["url"]
        logger.info("remote-git: PR opened: %s (branch=%s)", pr_url, branch)
        return pr_number, pr_url

    def merge_pr(
        self,
        pr_number: int,
        *,
        delete_branch: bool = True,
    ) -> str:
        """Merge a PR via ``gh pr merge --squash``.

        Returns the merge SHA.
        Raises RuntimeError if the merge fails.
        """
        cmd = ["gh", "pr", "merge", str(pr_number), "--squash"]
        if delete_branch:
            cmd.append("--delete-branch")

        result = subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh pr merge failed: {result.stderr.strip()}")

        merge_sha = (
            result.stdout.strip().splitlines()[-1]
            if result.stdout.strip()
            else ""
        )
        logger.info(
            "remote-git: PR merged: #%s sha=%s",
            pr_number, merge_sha[:8] if merge_sha else "?",
        )
        return merge_sha


def get_backend(repo_path: str) -> GitBackend:
    """Factory: return a GitBackend based on ``SOY_GIT_BACKEND`` env var.

    Returns :class:`LocalGitBackend` when ``SOY_GIT_BACKEND=local``
    (the default) and :class:`RemoteGitBackend` when
    ``SOY_GIT_BACKEND=remote``.
    """
    backend_type = os.getenv("SOY_GIT_BACKEND", "local").strip().lower()
    if backend_type == "remote":
        return RemoteGitBackend(repo_path=repo_path)
    return LocalGitBackend(repo_path=repo_path)
