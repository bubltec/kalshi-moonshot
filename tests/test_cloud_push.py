"""scripts/cloud_push.sh --build: a build the host cannot pull stops before the lane file is sent.
2026-11-16: the host's pull failed on a missing recorder tag after the new lane file was already in place; the old image refused its new field and every lane load raised an error for 100 seconds."""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

AWS = r"""#!/bin/bash
echo "aws $*" >> "$STUB_LOG"
case "$*" in
  *"ssm delete-parameter"*) exit 254 ;;
  *"ssm send-command"*)
    case "$*" in *moonshot-pull-check*) echo cmd-check ;; *) echo cmd-sync ;; esac ;;
  *"ssm get-command-invocation"*)
    case "$*" in
      *cmd-check*) if [ "${STUB_PULL:-ok}" = ok ]; then echo "Success"; else printf 'Failed\tmanifest unknown\n'; fi ;;
      *) echo "Success" ;;
    esac ;;
  *"sts get-caller-identity"*) echo 123 ;;
  *"ecr get-login-password"*) echo pw ;;
  *"cloudformation describe-stacks"*) echo i-123 ;;
esac
exit 0
"""
DOCKER = '#!/bin/bash\ncat > /dev/null 2>&1 < /dev/stdin || true\necho "docker $*" >> "$STUB_LOG"\n'


@pytest.fixture
def push(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "cloud_push.sh", tmp_path / "scripts" / "cloud_push.sh")
    (tmp_path / ".env").write_text("A=1\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "strategies.toml").write_text("[lane.x]\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("aws", AWS), ("docker", DOCKER), ("sleep", "#!/bin/bash\nexit 0\n")):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    log = tmp_path / "calls.log"

    def run(*args, **env):
        log.write_text("")
        e = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(log), **env}
        e.pop("AWS_PROFILE", None)
        done = subprocess.run(["bash", str(tmp_path / "scripts" / "cloud_push.sh"), "--ignore-page-changes", *args], env=e, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return done, log.read_text().splitlines()

    return run


def first(calls, text):
    return next((i for i, c in enumerate(calls) if text in c), None)


def test_integration_a_build_pulls_on_the_host_before_the_lane_file_is_sent(push):
    done, calls = push("--build")
    assert done.returncode == 0, done.stderr
    image, check, lane, sync = first(calls, "docker push"), first(calls, "moonshot-pull-check"), first(calls, "put-parameter"), first(calls, "moonshot-sync")
    assert None not in (image, check, lane, sync) and image < check < lane < sync


def test_integration_a_failed_pull_on_the_host_sends_no_lane_file(push):
    done, calls = push("--build", STUB_PULL="fail")
    assert done.returncode != 0 and "NOT sent" in done.stderr
    assert first(calls, "put-parameter") is None and first(calls, "moonshot-sync") is None


def test_integration_every_build_pushes_the_recorder_tag_and_needs_no_flag(push):
    done, calls = push("--build")
    assert done.returncode == 0, done.stderr
    assert first(calls, "describe-images") is None  # a missing recorder tag is no obstacle: the push makes it
    assert first(calls, "kalshi-moonshot-bot:recorder") is not None and first(calls, "moonshot-sync") is not None


def test_integration_a_config_only_push_has_no_pull_check_and_no_restart(push):
    done, calls = push()
    assert done.returncode == 0, done.stderr
    assert first(calls, "docker") is None and first(calls, "moonshot-pull-check") is None
    assert first(calls, "put-parameter") < first(calls, "moonshot-sync")
