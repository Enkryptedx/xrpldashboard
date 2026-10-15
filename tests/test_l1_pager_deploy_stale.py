"""l1_pager: deploy-staleness check (merged but never pulled).

THE WOUND (2026-10-15). The walker-pause-hardening fix `84616f3` was merged,
pulled to the Mac, and deployed to Render. But `l1_pager` runs from a
SEPARATE clone on Lenovo that nobody pulled. It sat at `86c899c`, dated
2026-09-30, for **15 days and 191 commits** — so it kept paging
`walker_failing:supply_escrow_walker` out of pre-fix code while the fix was
"live" everywhere the owner looked, and the owner reasonably concluded the
fix was broken.

Nothing alarmed on it. `plist_drift_canary` watches systemd/launchd unit
FILES, not the checkout's commit, and a stale pager cannot notice that it is
itself stale. The only signal was a human spotting a page that should have
stopped.

DESIGN CHOICES PINNED HERE, because each is load-bearing:

1. **Age, not commit count.** Count needs the incoming objects locally, i.e.
   a fetch. And "behind by N" fires on every merge until someone deploys,
   which is pure noise when deploys are manual. HEAD commit age is stateless
   and self-calibrating: a same-day deploy lag reads as hours and stays
   quiet; 15 days cannot hide.
2. **`ls-remote`, never `fetch`.** A monitor must not mutate the thing it
   monitors. `ls-remote` asks the remote for the ref and writes nothing to
   the local repo; a fetch inside a 20-minute timer would quietly rewrite
   the clone's object store forever.
3. **Both conditions required.** Differing SHAs alone fires on every merge;
   age alone fires whenever no new commits exist. Only together do they mean
   "someone forgot to deploy".
4. **Network failure skips, local git failure pages.** An unreachable remote
   is weather (matching plist_drift_canary's SSH convention). A broken local
   git is misconfiguration, and "staleness unprovable" must not read as
   "up to date".

Hermetic: pure predicates and a fake git runner. No git executed, no
network, no DB, main() never called.
"""
from __future__ import annotations

import datetime as dt
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import l1_pager as p  # noqa: E402

NOW = dt.datetime(2026, 10, 15, 21, 0, tzinfo=dt.timezone.utc)
LOCAL = "86c899c1111111111111111111111111111111"
REMOTE = "e77976f2222222222222222222222222222222"


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def runner(stdout="", returncode=0, raises=None, record=None):
    def _run(argv, **kwargs):
        if record is not None:
            record.append(argv)
        if raises is not None:
            raise raises
        return FakeProc(stdout, returncode)
    return _run


def git_stub(head=LOCAL, epoch=None, remote=REMOTE,
             local_err=None, remote_err=None):
    """Stand in for _git_capture, dispatching on the git subcommand."""
    if epoch is None:
        epoch = int(NOW.timestamp()) - 15 * 86400
    def _cap(args, *a, **k):
        if args[0] == "rev-parse":
            return ("" if local_err else head), local_err
        if args[0] == "log":
            return ("" if local_err else str(epoch)), local_err
        if args[0] == "ls-remote":
            return ("" if remote_err else f"{remote}\trefs/heads/main"), remote_err
        raise AssertionError("unexpected git call: " + " ".join(args))
    return _cap


# ── 1. the predicate ────────────────────────────────────────────────
class TestIsDeployStale:
    def test_the_real_incident_fires(self):
        assert p._is_deploy_stale(LOCAL, REMOTE, 15 * 86400) is True

    def test_same_day_deploy_lag_stays_quiet(self):
        """Deploys are manual; a few hours behind is normal, not an alarm."""
        assert p._is_deploy_stale(LOCAL, REMOTE, 2 * 3600) is False

    def test_up_to_date_never_fires_however_old(self):
        """No new commits means HEAD ages forever. That is not staleness."""
        assert p._is_deploy_stale(LOCAL, LOCAL, 365 * 86400) is False

    def test_exactly_at_threshold_does_not_fire(self):
        assert p._is_deploy_stale(LOCAL, REMOTE, p.DEPLOY_STALE_SECONDS) is False

    def test_one_second_past_threshold_fires(self):
        assert p._is_deploy_stale(
            LOCAL, REMOTE, p.DEPLOY_STALE_SECONDS + 1) is True

    def test_missing_shas_never_fire(self):
        assert p._is_deploy_stale("", REMOTE, 10 ** 7) is False
        assert p._is_deploy_stale(LOCAL, "", 10 ** 7) is False

    def test_unknown_age_never_fires(self):
        assert p._is_deploy_stale(LOCAL, REMOTE, None) is False

    def test_default_threshold_is_24h(self):
        assert p.DEPLOY_STALE_SECONDS == 24 * 3600


# ── 2. git invocation discipline ────────────────────────────────────
class TestGitCapture:
    def test_success_returns_stripped_stdout(self):
        out, err = p._git_capture(["rev-parse", "HEAD"],
                                  runner=runner(stdout=" abc123 \n"))
        assert (out, err) == ("abc123", None)

    def test_nonzero_exit_is_an_error(self):
        out, err = p._git_capture(["rev-parse", "HEAD"],
                                  runner=runner(returncode=128))
        assert out == "" and err == "rc128"

    def test_exception_never_propagates(self):
        out, err = p._git_capture(["rev-parse", "HEAD"],
                                  runner=runner(raises=OSError("boom")))
        assert out == "" and err == "OSError"

    def test_scoped_with_dash_C_not_cwd(self):
        rec = []
        p._git_capture(["rev-parse", "HEAD"], runner=runner(record=rec))
        assert rec[0][:3] == ["git", "-C", str(p.DEPLOY_REPO_DIR)]

    def test_check_never_fetches_or_pulls(self):
        """A monitor must not mutate the repo it monitors. Asserted against
        the SOURCE, not against a call we chose ourselves — otherwise the
        test only proves the argument we passed in.
        """
        src = inspect.getsource(p.check_deploy_stale)
        assert "fetch" not in src
        assert "pull" not in src.replace("pull --ff-only", "")

    def test_only_read_only_git_subcommands_are_used(self, monkeypatch):
        """Enumerate every git call the check actually makes."""
        seen = []

        def _cap(args, *a, **k):
            seen.append(args[0])
            if args[0] == "rev-parse":
                return LOCAL, None
            if args[0] == "log":
                return str(int(NOW.timestamp()) - 15 * 86400), None
            return f"{REMOTE}\trefs/heads/main", None

        monkeypatch.setattr(p, "_git_capture", _cap)
        p.check_deploy_stale(NOW)
        assert set(seen) <= {"rev-parse", "log", "ls-remote"}, seen


# ── 3. the check ────────────────────────────────────────────────────
class TestCheckDeployStale:
    def test_stale_checkout_produces_one_alert(self, monkeypatch):
        monkeypatch.setattr(p, "_git_capture", git_stub())
        alerts = p.check_deploy_stale(NOW)
        assert len(alerts) == 1
        a = alerts[0]
        assert a["id"] == "deploy_stale:HEAD"
        assert a["local_sha"] == LOCAL[:12]
        assert a["remote_sha"] == REMOTE[:12]
        assert a["head_age_s"] == 15 * 86400
        assert a["branch"] == "main"

    def test_up_to_date_is_silent(self, monkeypatch):
        monkeypatch.setattr(p, "_git_capture", git_stub(remote=LOCAL))
        assert p.check_deploy_stale(NOW) == []

    def test_recent_head_is_silent(self, monkeypatch):
        monkeypatch.setattr(p, "_git_capture", git_stub(
            epoch=int(NOW.timestamp()) - 3600))
        assert p.check_deploy_stale(NOW) == []

    def test_unreachable_remote_is_a_skip(self, monkeypatch):
        """Weather, not a finding — matches the SSH convention."""
        monkeypatch.setattr(p, "_git_capture",
                            git_stub(remote_err="TimeoutExpired"))
        assert p.check_deploy_stale(NOW) == []

    def test_broken_local_git_pages(self, monkeypatch):
        """Misconfiguration. 'Unprovable' must never read as 'up to date'."""
        monkeypatch.setattr(p, "_git_capture", git_stub(local_err="rc128"))
        alerts = p.check_deploy_stale(NOW)
        assert len(alerts) == 1
        assert alerts[0]["id"] == "deploy_stale:local_git_unavailable"
        assert alerts[0]["error"] == "rc128"

    def test_unparseable_commit_date_is_silent(self, monkeypatch):
        monkeypatch.setattr(p, "_git_capture", git_stub(epoch="not-a-number"))
        assert p.check_deploy_stale(NOW) == []


# ── 4. the message ──────────────────────────────────────────────────
class TestFormatAlert:
    def test_stale_message_names_both_shas_and_the_remedy(self):
        out = p.format_alert({
            "id": "deploy_stale:HEAD",
            "repo": "/home/charlie/xrpldashboard",
            "branch": "main",
            "local_sha": LOCAL[:12],
            "remote_sha": REMOTE[:12],
            "head_age_s": 15 * 86400,
            "threshold_s": 86400,
        })
        assert "DEPLOY stale" in out
        assert LOCAL[:12] in out and REMOTE[:12] in out
        assert "15.0d" in out
        assert "pull --ff-only origin main" in out

    def test_broken_git_message_is_distinct(self):
        out = p.format_alert({
            "id": "deploy_stale:local_git_unavailable",
            "repo": "/home/charlie/xrpldashboard",
            "error": "rc128",
        })
        assert "DEPLOY check broken" in out
        assert "unprovable" in out


# ── 5. wiring ───────────────────────────────────────────────────────
class TestWiring:
    def test_registered_in_gather_alerts(self):
        assert "check_deploy_stale" in inspect.getsource(p.gather_alerts)

    def test_repo_dir_derived_from_module_location(self):
        """Hardcoding /home/charlie would break the Mac and any future host."""
        assert p.DEPLOY_REPO_DIR.name != "tools"
        assert (p.DEPLOY_REPO_DIR / "tools").is_dir()

    def test_branch_is_configurable(self):
        assert p.DEPLOY_BRANCH == "main"
