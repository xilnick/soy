"""Tests for soy.services.git_backend (LocalGitBackend + RemoteGitBackend)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from soy.services.git_backend import (
    GitBackend,
    LocalGitBackend,
    RemoteGitBackend,
    get_backend,
)


# ---------------------------------------------------------------------------
# get_backend factory
# ---------------------------------------------------------------------------
def test_get_backend_returns_local_by_default(monkeypatch):
    monkeypatch.delenv("SOY_GIT_BACKEND", raising=False)
    backend = get_backend("/tmp/test")
    assert isinstance(backend, LocalGitBackend)
    assert not isinstance(backend, RemoteGitBackend)


def test_get_backend_returns_local_when_set(monkeypatch):
    monkeypatch.setenv("SOY_GIT_BACKEND", "local")
    backend = get_backend("/tmp/test")
    assert isinstance(backend, LocalGitBackend)
    assert not isinstance(backend, RemoteGitBackend)


def test_get_backend_returns_remote_when_set(monkeypatch):
    monkeypatch.setenv("SOY_GIT_BACKEND", "remote")
    backend = get_backend("/tmp/test")
    assert isinstance(backend, RemoteGitBackend)


def test_get_backend_case_insensitive(monkeypatch):
    monkeypatch.setenv("SOY_GIT_BACKEND", "Remote")
    backend = get_backend("/tmp/test")
    assert isinstance(backend, RemoteGitBackend)


# ---------------------------------------------------------------------------
# LocalGitBackend — unit tests with mocked git commands
# ---------------------------------------------------------------------------
def test_local_backend_init():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    assert backend.repo_path == "/tmp/test-repo"


def test_local_backend_create_branch():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch.object(backend, "run_git") as mock_run:
        mock_run.return_value = mock.Mock(stdout="", returncode=0)
        result = backend.create_branch("feature/test")
        assert result == "feature/test"
        mock_run.assert_called_once_with(["checkout", "-b", "feature/test", "HEAD"])


def test_local_backend_create_branch_with_base():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch.object(backend, "run_git") as mock_run:
        mock_run.return_value = mock.Mock(stdout="", returncode=0)
        result = backend.create_branch("feature/test", base="abc123")
        assert result == "feature/test"
        mock_run.assert_called_once_with(["checkout", "-b", "feature/test", "abc123"])


def test_local_backend_commit_stages_all_by_default():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        # First call: git add -A
        # Second call: git commit
        # Third call: git rev-parse HEAD
        mock_run.side_effect = [
            mock.Mock(returncode=0, stdout=""),       # git add
            mock.Mock(returncode=0, stdout=""),       # git commit
            mock.Mock(returncode=0, stdout="abc123"),  # git rev-parse
        ]
        with mock.patch.object(backend, "run_git") as mock_git:
            mock_git.side_effect = [
                mock.Mock(returncode=0, stdout=""),       # git add -A
                mock.Mock(returncode=0, stdout="abc123"), # git rev-parse
            ]
            with mock.patch("subprocess.run") as mock_sub:
                mock_sub.return_value = mock.Mock(returncode=0, stdout="")
                sha = backend.commit("test commit")
                assert sha == "abc123"


def test_local_backend_commit_raises_on_failure():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            mock.Mock(returncode=0, stdout=""),          # git add -A (run_git)
            mock.Mock(returncode=1, stdout="", stderr="nothing to commit"),  # git commit fails
        ]
        with mock.patch.object(backend, "run_git") as mock_git:
            mock_git.return_value = mock.Mock(returncode=0, stdout="")
            with pytest.raises(RuntimeError, match="git commit failed"):
                with mock.patch("subprocess.run") as mock_sub:
                    mock_sub.return_value = mock.Mock(
                        returncode=1, stdout="", stderr="nothing to commit"
                    )
                    backend.commit("empty commit")


def test_local_backend_merge_branch_squash():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(backend, "run_git") as mock_git:
            mock_git.side_effect = [
                mock.Mock(returncode=0, stdout=""),       # checkout main
                mock.Mock(returncode=0, stdout=""),       # merge --squash
                mock.Mock(returncode=0, stdout="def456"), # rev-parse HEAD
            ]
            sha = backend.merge_branch("feature/test", target="main", strategy="squash")
            assert sha == "def456"


def test_local_backend_delete_branch():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch.object(backend, "run_git") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        backend.delete_branch("feature/test")
        mock_run.assert_called_once_with(["branch", "-d", "feature/test"])


def test_run_git_raises_on_failure():
    backend = LocalGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="fatal: not a git repo"
        )
        with pytest.raises(RuntimeError, match="git status failed"):
            backend.run_git(["status"])


# ---------------------------------------------------------------------------
# RemoteGitBackend — unit tests
# ---------------------------------------------------------------------------
def test_remote_backend_is_subclass():
    backend = RemoteGitBackend(repo_path="/tmp/test")
    assert isinstance(backend, LocalGitBackend)


def test_remote_backend_push():
    backend = RemoteGitBackend(repo_path="/tmp/test-repo")
    with mock.patch.object(backend, "run_git") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        backend.push("feature/test")
        mock_run.assert_called_once_with(["push", "origin", "feature/test"])


def test_remote_backend_open_pr():
    backend = RemoteGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout='{"number": 42, "url": "https://github.com/org/repo/pull/42"}',
            stderr="",
        )
        pr_num, pr_url = backend.open_pr(
            "feature/test", "Test PR", "PR body",
        )
        assert pr_num == 42
        assert pr_url == "https://github.com/org/repo/pull/42"


def test_remote_backend_open_pr_failure():
    backend = RemoteGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="gh: not authenticated"
        )
        with pytest.raises(RuntimeError, match="gh pr create failed"):
            backend.open_pr("feature/test", "Test", "body")


def test_remote_backend_merge_pr():
    backend = RemoteGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=0, stdout="merge-sha-123", stderr=""
        )
        sha = backend.merge_pr(42)
        assert sha == "merge-sha-123"


def test_remote_backend_merge_pr_failure():
    backend = RemoteGitBackend(repo_path="/tmp/test-repo")
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="merge conflict"
        )
        with pytest.raises(RuntimeError, match="gh pr merge failed"):
            backend.merge_pr(42)
