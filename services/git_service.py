"""
asf.services.git_service
=========================

Git-as-SSOT: create the feature branch and write the spec for a mission.

When a mission is ingested (from the GitHub webhook), this service
clones-or-opens the target repo into a per-mission working directory,
creates the ``feature/asf-{issue}`` branch, writes ``spec.md`` to it,
and commits with the ASF identity. The commit SHA + branch + spec path
are written back onto the mission row (Git is the source of truth; the
DB tracks pointers).

Push to ``origin`` is gated behind ``ASF_GIT_PUSH_ENABLED`` (default
off) so local/dev runs never need write credentials. ``GitPython`` is
imported lazily so importing this module does not require ``git`` on a
host that never enables the feature.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from asf.models.mission import Mission

logger = logging.getLogger("asf.services.git_service")


def render_spec_template(mission: Mission) -> str:
    """Render the placeholder ``spec.md`` for a freshly-ingested mission.

    This is an honest M2 stub: the real RFC is produced by the planning
    agent in a later milestone. The stub records the ingested issue
    context so the branch + ``spec.md`` exist as Git-as-SSOT anchors —
    it is explicitly NOT agent output.
    """
    return (
        f"# RFC — {mission.title}\n\n"
        "> **Auto-generated placeholder (ASF M2).** This stub records the\n"
        "> ingested issue context so the feature branch and `spec.md` exist\n"
        "> as Git-as-SSOT anchors. The real RFC is produced by the planning\n"
        "> agent in a later milestone — do not treat this as agent output.\n\n"
        f"- **Mission:** `{mission.id}`\n"
        f"- **Source:** {mission.source or 'manual'} / "
        f"{mission.external_id or '—'}\n"
        f"- **Issue:** {mission.issue_id or '—'}\n"
        f"- **Branch:** {mission.branch_prefix or '—'}\n\n"
        "## Problem statement\n\n"
        f"{mission.description or '_(no description provided)_'}\n"
    )


class GitService:
    """Create the per-mission feature branch + spec commit."""

    def __init__(
        self,
        *,
        workdir: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
        push_enabled: bool | None = None,
        spec_path: str | None = None,
    ) -> None:
        from asf import config

        self.workdir = workdir or config.git_workdir()
        self.author_name = author_name or config.git_author_name()
        self.author_email = author_email or config.git_author_email()
        self.push_enabled = (
            config.git_push_enabled() if push_enabled is None else push_enabled
        )
        self.spec_path = spec_path or config.git_spec_path()

    def _repo_dir(self, mission_id) -> str:
        return os.path.join(self.workdir, str(mission_id))

    def create_branch_and_spec(self, db: Session, mission_id) -> dict:
        """Clone/open the repo, create the branch, commit ``spec.md``.

        Updates ``mission.branch``, ``spec_path`` and ``spec_commit_sha``
        (and a ``git`` block in metadata) and commits the DB change.
        Returns a summary dict. Raises ``ValueError`` if the mission is
        missing or has no ``repo_url``; git errors propagate (the caller
        — the webhook — wraps this best-effort so they never break the
        request).
        """
        from git import Actor, Repo

        mission = db.get(Mission, mission_id)
        if mission is None:
            raise ValueError(f"mission {mission_id} not found")
        if not mission.repo_url:
            raise ValueError(f"mission {mission_id} has no repo_url")

        branch_name = (
            mission.branch_prefix
            or f"feature/asf-{mission.external_id or mission.id}"
        )
        os.makedirs(self.workdir, exist_ok=True)
        path = self._repo_dir(mission_id)

        if os.path.isdir(os.path.join(path, ".git")):
            repo = Repo(path)
        else:
            repo = Repo.clone_from(mission.repo_url, path)

        with repo.config_writer() as cw:
            cw.set_value("user", "name", self.author_name)
            cw.set_value("user", "email", self.author_email)

        if branch_name in repo.heads:
            repo.heads[branch_name].checkout()
        else:
            repo.create_head(branch_name).checkout()

        spec_abs = os.path.join(repo.working_tree_dir, self.spec_path)
        os.makedirs(os.path.dirname(spec_abs) or ".", exist_ok=True)
        with open(spec_abs, "w", encoding="utf-8") as fh:
            fh.write(render_spec_template(mission))

        repo.index.add([self.spec_path])
        actor = Actor(self.author_name, self.author_email)
        commit = repo.index.commit(
            f"docs(asf): RFC spec for mission "
            f"{mission.external_id or mission.id}",
            author=actor,
            committer=actor,
        )
        sha = commit.hexsha

        pushed = False
        if self.push_enabled:
            try:
                repo.remotes.origin.push(branch_name)
                pushed = True
            except Exception:  # noqa: BLE001 — push is best-effort
                logger.exception("git push failed for mission %s", mission_id)

        mission.branch = branch_name
        mission.spec_path = self.spec_path
        mission.spec_commit_sha = sha
        md = dict(mission.mission_metadata or {})
        md["git"] = {
            "branch": branch_name,
            "spec_path": self.spec_path,
            "commit_sha": sha,
            "pushed": pushed,
        }
        mission.mission_metadata = md
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "git-ssot: mission %s branch=%s spec=%s commit=%s pushed=%s",
            mission_id, branch_name, self.spec_path, sha[:8], pushed,
        )
        return {
            "branch": branch_name,
            "spec_path": self.spec_path,
            "commit_sha": sha,
            "pushed": pushed,
        }
