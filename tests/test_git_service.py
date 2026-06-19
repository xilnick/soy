"""Tests for soy.services.git_service — extended methods.

Tests the new methods: create_worktree, commit_and_push, open_pr, merge_pr.
All git/gh subprocess calls are mocked.
"""

from unittest import mock

import pytest


class TestGitServiceOpenPR:
    def test_open_pr_success(self, tmp_path):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)

        import json
        fake_stdout = json.dumps({"number": 42, "url": "https://github.com/org/repo/pull/42"})
        fake_result = mock.Mock(returncode=0, stdout=fake_stdout, stderr="")

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            pr_num, pr_url = gs.open_pr(
                "feature/soy-1", "Fix tests", "Body text",
                cwd=str(tmp_path),
            )

        assert pr_num == 42
        assert pr_url == "https://github.com/org/repo/pull/42"
        cmd = m_run.call_args[0][0]
        assert cmd[:3] == ["gh", "pr", "create"]
        assert "--head" in cmd
        assert "feature/soy-1" in cmd

    def test_open_pr_failure_raises(self, tmp_path):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)
        fake_result = mock.Mock(returncode=1, stdout="", stderr="permission denied")

        with mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(RuntimeError, match="gh pr create failed"):
                gs.open_pr("branch", "title", "body", cwd=str(tmp_path))


class TestGitServiceMergePR:
    def test_merge_pr_success(self):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)
        fake_result = mock.Mock(returncode=0, stdout="abc123def\n", stderr="")

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            sha = gs.merge_pr(42, cwd="/some/repo")

        assert sha == "abc123def"
        cmd = m_run.call_args[0][0]
        assert cmd[:3] == ["gh", "pr", "merge"]
        assert "--squash" in cmd
        assert "--delete-branch" in cmd

    def test_merge_pr_failure_raises(self):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)
        fake_result = mock.Mock(returncode=1, stdout="", stderr="merge conflict")

        with mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(RuntimeError, match="gh pr merge failed"):
                gs.merge_pr(99)


class TestGitServiceCommitAndPush:
    def test_commit_and_push_success(self, tmp_path):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=False)

        fake_repo = mock.Mock()
        fake_commit = mock.Mock()
        fake_commit.hexsha = "abc12345"
        fake_repo.index.commit.return_value = fake_commit
        fake_repo.config_writer.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        fake_repo.config_writer.return_value.__exit__ = mock.Mock(return_value=False)

        with mock.patch("git.Repo", return_value=fake_repo):
            sha = gs.commit_and_push(str(tmp_path), "test commit")

        assert sha == "abc12345"
        fake_repo.git.add.assert_called_once_with(A=True)

    def test_commit_push_pushes_when_enabled(self, tmp_path):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)

        fake_repo = mock.Mock()
        fake_commit = mock.Mock()
        fake_commit.hexsha = "abc12345"
        fake_repo.index.commit.return_value = fake_commit
        fake_repo.active_branch.name = "feature/soy-1"
        fake_repo.config_writer.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        fake_repo.config_writer.return_value.__exit__ = mock.Mock(return_value=False)

        with mock.patch("git.Repo", return_value=fake_repo):
            gs.commit_and_push(str(tmp_path), "test commit")

        fake_repo.remotes.origin.push.assert_called_once_with("feature/soy-1")


class TestGitServiceCreateWorktree:
    def test_create_worktree_success(self, tmp_path):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)
        # Mock the repo dir to look like it has a .git
        repo_dir = gs._repo_dir("mission-123")

        fake_result = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("os.path.isdir", return_value=True):
            with mock.patch("os.makedirs"):
                with mock.patch("subprocess.run", return_value=fake_result) as m_run:
                    path = gs.create_worktree("mission-123", "feature/soy-1")

        assert "/tmp/soy-worktrees/mission-123" == path
        # Should have tried to add worktree
        calls = [c for c in m_run.call_args_list if "worktree" in str(c)]
        assert len(calls) >= 1

    def test_create_worktree_raises_if_not_cloned(self):
        from soy.services.git_service import GitService

        gs = GitService(push_enabled=True)

        with mock.patch("os.path.isdir", return_value=False):
            with mock.patch("os.makedirs"):
                with pytest.raises(RuntimeError, match="Repo not cloned"):
                    gs.create_worktree("mission-123", "feature/soy-1")
