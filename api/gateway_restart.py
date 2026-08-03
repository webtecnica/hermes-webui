"""Helpers for restarting the active-profile Hermes gateway."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from api.profiles import (
    _PROFILE_ID_RE,
    _is_root_profile,
    get_active_hermes_home,
    get_active_profile_name,
    get_hermes_home_for_profile,
)

logger = logging.getLogger(__name__)

_GATEWAY_RESTART_LOCK = threading.Lock()

# Name of the gateway's persisted runtime status file, written by the gateway
# process itself (gateway/status.py) into the HERMES_HOME it was launched with.
# The record carries the gateway's own PID under the "pid" key.
_GATEWAY_RUNTIME_STATUS_FILE = "gateway_state.json"


def _resolve_hermes_command() -> str:
    """Resolve the CLI path used for active-profile gateway restarts."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd

    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _consume_stream(stream) -> None:
    """Drain a subprocess stream to prevent stdout/stderr pipe deadlocks."""
    try:
        while stream and stream.read(4096):
            pass
    except Exception:
        pass


def _release_lock() -> None:
    try:
        _GATEWAY_RESTART_LOCK.release()
    except RuntimeError:
        # The lock may already have been released by another path.
        pass


def _subprocess_became_gateway(proc: subprocess.Popen, hermes_home: Path) -> bool:
    """Return True when the restart subprocess has itself become the gateway.

    Single-container deployments have no service manager, so ``hermes gateway
    restart`` falls through to ``run_gateway()`` and the CLI process becomes
    the gateway itself: it binds the API-server port and never exits (#6730).
    The gateway writes ``gateway_state.json`` into the HERMES_HOME it was
    launched with (the same ``hermes_home`` we put on the subprocess env),
    recording its own PID under the ``pid`` key.  When that recorded PID
    equals the subprocess PID and the gateway self-reports as running, the
    timeout means "restart succeeded and this process IS the gateway", not
    "restart hung" — terminating it would SIGTERM the healthy replacement.

    Any unverifiable input fails closed (returns False), preserving the
    previous terminate-on-timeout behaviour for genuinely hung restarts.
    """
    try:
        proc_pid = int(proc.pid)
    except (AttributeError, TypeError, ValueError):
        return False
    try:
        runtime_status = json.loads(
            (hermes_home / _GATEWAY_RUNTIME_STATUS_FILE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(runtime_status, dict):
        return False
    if runtime_status.get("gateway_state") not in ("running", "starting"):
        return False
    try:
        return int(runtime_status.get("pid")) == proc_pid
    except (TypeError, ValueError):
        return False


def _gateway_restart_profile_context(profile: str | None = None) -> tuple[Path, str | None]:
    """Return the HERMES_HOME and CLI profile arg for a gateway restart."""
    if profile is None:
        raw_profile = str(get_active_profile_name() or "default").strip()
        active_home = Path(get_active_hermes_home())
    else:
        raw_profile = str(profile or "")
        if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile):
            raise ValueError(f"Invalid profile for gateway restart: {profile!r}")
        active_home = Path(get_hermes_home_for_profile(raw_profile))

    if (
        raw_profile == "default"
        and active_home.name == "default"
        and active_home.parent.name == "profiles"
    ):
        return active_home, None
    if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile) or _is_root_profile(raw_profile):
        return active_home, "default"
    return active_home, raw_profile


def restart_active_profile_gateway(
    *,
    profile: str | None = None,
    quick_timeout_seconds: float = 2.0,
    background_wait_seconds: float = 240.0,
) -> dict:
    """Run a non-blocking ``hermes gateway restart`` for the active profile.

    Returns a short status dict with these values:
    - completed: command finished quickly and succeeded.
    - in_progress: command did not finish within ``quick_timeout_seconds``.
    - failed: command finished quickly with non-zero exit status.
    - busy: restart already in progress from another caller.
    """
    if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "Restart already in progress. Please wait a moment and try again.",
        }

    try:
        active_home, cli_profile = _gateway_restart_profile_context(profile)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(active_home)
        hermes_cmd = _resolve_hermes_command()
        cmd = [hermes_cmd]
        if cli_profile is not None:
            cmd.extend(["--profile", cli_profile])
        cmd.extend(["gateway", "restart"])

        if cli_profile is None:
            logger.info(
                "Restarting gateway service via CLI command: %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                active_home,
            )
        else:
            logger.info(
                "Restarting gateway service via CLI command: %s --profile %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                cli_profile,
                active_home,
            )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            stdout, stderr = proc.communicate(timeout=quick_timeout_seconds)
            _release_lock()
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            if proc.returncode == 0:
                logger.info("Gateway service restarted successfully: %s", stdout)
                return {
                    "status": "completed",
                    "message": "Gateway service restarted successfully",
                    "detail": stdout or stderr,
                }

            logger.error("Gateway service restart failed with code %s: %s", proc.returncode, stderr)
            return {
                "status": "failed",
                "message": f"Restart failed: {stderr or stdout}",
                "detail": stdout or stderr,
                "returncode": proc.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.info(
                "Gateway restart is taking longer than %.1fs (likely draining in-flight runs);"
                " continuing in background",
                quick_timeout_seconds,
            )

            threading.Thread(target=_consume_stream, args=(proc.stdout,), daemon=True).start()
            threading.Thread(target=_consume_stream, args=(proc.stderr,), daemon=True).start()

            def _wait_and_release() -> None:
                try:
                    proc.wait(timeout=background_wait_seconds)
                except subprocess.TimeoutExpired:
                    if _subprocess_became_gateway(proc, active_home):
                        # Single-container image: no service manager, so the
                        # restart CLI became the gateway itself. Killing it
                        # would SIGTERM the healthy replacement and drop every
                        # active session/SSE stream (#6730). Treat the timeout
                        # as success: release the lock and leave it running.
                        logger.warning(
                            "Gateway restart process timed out after %.1fs but the "
                            "subprocess has become the gateway itself (PID %s owns "
                            "gateway_state.json); leaving it running.",
                            background_wait_seconds,
                            proc.pid,
                        )
                        return
                    logger.error(
                        "Gateway restart process timed out after %.1fs. Terminating process.",
                        background_wait_seconds,
                    )
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            try:
                                proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    "Gateway restart process refused to die even after SIGKILL.",
                                )
                    except Exception:
                        logger.exception("Failed to terminate timed out gateway restart process.")
                finally:
                    _release_lock()

            threading.Thread(target=_wait_and_release, daemon=True).start()
            return {
                "status": "in_progress",
                "message": "Gateway service restart initiated (in progress)",
            }
    except Exception as exc:
        _release_lock()
        logger.exception("Failed to run gateway restart command")
        return {
            "status": "failed",
            "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
        }
