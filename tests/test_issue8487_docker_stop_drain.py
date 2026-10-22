"""Regression tests for Issue #8487 — Docker stop drain and SIGTERM forwarding.

Issue #8487: `docker stop` SIGKILLs the WebUI server — the #7293 stop drain
never runs in the bundled image because `su` drops privileges by forking a child
shell and trapping SIGTERM instead of forwarding it to `server.py`.
Furthermore, `python server.py` was executed without `exec`, leaving bash
as PID 1, and the compose templates lacked `stop_grace_period` (Docker defaults
to 10s, which is much shorter than the 90s shutdown drain).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_docker_init_uses_setpriv_for_privilege_drop():
    """docker_init.bash must use setpriv to drop privileges without forking,
    falling back to su only if setpriv is unavailable."""
    script = (REPO / "docker_init.bash").read_text(encoding="utf-8")
    assert "setpriv" in script, "docker_init.bash must check for and use setpriv"
    assert "exec setpriv --reuid=" in script, (
        "docker_init.bash must exec setpriv to preserve PID 1"
    )
    assert "--init-groups" in script


def test_docker_init_execs_python_server():
    """docker_init.bash must exec python server.py so Python replaces the shell
    process and receives SIGTERM directly."""
    script = (REPO / "docker_init.bash").read_text(encoding="utf-8")
    assert "exec python server.py" in script, (
        "docker_init.bash must use `exec python server.py` instead of running it "
        "as a non-exec child process"
    )
    # Ensure the old non-exec invocation is gone
    assert "python server.py || error_exit" not in script


def test_compose_files_declare_stop_grace_period():
    """All docker-compose files must set stop_grace_period: 120s for webui
    so the 90s shutdown drain completes before Docker sends SIGKILL."""
    compose_files = [
        "docker-compose.yml",
        "docker-compose.two-container.yml",
        "docker-compose.three-container.yml",
    ]
    for filename in compose_files:
        content = (REPO / filename).read_text(encoding="utf-8")
        assert "stop_grace_period: 120s" in content, (
            f"{filename} must specify `stop_grace_period: 120s` for the webui container"
        )


def test_docker_and_supervisor_docs_mention_stop_grace_period():
    """Documentation in docs/docker.md and docs/supervisor.md must explain
    the shutdown drain and stop_grace_period requirement."""
    docker_doc = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")
    supervisor_doc = (REPO / "docs" / "supervisor.md").read_text(encoding="utf-8")

    assert "stop_grace_period" in docker_doc
    assert "HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS" in docker_doc

    assert "setpriv" in supervisor_doc
    assert "stop_grace_period" in supervisor_doc
