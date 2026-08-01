from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.sandbox import sandbox_command, sanitized_environment


@unittest.skipUnless(
    os.environ.get("ARCTL_HOST_SANDBOX_TEST") == "1",
    "requires a host that can create a Codex sandbox",
)
class HostSandboxTests(unittest.TestCase):
    def test_reflection_profile_reads_both_arms_without_mutating_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            champion = root / "champion"
            scratch = root / "scratch"
            codex_home = root / "codex-home"
            for directory in (candidate, champion, scratch, codex_home):
                directory.mkdir()
            (candidate / "agent.py").write_text("candidate")
            (champion / "agent.py").write_text("champion")
            private = root / "evidence.private.json"
            private.write_text("private")
            result = scratch / "probe.json"
            script = """\
import json
import sys
from pathlib import Path

candidate, champion, private, result = map(Path, sys.argv[1:])
checks = {
    "candidate_read": (candidate / "agent.py").read_text() == "candidate",
    "champion_read": (champion / "agent.py").read_text() == "champion",
}
for name, path in (
    ("candidate_write_denied", candidate / "forbidden"),
    ("champion_write_denied", champion / "forbidden"),
):
    try:
        path.write_text("bad")
    except OSError:
        checks[name] = True
    else:
        checks[name] = False
try:
    private.read_text()
except OSError:
    checks["private_read_denied"] = True
else:
    checks["private_read_denied"] = False
result.write_text(json.dumps(checks))
"""
            command = sandbox_command(
                (
                    "python3",
                    "-c",
                    script,
                    str(candidate),
                    str(champion),
                    str(private),
                    str(result),
                ),
                cwd=candidate,
                read_paths=(candidate, champion),
                write_paths=(scratch,),
                profile="arctl-research",
            )
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=sanitized_environment(
                    codex_home=codex_home,
                    writable_home=scratch,
                ),
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(all(json.loads(result.read_text()).values()))

    def test_subject_profile_enforces_read_write_network_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            output = root / "output"
            codex_home = root / "codex-home"
            for directory in (worktree, output, codex_home):
                directory.mkdir()
            batch = root / "batch.json"
            private = root / "evaluator-secret.txt"
            batch.write_text("public")
            private.write_text("private")
            result = output / "probe.json"
            auth = Path.home() / ".codex" / "auth.json"
            script = """\
import json
import socket
import sys
from pathlib import Path

batch, private, auth, worktree, result = map(Path, sys.argv[1:])
checks = {"batch_read": batch.read_text() == "public"}
for name, path in (("private_denied", private), ("auth_denied", auth)):
    try:
        path.read_bytes()
    except OSError:
        checks[name] = True
    else:
        checks[name] = False
try:
    (worktree / "forbidden").write_text("bad")
except OSError:
    checks["worktree_write_denied"] = True
else:
    checks["worktree_write_denied"] = False
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.5)
except OSError:
    checks["network_denied"] = True
else:
    checks["network_denied"] = False
result.write_text(json.dumps(checks))
"""
            command = sandbox_command(
                (
                    "python3",
                    "-c",
                    script,
                    str(batch),
                    str(private),
                    str(auth),
                    str(worktree),
                    str(result),
                ),
                cwd=worktree,
                read_paths=(worktree, batch),
                write_paths=(output,),
                profile="arctl-subject",
            )
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=sanitized_environment(
                    codex_home=codex_home,
                    writable_home=output,
                ),
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            checks = json.loads(result.read_text())
            self.assertTrue(all(checks.values()), checks)

    def test_research_profile_protects_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            scratch = root / "scratch"
            codex_home = root / "codex-home"
            for directory in (worktree, scratch, codex_home):
                directory.mkdir()
            git_file = worktree / ".git"
            git_file.write_text("gitdir: protected")
            private = root / "calibration.private.json"
            private.write_text("private")
            result = scratch / "probe.json"
            script = """\
import json
import sys
from pathlib import Path

worktree, git_file, private, result = map(Path, sys.argv[1:])
checks = {}
try:
    git_file.write_text("tampered")
except OSError:
    checks["git_write_denied"] = True
else:
    checks["git_write_denied"] = False
try:
    private.read_text()
except OSError:
    checks["private_read_denied"] = True
else:
    checks["private_read_denied"] = False
(worktree / "candidate.py").write_text("changed")
checks["candidate_write_allowed"] = (worktree / "candidate.py").is_file()
result.write_text(json.dumps(checks))
"""
            command = sandbox_command(
                (
                    "python3",
                    "-c",
                    script,
                    str(worktree),
                    str(git_file),
                    str(private),
                    str(result),
                ),
                cwd=worktree,
                read_paths=(git_file,),
                write_paths=(worktree, scratch),
                profile="arctl-research",
            )
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=sanitized_environment(
                    codex_home=codex_home,
                    writable_home=scratch,
                ),
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(all(json.loads(result.read_text()).values()))
