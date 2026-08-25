from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

from arctl.errors import StateError
from arctl.manifest import EvaluatorManifest
from arctl.runner import _research_schema, _validate_codex_output_schema
from arctl.sandbox import (
    MAX_AGENT_PROMPT_BYTES,
    command_runtime_read_paths,
    networked_dependency_command,
    research_command,
    sandbox_command,
    sanitized_environment,
)

from .test_manifest import valid_manifest


def write_output_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"type":"object","properties":{},"required":[],'
        '"additionalProperties":false}',
        encoding="utf-8",
    )
    return path


class SandboxCommandTests(unittest.TestCase):
    def test_networked_dependencies_inherit_network_without_mounting_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            environment = root / "venv"
            command = networked_dependency_command(
                ("/bin/true", "sync"),
                cwd=runtime,
                write_paths=(runtime, environment),
            )

        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertNotIn("--unshare-net", command)
        self.assertFalse(
            any(
                command[index : index + 3] == ("--ro-bind", "/", "/")
                for index in range(len(command) - 2)
            )
        )
        self.assertIn(str(runtime.resolve()), command)
        self.assertIn(str(environment.resolve()), command)
        self.assertEqual(
            command[-3:], ("--", "/arctl-tools/true", "sync")
        )

    def test_runtime_paths_include_the_active_virtual_environment(self) -> None:
        paths = command_runtime_read_paths((sys.executable, "-V"))

        self.assertIn(Path(sys.executable).resolve(), paths)
        self.assertIn(Path(sys.prefix).resolve(), paths)
        self.assertIn(Path(sys.base_prefix).resolve(), paths)

    def test_runtime_paths_include_a_pyenv_shim_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".pyenv"
            shim = root / "shims" / "python3"
            dispatcher = root / "libexec" / "pyenv"
            shim.parent.mkdir(parents=True)
            dispatcher.parent.mkdir(parents=True)
            shim.write_text("#!/bin/sh\n")
            dispatcher.write_text("#!/bin/sh\n")

            self.assertIn(root.resolve(), command_runtime_read_paths((str(shim),)))

    def test_research_schema_matches_strict_codex_output_contract(self) -> None:
        manifest = EvaluatorManifest.from_mapping(valid_manifest())

        schema = _research_schema(manifest)

        self.assertEqual(
            schema["properties"]["schema_version"],
            {"type": "integer", "const": 2},
        )
        telemetry = schema["properties"]["expected_telemetry"]
        self.assertIs(telemetry["additionalProperties"], False)
        self.assertEqual(telemetry["required"], list(manifest.public_telemetry))
        self.assertEqual(set(telemetry["properties"]), set(manifest.public_telemetry))
        self.assertTrue(
            all(
                property_schema["type"] == ["string", "null"]
                for property_schema in telemetry["properties"].values()
            )
        )

    def test_research_schema_preflight_rejects_open_or_untyped_nodes(self) -> None:
        with self.assertRaisesRegex(StateError, "lacks a type"):
            _validate_codex_output_schema(
                {
                    "type": "object",
                    "properties": {"version": {"const": 1}},
                    "required": ["version"],
                    "additionalProperties": False,
                }
            )

    def test_reflection_command_keeps_the_candidate_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            champion = root / "champion"
            scratch = root / "scratch"
            for path in (candidate, champion, scratch):
                path.mkdir()
            command = research_command(
                worktree=candidate,
                scratch=scratch,
                output_schema=write_output_schema(scratch / "schema.json"),
                prompt="reflect",
                read_paths=(champion,),
                writable_worktree=False,
            )
            filesystem = next(
                command[index + 1]
                for index, item in enumerate(command[:-1])
                if item == "--config" and "filesystem=" in command[index + 1]
            )
            self.assertIn(f'"{candidate.resolve()}"="read"', filesystem)
            self.assertIn(f'"{champion.resolve()}"="read"', filesystem)
            self.assertIn(f'"{scratch.resolve()}"="write"', filesystem)
            self.assertNotIn(f'"{candidate.resolve()}"="write"', filesystem)
        with self.assertRaisesRegex(StateError, "not strict"):
            _validate_codex_output_schema(
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": True,
                }
            )

    def test_subject_profile_uses_exact_paths_and_disables_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "candidate"
            batch = root / "batch.json"
            output = root / "output"
            command = sandbox_command(
                ("python3", "subject.py", str(batch), str(output / "result.json")),
                cwd=worktree,
                read_paths=(worktree, batch),
                write_paths=(output,),
                profile="arctl-subject",
            )
            joined = "\n".join(command)
            self.assertIn('":minimal"="read"', joined)
            self.assertIn('":root"="deny"', joined)
            self.assertIn(f'"{worktree}"="read"', joined)
            self.assertIn(f'"{batch}"="read"', joined)
            self.assertIn(f'"{output}"="write"', joined)
            self.assertIn("permissions.arctl-subject.network.enabled=false", joined)
            self.assertNotIn("--sandbox", command)

    def test_refuses_conflicting_read_and_write_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with self.assertRaisesRegex(StateError, "both read-only and writable"):
                sandbox_command(
                    ("true",),
                    cwd=path,
                    read_paths=(path,),
                    write_paths=(path,),
                    profile="arctl-test",
                )

    def test_research_is_ephemeral_noninteractive_and_plugin_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "approved-runtime"
            command = research_command(
                worktree=root / "worktree",
                scratch=root / "scratch",
                output_schema=write_output_schema(root / "schema.json"),
                prompt="Make one improvement.",
                read_paths=(runtime,),
            )
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--strict-config", command)
            self.assertIn("approval_policy=\"never\"", command)
            self.assertIn("multi_agent", command)
            self.assertIn("permissions.arctl-research.network.enabled=false", command)
            joined = "\n".join(command)
            self.assertIn(f'"{(root / "worktree" / ".git").resolve()}"="read"', joined)
            self.assertIn(f'"{runtime.resolve()}"="read"', joined)
            self.assertIn(f'"{(root / "worktree").resolve()}"="write"', joined)
            self.assertEqual(command[-1], "-")
            self.assertEqual(
                (root / "prompt.public.txt").read_text(encoding="utf-8"),
                "Make one improvement.",
            )

    def test_setup_can_explicitly_enable_network_and_live_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = research_command(
                worktree=root / "worktree",
                scratch=root / "scratch",
                output_schema=write_output_schema(root / "schema.json"),
                prompt="inspect public documentation",
                writable_worktree=False,
                network_enabled=True,
            )
            joined = "\n".join(command)
            self.assertIn("permissions.arctl-research.network.enabled=true", joined)
            self.assertIn('web_search="live"', joined)

    def test_research_rejects_a_prompt_over_the_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(StateError, "global limit"):
                research_command(
                    worktree=root / "worktree",
                    scratch=root / "scratch",
                    output_schema=root / "schema.json",
                    prompt="x" * (MAX_AGENT_PROMPT_BYTES + 1),
                )

    def test_research_rejects_invalid_schema_before_saving_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(StateError, "root must be an object"):
                research_command(
                    worktree=root / "worktree",
                    scratch=root / "scratch",
                    output_schema=schema,
                    prompt="must not be saved",
                )

            self.assertFalse((root / "prompt.public.txt").exists())

    def test_strategy_selects_explicit_model_effort_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            scratch = root / "scratch"
            worktree.mkdir()
            scratch.mkdir()
            schema = scratch / "schema.json"
            write_output_schema(schema)
            with mock.patch("arctl.sandbox.shutil.which", return_value="/usr/bin/codex"):
                command = research_command(
                    worktree=worktree,
                    scratch=scratch,
                    output_schema=schema,
                    prompt="orient",
                    output_name="strategy.public.json",
                    model="gpt-5.6-sol",
                    reasoning_effort="high",
                    writable_worktree=False,
                    read_worktree=False,
                )
            self.assertIn("gpt-5.6-sol", command)
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertIn(str((scratch / "strategy.public.json").resolve()), command)
            joined = "\n".join(command)
            self.assertNotIn(f'"{worktree.resolve()}"="read"', joined)
            self.assertNotIn(f'"{worktree.resolve()}"="write"', joined)

    def test_environment_does_not_inherit_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = sanitized_environment(
                codex_home=root / "codex-home",
                writable_home=root / "output",
            )
            self.assertEqual(
                set(environment),
                {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "PYTHONPYCACHEPREFIX"}
                | ({"LANG"} if "LANG" in environment else set())
                | ({"LC_ALL"} if "LC_ALL" in environment else set())
                | ({"TZ"} if "TZ" in environment else set()),
            )
            self.assertNotIn("OPENAI_API_KEY", environment)

    def test_environment_routes_compiled_python_outside_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            writable_home = root / "output"
            worktree.mkdir()
            writable_home.mkdir()
            source = worktree / "policy.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")

            completed = subprocess.run(
                (sys.executable, "-m", "py_compile", source.name),
                cwd=worktree,
                env=sanitized_environment(
                    codex_home=root / "codex-home",
                    writable_home=writable_home,
                ),
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(list(worktree.rglob("*.pyc")), [])
            self.assertNotEqual(list((writable_home / "pycache").rglob("*.pyc")), [])
