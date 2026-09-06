from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from struct import pack

import pytest
from heavenbase.utils import sha256hash
from PIL import Image
from typer.testing import CliRunner

import paradev.build._fs as build_fs
import paradev.build.artifacts as build_artifacts
import paradev.sdk.project as project_sdk
from paradev.build import publication as build_publication
from paradev.build import (
    Artifact,
    BuildContext,
    BuildRegistry,
    BuildResult,
    Collection,
    CollectionSourceBundle,
    CollectionSourceFamily,
    Diagnostic,
    FamilyCompileResult,
    JsonViewWriter,
    Module,
    PDXTextWriter,
    RoutedSourceFamily,
    SimpleSourceFamily,
    Slot,
    SourceRoute,
    plan_build,
)
from paradev.cli import build_app
from paradev.config import CM_PARADEV, DEFAULT_CONFIG
from paradev.games import registry_for_profile
from paradev.pdx import PDXBlock
from paradev.sdk import Project


def test_default_build_config_defines_parallelism() -> None:
    assert DEFAULT_CONFIG["paradev"]["build"]["parallelism"] == 1


def test_plan_build_applies_registered_artifact_postprocessors() -> None:
    class Family:
        family = "demo"

        def emit(
            self,
            _ctx: BuildContext,
            modules: tuple[Module, ...],
            _collections: tuple[Collection, ...],
        ) -> tuple[Artifact, ...]:
            return (
                Artifact(
                    path="common/original.txt",
                    artifact_type="pdx",
                    owner=f"module:{modules[0].module_id}",
                    payload="original",
                ),
            )

    class Postprocessor:
        postprocessor_id = "rename"

        def process(
            self,
            ctx: BuildContext,
            artifacts: tuple[Artifact, ...],
        ) -> tuple[Artifact, ...]:
            assert ctx.metadata["emit_artifacts"] is True
            return (replace(artifacts[0], path="common/reconciled.txt"),)

    registry = BuildRegistry().add(Family()).add(PDXTextWriter()).add(Postprocessor())

    result = plan_build(
        "postprocessed",
        registry,
        metadata={"emit_artifacts": True},
        modules=(Module(module_id="demo/one", family="demo", root="."),),
    )

    assert [str(artifact.path) for artifact in result.artifacts] == ["common/reconciled.txt"]
    assert result.diagnostics == ()


def test_plan_build_uses_one_combined_family_compile_hook() -> None:
    class Family:
        family = "combined"

        def compile(
            self,
            _ctx: BuildContext,
            modules: tuple[Module, ...],
            _collections: tuple[Collection, ...],
        ) -> FamilyCompileResult:
            return FamilyCompileResult(
                artifacts=(
                    Artifact(
                        path="common/combined.txt",
                        artifact_type="pdx",
                        owner=f"module:{modules[0].module_id}",
                        payload="combined",
                    ),
                ),
                diagnostics=(
                    Diagnostic(
                        code="combined.notice",
                        message="Combined preparation ran once.",
                        severity="warning",
                    ),
                ),
            )

        def check(self, *_args: object) -> tuple[Diagnostic, ...]:
            raise AssertionError("combined compile must replace check")

        def emit(self, *_args: object) -> tuple[Artifact, ...]:
            raise AssertionError("combined compile must replace emit")

    module = Module(module_id="combined/sample", family="combined", root=".")
    result = plan_build(
        "combined",
        BuildRegistry().add(Family()).add(PDXTextWriter()),
        modules=(module,),
    )

    assert [str(artifact.path) for artifact in result.artifacts] == ["common/combined.txt"]
    assert result.diagnostics == (
        Diagnostic(
            code="combined.notice",
            message="Combined preparation ran once.",
            severity="warning",
            family="combined",
        ),
    )


def test_plan_build_reports_invalid_combined_compile_results() -> None:
    class Family:
        family = "combined"

        def compile(self, *_args: object) -> object:
            return ()

    module = Module(module_id="combined/sample", family="combined", root=".")
    result = plan_build(
        "combined",
        BuildRegistry().add(Family()),
        modules=(module,),
    )

    assert result.artifacts == ()
    assert result.diagnostics == (
        Diagnostic(
            code="family.compile_failed",
            message=("Build family 'combined' compile failed: ValueError: Build " "family 'combined' compile must return FamilyCompileResult."),
            severity="error",
            family="combined",
        ),
    )


def test_plan_build_omits_inactive_modules_and_their_diagnostics() -> None:
    class Family:
        family = "demo"

        def emit(
            self,
            _ctx: BuildContext,
            modules: tuple[Module, ...],
            _collections: tuple[Collection, ...],
        ) -> tuple[Artifact, ...]:
            return tuple(
                Artifact(
                    path=f"common/{module.module_id.rsplit('/', 1)[-1]}.txt",
                    artifact_type="pdx",
                    owner=f"module:{module.module_id}",
                    payload=module.module_id,
                )
                for module in modules
            )

    inactive = Module(
        module_id="demo/inactive",
        family="demo",
        root=".",
        metadata={"inactive": True},
    )
    active = Module(module_id="demo/active", family="demo", root=".")
    result = plan_build(
        "inactive-demo",
        BuildRegistry().add(Family()).add(PDXTextWriter()),
        modules=(inactive, active),
        diagnostics=(
            Diagnostic(
                code="source.invalid",
                message="Disabled draft is incomplete.",
                module_id=inactive.module_id,
            ),
        ),
    )

    assert [module.module_id for module in result.modules] == ["demo/active"]
    assert [str(artifact.path) for artifact in result.artifacts] == ["common/active.txt"]
    assert result.diagnostics == ()


def test_plan_build_reports_postprocessor_failures_as_blocking_diagnostics() -> None:
    class Postprocessor:
        postprocessor_id = "broken"

        def process(
            self,
            _ctx: BuildContext,
            _artifacts: tuple[Artifact, ...],
        ) -> tuple[Artifact, ...]:
            raise RuntimeError("cannot reconcile")

    result = plan_build(
        "postprocessed",
        BuildRegistry().add(Postprocessor()),
        metadata={"emit_artifacts": True},
    )

    assert result.blocked is True
    assert result.diagnostics[0].code == "build.postprocessor_failed"
    assert "broken" in result.diagnostics[0].message


def test_project_build_reports_progress_events_for_compiler_phases(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "progress_mod", project_id="progress_mod", title="Progress Mod")
    events: list[dict[str, object]] = []

    result = project.build(emit_artifacts=True, emit_manifests=True, progress=events.append)

    assert result.dry_run is False
    phases = [event["phase"] for event in events]
    assert phases[0] == "discover_modules"
    assert "discover_collections" in phases
    assert "basic_copy" in phases
    assert "entity_compile" in phases
    assert "artifact_generation" in phases
    assert "validating_publication" in phases
    assert "post_processing" in phases
    assert phases.index("artifact_generation") < phases.index("validating_publication")
    validation = next(event for event in events if event["phase"] == "validating_publication")
    assert validation == {
        "schema": "paradev.build.progress.v1",
        "phase": "validating_publication",
        "percent": 75,
        "label": "Validating publication",
        "detail": f"Checking {len(result.artifacts)} planned artifacts against existing generated output.",
        "total": len(result.artifacts),
        "counts": {"artifacts": len(result.artifacts)},
    }
    assert phases[-1] == "complete"
    assert events[-1]["percent"] == 100
    assert all("percent" in event for event in events)


def test_project_build_blocked_emission_returns_dry_result_and_writes_manifests(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(FailingCheckFamily())

    result = project.build(registry=registry, emit_artifacts=True, emit_manifests=True)

    assert result.blocked is True
    assert result.dry_run is True
    assert not (project.output_root / "common/national_focus/GER_sample.txt").exists()
    diagnostics = json.loads((project.build_root / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["diagnostics"][0]["code"] == "family.check_failed"


def test_project_build_full_rebuild_retries_transient_output_delete_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "generated"
    root.mkdir()
    (root / "stale.txt").write_text("old", encoding="utf-8")
    calls = 0
    original_delete_dir = project_sdk.delete_dir

    def flaky_delete_dir(path: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(66, "Directory not empty", str(path / "gfx"))
        return original_delete_dir(path)

    monkeypatch.setattr(project_sdk, "delete_dir", flaky_delete_dir)
    monkeypatch.setattr(project_sdk.time, "sleep", lambda _seconds: None)

    assert project_sdk._delete_generated_root(root) is True
    assert calls == 2
    assert not root.exists()


def test_project_build_full_rebuild_preserves_registered_hoi4_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    project = Project.create(tmp_path / "stable-mod", project_id="stable_mod", title="Stable Mod")
    project.build(emit_artifacts=True)
    output_identity = project.output_root.stat().st_ino
    stale_file = project.output_root / "stale.txt"
    stale_file.write_text("stale", encoding="utf-8")

    project.build(emit_artifacts=True, full_rebuild=True)

    assert project.output_root.stat().st_ino == output_identity
    assert not stale_file.exists()
    assert (project.output_root / "descriptor.mod").is_file()
    assert (mod_root / "stable_mod.mod").is_file()
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["identity"]["roots"]["output"]["full_clean_owned"] is True
    assert ledger["identity"]["roots"]["build"]["full_clean_owned"] is True


def test_project_full_rebuild_refuses_unowned_nonempty_roots_before_clearing_either(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    output_file = project.output_root / "user-output.txt"
    build_file = project.build_root / "user-build.txt"
    output_file.parent.mkdir(parents=True)
    build_file.parent.mkdir(parents=True)
    output_file.write_text("output", encoding="utf-8")
    build_file.write_text("build", encoding="utf-8")

    with pytest.raises(ValueError, match="not owned by this project"):
        project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    assert output_file.read_text(encoding="utf-8") == "output"
    assert build_file.read_text(encoding="utf-8") == "build"
    assert not (project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).exists()


def test_full_rebuild_uses_complete_cached_ledger_without_claiming_untracked_files(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    output_file = project.output_root / "notes/user-output.txt"
    build_file = project.build_root / "notes/user-build.txt"
    output_file.parent.mkdir(parents=True)
    build_file.parent.mkdir(parents=True)
    output_file.write_text("output", encoding="utf-8")
    build_file.write_text("build", encoding="utf-8")

    project.build(emit_artifacts=True, emit_manifests=True)
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["identity"]["roots"]["output"]["full_clean_owned"] is False
    assert ledger["identity"]["roots"]["build"]["full_clean_owned"] is False

    result = project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    assert result.blocked is False
    assert output_file.read_text(encoding="utf-8") == "output"
    assert build_file.read_text(encoding="utf-8") == "build"
    assert (project.output_root / "common/national_focus/GER_sample.txt").is_file()
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["identity"]["roots"]["output"]["full_clean_owned"] is False
    assert ledger["identity"]["roots"]["build"]["full_clean_owned"] is False


def test_tracked_full_clean_keeps_complete_ledger_until_all_deletions_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_file = project.output_root / "notes/user-output.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("output", encoding="utf-8")
    project.build(emit_artifacts=True, emit_manifests=True)
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    original_delete = build_publication._delete_tracked_artifact
    calls = 0

    def fail_after_first_delete(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated tracked-clean interruption")
        original_delete(*args, **kwargs)

    monkeypatch.setattr(build_publication, "_delete_tracked_artifact", fail_after_first_delete)
    with pytest.raises(OSError, match="tracked-clean interruption"):
        project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    interrupted_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert interrupted_ledger["state"] == "complete"
    assert user_file.read_text(encoding="utf-8") == "output"

    monkeypatch.setattr(build_publication, "_delete_tracked_artifact", original_delete)
    result = project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    assert result.blocked is False
    assert user_file.read_text(encoding="utf-8") == "output"


def test_cached_build_revalidates_empty_root_before_claiming_full_clean_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_file = project.output_root / "notes/user-data.txt"
    original_claim = build_publication.claim_publication_root
    injected = False

    def inject_user_file_before_claim(
        root: build_fs.AnchoredDirectory,
        *,
        root_name: str,
        project_id: str,
        project_root: Path,
        full_clean_owned: bool,
        require_empty_for_full_clean: bool,
    ) -> bool:
        nonlocal injected
        if root_name == "output" and not injected:
            root.write_bytes("notes/user-data.txt", b"USER DATA\n", replace=False)
            injected = True
        return original_claim(
            root,
            root_name=root_name,
            project_id=project_id,
            project_root=project_root,
            full_clean_owned=full_clean_owned,
            require_empty_for_full_clean=require_empty_for_full_clean,
        )

    monkeypatch.setattr(build_publication, "claim_publication_root", inject_user_file_before_claim)

    project.build(emit_artifacts=True, emit_manifests=True)

    output_marker = json.loads((project.output_root / build_publication.PUBLICATION_ROOT_NAME).read_text(encoding="utf-8"))
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert injected is True
    assert output_marker["full_clean_owned"] is False
    assert ledger["identity"]["roots"]["output"]["full_clean_owned"] is False

    result = project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    assert result.blocked is False
    assert user_file.read_bytes() == b"USER DATA\n"


def test_cached_build_does_not_treat_complete_legacy_plan_manifests_as_emitted_ownership(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_manifests=True)
    (project.build_root / build_publication.PUBLICATION_ROOT_NAME).unlink()
    user_file = project.output_root / "common/national_focus/GER_sample.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("USER DATA\n", encoding="utf-8")

    with pytest.raises(ValueError, match="untracked existing artifact path"):
        project.build(emit_artifacts=True, emit_manifests=True)

    assert user_file.read_text(encoding="utf-8") == "USER DATA\n"
    assert not (project.output_root / build_publication.PUBLICATION_ROOT_NAME).exists()


def test_replacing_an_owned_output_root_invalidates_full_clean_ownership(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    detached = tmp_path / "detached-output"
    project.output_root.rename(detached)
    user_file = project.output_root / "user-owned.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("safe", encoding="utf-8")

    with pytest.raises(ValueError, match="not owned by this project"):
        project.build(emit_artifacts=True, full_rebuild=True)

    assert user_file.read_text(encoding="utf-8") == "safe"
    assert (detached / "common/national_focus/GER_sample.txt").is_file()


def test_legacy_publication_ledger_cannot_authorize_full_clean(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    output_file = project.output_root / "user-output.txt"
    build_file = project.build_root / "user-build.txt"
    output_file.parent.mkdir(parents=True)
    build_file.parent.mkdir(parents=True)
    output_file.write_text("output", encoding="utf-8")
    build_file.write_text("build", encoding="utf-8")
    (project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).write_text(
        json.dumps({"schema": "paradev.build.emitted-artifacts.v1", "artifacts": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not owned by this project"):
        project.build(emit_artifacts=True, full_rebuild=True)

    assert output_file.read_text(encoding="utf-8") == "output"
    assert build_file.read_text(encoding="utf-8") == "build"


@pytest.mark.parametrize(
    ("output_root", "build_root", "message"),
    [
        ("/", ".paradev/.cache/build", "cannot be a filesystem root"),
        (".", ".paradev/.cache/build", "cannot contain the project root"),
        ("build/shared", "build/shared", "must be disjoint"),
        ("build/shared", "build/shared/cache", "must be disjoint"),
        ("src/generated", ".paradev/.cache/build", "cannot overlap source root"),
    ],
)
def test_project_build_rejects_unsafe_generated_root_layouts_before_mutation(
    tmp_path: Path,
    output_root: str,
    build_root: str,
    message: str,
) -> None:
    _write_project(tmp_path)
    manifest_path = tmp_path / "paradev.yaml"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest = manifest.replace("output_root: build/mod", f"output_root: {output_root}")
    manifest = manifest.replace("build_root: .paradev/.cache/build", f"build_root: {build_root}")
    manifest_path.write_text(manifest, encoding="utf-8")
    source_path = tmp_path / "src/modules/focus/GER_sample/def.txt"
    original_source = source_path.read_bytes()
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match=message):
        project.build(emit_artifacts=True, emit_manifests=True, full_rebuild=True)

    assert manifest_path.is_file()
    assert source_path.read_bytes() == original_source


def test_project_build_skips_unchanged_registered_hoi4_launcher_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    project = Project.create(
        tmp_path / "stable-launcher",
        project_id="stable_launcher",
        title="Stable Launcher",
    )
    project.build(emit_artifacts=True)
    launcher = mod_root / "stable_launcher.mod"
    launcher_block = PDXBlock.from_file(launcher)
    launcher_tail_keys = {"supported_version", "path", "remote_file_id"}
    launcher_block.entries = [
        *(entry for entry in launcher_block.entries if entry.key_str not in launcher_tail_keys),
        *(entry for entry in launcher_block.entries if entry.key_str in launcher_tail_keys),
    ]
    launcher.write_text(launcher_block.to_str().rstrip("\n"), encoding="utf-8")
    preserved_timestamp = 1_700_000_000_000_000_000
    os.utime(launcher, ns=(preserved_timestamp, preserved_timestamp))

    project.build(emit_artifacts=True)

    assert launcher.stat().st_mtime_ns == preserved_timestamp


def test_project_build_replaces_malformed_registered_hoi4_launcher_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    project = Project.create(
        tmp_path / "repaired-launcher",
        project_id="repaired_launcher",
        title="Repaired Launcher",
    )
    project.build(emit_artifacts=True)
    launcher = mod_root / "repaired_launcher.mod"
    launcher.write_text("tags={\n", encoding="utf-8")

    project.build(emit_artifacts=True)

    assert launcher.read_bytes() == (project.build_root / "launcher/repaired_launcher.mod").read_bytes()


def test_project_build_repairs_relocated_launcher_ownership_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    project = Project.create(
        tmp_path / "relocated-launcher",
        project_id="relocated_launcher",
        title="Relocated Launcher",
    )
    project.build(emit_artifacts=True)
    marker = mod_root / ".paradev-relocated_launcher.launcher.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["project_root"] = str(tmp_path / "retired-worktree")
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    project.build(emit_artifacts=True)

    repaired = json.loads(marker.read_text(encoding="utf-8"))
    assert repaired == {
        "schema": project_sdk.HOI4_LAUNCHER_OWNER_SCHEMA,
        "project_id": project.project_id,
        "project_root": str(project.root),
        "target": "relocated_launcher.mod",
    }


def test_project_build_can_publish_without_accessing_foreign_launcher_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    mod_root.mkdir()
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    project = Project.create(
        tmp_path / "project-only",
        project_id="project_only",
        title="Project Only",
    )
    launcher = mod_root / "project_only.mod"
    launcher.write_text("USER LAUNCHER DATA\n", encoding="utf-8")
    marker = mod_root / ".paradev-project_only.launcher.json"
    foreign_marker = b'{"schema":"foreign.launcher-owner.v1"}\n'
    marker.write_bytes(foreign_marker)

    with pytest.raises(ValueError, match="already claimed by another ParaDev project"):
        project.build(emit_artifacts=True)

    result = project.build(
        emit_artifacts=True,
        emit_manifests=True,
        sync_launcher_descriptor=False,
    )

    assert result.blocked is False
    assert result.dry_run is False
    assert (project.output_root / "descriptor.mod").is_file()
    assert (project.build_root / "launcher/project_only.mod").is_file()
    assert launcher.read_text(encoding="utf-8") == "USER LAUNCHER DATA\n"
    assert marker.read_bytes() == foreign_marker


def test_project_build_serializes_artifact_and_manifest_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = Project.create(tmp_path / "locked_mod", project_id="locked_mod", title="Locked Mod")
    acquired_roots: list[tuple[Path, ...]] = []
    source_locks: list[Path] = []
    discovery_lock_states: list[bool] = []
    original_discover_modules = Project._discover_modules

    @contextmanager
    def record_lock(*roots: Path) -> Iterator[None]:
        acquired_roots.append(roots)
        yield

    @contextmanager
    def record_source_lock(locked_project: Project) -> Iterator[None]:
        source_locks.append(locked_project.root)
        yield

    def record_discover_modules(self: Project, **kwargs: object) -> object:
        discovery_lock_states.append(project_sdk._BUILD_MUTATION_SCOPE_ACTIVE.get())
        return original_discover_modules(self, **kwargs)

    monkeypatch.setattr(project_sdk, "_project_build_mutation_lock", record_lock)
    monkeypatch.setattr(project_sdk, "_project_source_mutation_lock", record_source_lock)
    monkeypatch.setattr(Project, "_discover_modules", record_discover_modules)

    project.build(emit_artifacts=True, emit_manifests=True)

    assert discovery_lock_states == [True]
    assert source_locks == [project.root]
    assert acquired_roots == [
        (
            project.output_root,
            project.build_root,
            project.output_root.parent / f"{project.project_id}.mod",
        )
    ]


def test_project_build_omits_launcher_from_mutation_scope_when_sync_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(
        tmp_path / "project_only_lock",
        project_id="project_only_lock",
        title="Project Only Lock",
    )
    acquired_roots: list[tuple[Path, ...]] = []

    @contextmanager
    def record_lock(*roots: Path) -> Iterator[None]:
        acquired_roots.append(roots)
        yield

    monkeypatch.setattr(project_sdk, "_project_build_mutation_lock", record_lock)

    project.build(
        emit_artifacts=True,
        emit_manifests=True,
        sync_launcher_descriptor=False,
    )

    assert acquired_roots == [(project.output_root, project.build_root)]


def test_project_build_mutation_lock_blocks_another_process_for_shared_roots(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "shared-output"
    build_root = tmp_path / "shared-build"
    ready_path = tmp_path / "child-ready"
    acquired_path = tmp_path / "child-acquired"
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from paradev.sdk.project import _project_build_mutation_lock",
            "output_root, build_root, ready_path, acquired_path = map(Path, sys.argv[1:])",
            "ready_path.write_text('ready', encoding='utf-8')",
            "with _project_build_mutation_lock(output_root, build_root):",
            "    acquired_path.write_text('acquired', encoding='utf-8')",
        )
    )
    child: subprocess.Popen[str] | None = None
    try:
        with project_sdk._project_build_mutation_lock(output_root, build_root):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(output_root),
                    str(build_root),
                    str(ready_path),
                    str(acquired_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready_path.is_file()
            assert child.poll() is None
            assert not acquired_path.exists()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stdout + stderr
        assert acquired_path.read_text(encoding="utf-8") == "acquired"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_project_build_mutation_lock_serializes_shared_launcher_descriptor_for_custom_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))

    def custom_output_project(root_name: str, output_name: str) -> Project:
        project = Project.create(tmp_path / root_name, project_id="shared_launcher", title=root_name)
        manifest = project.manifest_path.read_text(encoding="utf-8")
        project.manifest_path.write_text(
            f"{manifest.rstrip()}\noutput_root: {json.dumps(str(mod_root / output_name))}\n",
            encoding="utf-8",
        )
        return Project.load(project.root)

    first = custom_output_project("first-project", "first-output")
    second = custom_output_project("second-project", "second-output")
    first_roots = project_sdk._project_build_mutation_roots(first, include_launcher=True)
    second_roots = project_sdk._project_build_mutation_roots(second, include_launcher=True)
    launcher_target = (mod_root / "shared_launcher.mod").resolve()

    assert first_roots[:2] != second_roots[:2]
    assert first_roots[2] == second_roots[2] == launcher_target

    ready_path = tmp_path / "launcher-child-ready"
    acquired_path = tmp_path / "launcher-child-acquired"
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from paradev.sdk.project import _project_build_mutation_lock",
            "roots = tuple(Path(value) for value in sys.argv[1:-2])",
            "ready_path, acquired_path = map(Path, sys.argv[-2:])",
            "ready_path.write_text('ready', encoding='utf-8')",
            "with _project_build_mutation_lock(*roots):",
            "    acquired_path.write_text('acquired', encoding='utf-8')",
        )
    )
    child: subprocess.Popen[str] | None = None
    try:
        with project_sdk._project_build_mutation_lock(*first_roots):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    *(str(root) for root in second_roots),
                    str(ready_path),
                    str(acquired_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready_path.is_file()
            assert child.poll() is None
            assert not acquired_path.exists()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stdout + stderr
        assert acquired_path.read_text(encoding="utf-8") == "acquired"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_project_build_mutation_identity_survives_case_unicode_aliases_and_root_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_sdk.sys, "platform", "darwin")
    composed_root = tmp_path / "Caf\u00e9Output"
    decomposed_alias = tmp_path / "cafe\u0301output"
    composed_root.mkdir()

    existing_identity = project_sdk._build_mutation_root_identity(composed_root)
    alias_identity = project_sdk._build_mutation_root_identity(decomposed_alias)
    composed_root.rmdir()
    deleted_identity = project_sdk._build_mutation_root_identity(composed_root)

    assert existing_identity == alias_identity == deleted_identity


def test_windows_build_mutation_lock_retries_only_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def eventually_lock(file_descriptor: int, mode: int, length: int) -> None:
        nonlocal attempts
        assert (file_descriptor, mode, length) == (7, 11, 1)
        attempts += 1
        if attempts <= 12:
            raise OSError(errno.EACCES, "lock held")

    monkeypatch.setattr(project_sdk.time, "sleep", lambda _seconds: None)

    project_sdk._lock_windows_build_mutation_file_descriptor(7, eventually_lock, 11)

    assert attempts == 13

    def invalid_lock(_file_descriptor: int, _mode: int, _length: int) -> None:
        raise OSError(errno.EBADF, "bad descriptor")

    with pytest.raises(OSError, match="bad descriptor"):
        project_sdk._lock_windows_build_mutation_file_descriptor(7, invalid_lock, 11)


def test_project_build_filters_target_modules_for_partial_recompile(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "target_mod", project_id="target_mod", title="Target Mod")
    target_module = project.discover_modules().modules[0]

    result = project.build(module_id=target_module.module_id)

    assert [module.module_id for module in result.modules] == [target_module.module_id]


@pytest.mark.pihc3
def test_project_localisation_postprocessor_shadows_vanilla_state_name_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(game_root))
    project = _write_localisation_postprocessor_project(tmp_path / "project")
    root = project.root
    native = root / "src/modules/native_loc"
    shutil.rmtree(native / "BETA")
    (native / "ALPHA").rename(native / "4")
    native.rename(root / "src/modules/state")
    (root / "src/modules/state/4/main.loc").write_text("[en.STATE_4]\nTrottingham\n\n[zh.STATE_4]\n驼丁汉\n", encoding="utf-8")
    (root / "src/modules/state/4/def.txt").write_text("state = { id = 4 name = STATE_4 provinces = { 1 } }\n", encoding="utf-8")
    state_extension = Path(__file__).resolve().parents[1] / "projects/PIHC3/extensions/state/__init__.py"
    (root / "system/state.py").write_text(
        state_extension.read_text(encoding="utf-8") + "\ndef register(registry):\n    registry.add(PIHC3StateFamily())\n",
        encoding="utf-8",
    )
    config = root / "paradev.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        .split("  native_loc:")[0]
        .replace("  - system/localisation_postprocessor.py", "  - system/localisation_postprocessor.py\n  - system/state.py"),
        encoding="utf-8",
    )
    for language, name in (("english", "Lower Austria"), ("simp_chinese", "下奥地利")):
        reference = game_root / f"localisation/{language}/state_names_l_{language}.yml"
        reference.parent.mkdir(parents=True)
        reference.write_text(f'\ufeffl_{language}:\n STATE_4:0 "{name}"\n', encoding="utf-8")
    project = Project.load(root)
    result = project.build(emit_artifacts=True, emit_manifests=True)
    assert not result.blocked
    artifacts = {str(artifact.path): artifact for artifact in result.artifacts}
    for language, name in (("english", "Trottingham"), ("simp_chinese", "驼丁汉")):
        path = f"localisation/{language}/state_names_l_{language}.yml"
        artifact = artifacts[path]
        assert f' STATE_4:0 "{name}"'.encode() in (project.output_root / path).read_bytes()
        obsolete = f"localisation/replace/{language}/state_names_l_{language}.yml"
        assert obsolete not in artifacts
        assert obsolete in artifact.metadata["publication_replaces"]


@pytest.mark.pihc3
def test_project_localisation_postprocessor_keeps_targeted_publication_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(game_root))
    project = _write_localisation_postprocessor_project(tmp_path / "project")

    planned = project.build(strict_metadata=True)
    first = project.build(
        emit_artifacts=True,
        emit_manifests=True,
        strict_metadata=True,
    )
    alpha_path = project.output_root / "localisation/english/ALPHA_l_english.yml"
    beta_path = project.output_root / "localisation/english/BETA_l_english.yml"
    baseline_path = project.output_root / "localisation/english/baseline_l_english.yml"
    first_localisation = {path.relative_to(project.output_root).as_posix(): path.read_bytes() for path in (alpha_path, beta_path, baseline_path)}

    cached = project.build(emit_artifacts=True, emit_manifests=True)

    assert first.blocked is False
    assert [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in planned.artifacts
    ] == [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in first.artifacts
    ]
    assert cached.blocked is False
    assert first_localisation == {path.relative_to(project.output_root).as_posix(): path.read_bytes() for path in (alpha_path, beta_path, baseline_path)}
    assert b"Native shared" in alpha_path.read_bytes()
    assert b"Legacy shared" not in baseline_path.read_bytes()

    unrelated = project.output_root / "notes/user-owned.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("keep", encoding="utf-8")
    reference = game_root / "localisation/english/vanilla_l_english.yml"
    reference.parent.mkdir(parents=True)
    reference.write_text(
        '\ufeffl_english:\n ALPHA_ONLY:0 "Vanilla alpha"\n',
        encoding="utf-8",
    )
    replace_path = project.output_root / "localisation/replace/english/ALPHA_l_english.yml"
    replace_path.parent.mkdir(parents=True)
    alpha_path.replace(replace_path)
    legacy_state = project.build_root / "localisation-postprocess.json"
    legacy_state.write_text(
        json.dumps(
            {
                "schema": "pihc3.localisation-postprocess.v1",
                "derived_paths": ["localisation/replace/english/ALPHA_l_english.yml"],
            }
        ),
        encoding="utf-8",
    )

    targeted_plan = project.build(
        module_id="native_loc/ALPHA",
        strict_metadata=True,
    )
    targeted = project.build(
        module_id="native_loc/ALPHA",
        emit_artifacts=True,
        emit_manifests=True,
        strict_metadata=True,
    )

    targeted_paths = {str(artifact.path) for artifact in targeted.artifacts if artifact.artifact_type != "mod_descriptor"}
    assert targeted.blocked is False
    assert [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in targeted_plan.artifacts
    ] == [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in targeted.artifacts
    ]
    assert "common/native/ALPHA.txt" in targeted_paths
    assert "common/native/BETA.txt" not in targeted_paths
    assert {
        "localisation/english/BETA_l_english.yml",
        "localisation/english/baseline_l_english.yml",
        "localisation/replace/english/ALPHA_l_english.yml",
    }.issubset(targeted_paths)
    assert not alpha_path.exists()
    assert replace_path.is_file()
    assert beta_path.read_bytes() == first_localisation["localisation/english/BETA_l_english.yml"]
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert legacy_state.is_file()

    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    localisation_rows = [row for row in ledger["artifacts"] if str(row["path"]).endswith(".yml")]
    assert localisation_rows
    assert all(row["publication_scope"] == "project" for row in localisation_rows)
    moved_row = next(row for row in localisation_rows if row["path"] == "localisation/replace/english/ALPHA_l_english.yml")
    assert moved_row["publication_replaces"] == [
        "localisation/english/ALPHA_l_english.yml",
        "localisation/replace/ALPHA_l_english.yml",
    ]

    alpha_source = project.root / "src/modules/native_loc/ALPHA/main.loc"
    alpha_source.write_text(
        alpha_source.read_text(encoding="utf-8").replace(
            "Alpha v1",
            "Alpha v2",
        ),
        encoding="utf-8",
    )
    updated = project.build(
        module_id="native_loc/ALPHA",
        emit_artifacts=True,
        emit_manifests=True,
    )

    assert b"Alpha v2" in replace_path.read_bytes()
    assert "common/native/BETA.txt" not in {str(artifact.path) for artifact in updated.artifacts}
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.pihc3
def test_project_localisation_postprocessor_reconciles_copy_root_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(tmp_path / "game"))
    project = _write_localisation_postprocessor_project(tmp_path / "project")
    copied_path = project.root / "legacy/localisation/english/ZZZ_copied_l_english.yml"
    copied_path.parent.mkdir(parents=True)
    copied_path.write_text(
        '\ufeffl_english:\n SHARED_KEY:0 "Copied shared"\n COPY_ONLY:0 "Copied only"\n',
        encoding="utf-8",
    )
    project.manifest_path.write_text(
        f"{project.manifest_path.read_text(encoding='utf-8')}" "copy_roots:\n" "  - id: legacy\n" "    source: legacy\n" "    target: .\n",
        encoding="utf-8",
    )

    first = Project.load(project.root).build(strict_metadata=True)
    second = Project.load(project.root).build(strict_metadata=True)

    copied = next(artifact for artifact in first.artifacts if artifact.owner == "copy_root:legacy")
    native = next(artifact for artifact in first.artifacts if str(artifact.path) == "localisation/english/ALPHA_l_english.yml")
    assert first.blocked is False
    assert copied.artifact_type == "pihc3_localisation"
    assert copied.payload == (b'\xef\xbb\xbfl_english:\n COPY_ONLY:0 "Copied only"\n')
    assert b"Native shared" in native.payload
    assert b"Copied shared" not in copied.payload
    assert [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in first.artifacts
    ] == [
        (
            artifact.target_root,
            str(artifact.path),
            artifact.artifact_type,
            artifact.owner,
        )
        for artifact in second.artifacts
    ]


@pytest.mark.pihc3
def test_project_localisation_postprocessor_refuses_differing_untracked_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(game_root))
    project = _write_localisation_postprocessor_project(tmp_path / "project")
    project.build(emit_artifacts=True, emit_manifests=True)
    alpha_path = project.output_root / "localisation/english/ALPHA_l_english.yml"
    replace_path = project.output_root / "localisation/replace/english/ALPHA_l_english.yml"
    replace_path.parent.mkdir(parents=True)
    alpha_path.replace(replace_path)
    replace_path.write_text("USER DATA\n", encoding="utf-8")
    unrelated = project.output_root / "notes/user-owned.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("keep", encoding="utf-8")
    reference = game_root / "localisation/english/vanilla_l_english.yml"
    reference.parent.mkdir(parents=True)
    reference.write_text(
        '\ufeffl_english:\n ALPHA_ONLY:0 "Vanilla alpha"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="refuses to adopt an untracked artifact whose content differs",
    ):
        project.build(
            module_id="native_loc/ALPHA",
            emit_artifacts=True,
            emit_manifests=True,
        )

    assert replace_path.read_text(encoding="utf-8") == "USER DATA\n"
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.pihc3
def test_project_localisation_postprocessor_uses_shared_configured_game_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cm_paradev_lock,
) -> None:
    monkeypatch.delenv("PIHC3_HOI4_GAME_ROOT", raising=False)
    configured_root = tmp_path / "configured-game"
    reference = configured_root / "localisation/english/vanilla_l_english.yml"
    reference.parent.mkdir(parents=True)
    reference.write_text(
        '\ufeffl_english:\n ALPHA_ONLY:0 "Vanilla alpha"\n',
        encoding="utf-8",
    )
    environment_root = tmp_path / "environment-game"
    environment_root.mkdir()
    previous = CM_PARADEV.get("paradev.hoi4.game_root", default=None)
    try:
        CM_PARADEV.set("paradev.hoi4.game_root", str(configured_root))
        project = _write_localisation_postprocessor_project(tmp_path / "project")

        configured_plan = project.build(strict_metadata=True)

        assert "localisation/replace/english/ALPHA_l_english.yml" in {str(artifact.path) for artifact in configured_plan.artifacts}

        monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(environment_root))
        environment_plan = project.build(strict_metadata=True)

        assert "localisation/english/ALPHA_l_english.yml" in {str(artifact.path) for artifact in environment_plan.artifacts}
        assert "localisation/replace/english/ALPHA_l_english.yml" not in {str(artifact.path) for artifact in environment_plan.artifacts}
    finally:
        if previous is None:
            CM_PARADEV.unset("paradev.hoi4.game_root")
        else:
            CM_PARADEV.set("paradev.hoi4.game_root", previous)


@pytest.mark.pihc3
def test_project_localisation_postprocessor_preserves_language_identity_for_matching_basenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(game_root))
    project = _write_localisation_postprocessor_project(tmp_path / "project")
    baseline_root = project.root / "src/modules/localization_component/BASE/localisation"
    english_source = baseline_root / "english/shared.yml"
    french_source = baseline_root / "french/shared.yml"
    french_source.parent.mkdir(parents=True)
    english_source.write_text(
        '\ufeffl_english:\n SHARED_BASENAME:0 "English value"\n',
        encoding="utf-8",
    )
    french_source.write_text(
        'l_french:\n SHARED_BASENAME:0 "French value"\n',
        encoding="utf-8",
    )
    english_reference = game_root / "localisation/english/vanilla.yml"
    french_reference = game_root / "localisation/french/vanilla.yml"
    english_reference.parent.mkdir(parents=True)
    french_reference.parent.mkdir(parents=True)
    english_reference.write_text(
        '\ufeff# LocEditor reference annotation\nl_english:\n SHARED_BASENAME:0 "Vanilla English"\n',
        encoding="utf-8",
    )
    french_reference.write_text(
        '\ufeffl_french:\n SHARED_BASENAME:0 "Vanilla French"\n',
        encoding="utf-8",
    )
    (french_reference.parent / "comment_only.yml").write_text(
        "\ufeff# LocEditor file with no active keys\n# l_french:\n",
        encoding="utf-8",
    )
    (game_root / "localisation/languages.yml").write_text(
        '\ufeffl_english:\n LANGUAGE_META_EN:0 "English"\nl_french:\n LANGUAGE_META_FR:0 "French"\n',
        encoding="utf-8",
    )

    result = project.build(strict_metadata=True)

    artifacts = {str(artifact.path): artifact for artifact in result.artifacts}
    english_path = "localisation/replace/english/shared.yml"
    french_path = "localisation/replace/french/shared.yml"
    assert result.blocked is False
    assert english_path in artifacts
    assert french_path in artifacts
    assert "localisation/replace/shared.yml" not in artifacts
    english_payload = artifacts[english_path].payload
    french_payload = artifacts[french_path].payload
    assert isinstance(english_payload, bytes)
    assert isinstance(french_payload, bytes)
    assert english_payload.startswith(b"\xef\xbb\xbf")
    assert french_payload.startswith(b"\xef\xbb\xbf")
    assert not english_payload.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    assert not french_payload.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    assert b"l_english:" in english_payload
    assert b"English value" in english_payload
    assert b"French value" not in english_payload
    assert b"l_french:" in french_payload
    assert b"French value" in french_payload
    assert b"English value" not in french_payload


@pytest.mark.pihc3
@pytest.mark.parametrize(
    ("payload", "message_fragment"),
    (
        (b'\xffl_english:\n KEY:0 "invalid"\n', "is not strict UTF-8"),
        (b' KEY:0 "missing header"\n', "must start with one valid l_* header"),
        (
            b'\xef\xbb\xbfl_english:\n KEY:0 "English"\nl_french:\n KEY_2:0 "French"\n',
            "contains multiple language headers",
        ),
        (
            b'\xef\xbb\xbfl_french:\n KEY:0 "wrong path language"\n',
            "does not match path language",
        ),
    ),
    ids=("invalid-utf8", "missing-header", "mixed-headers", "path-header-mismatch"),
)
def test_project_localisation_postprocessor_rejects_malformed_copy_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    message_fragment: str,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    monkeypatch.setenv("PIHC3_HOI4_GAME_ROOT", str(game_root))
    project = _write_localisation_postprocessor_project(tmp_path / "project")
    first = project.build(
        emit_artifacts=True,
        emit_manifests=True,
        strict_metadata=True,
    )
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    output_before = {path.relative_to(project.output_root).as_posix(): path.read_bytes() for path in project.output_root.rglob("*") if path.is_file()}
    ledger_before = ledger_path.read_bytes()
    malformed_source = project.root / "src/modules/localization_component/BASE/localisation/english/baseline_l_english.yml"
    malformed_source.write_bytes(payload)

    blocked = project.build(
        emit_artifacts=True,
        emit_manifests=True,
        strict_metadata=True,
    )

    assert first.blocked is False
    assert blocked.blocked is True
    assert blocked.dry_run is True
    assert [diagnostic.code for diagnostic in blocked.diagnostics] == ["build.postprocessor_failed"]
    assert message_fragment in blocked.diagnostics[0].message
    assert {path.relative_to(project.output_root).as_posix(): path.read_bytes() for path in project.output_root.rglob("*") if path.is_file()} == output_before
    assert ledger_path.read_bytes() == ledger_before


def test_cached_build_prunes_stale_tracked_artifacts_without_touching_untracked_files(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True, emit_manifests=True)
    generated = project.output_root / "common/national_focus/GER_sample.txt"
    untracked = project.output_root / "notes/user-owned.txt"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("keep", encoding="utf-8")

    shutil.rmtree(tmp_path / "src/modules/focus/GER_sample")
    result = project.build(emit_artifacts=True, emit_manifests=True)

    assert result.blocked is False
    assert not generated.exists()
    assert untracked.read_text(encoding="utf-8") == "keep"
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["schema"] == build_publication.EMITTED_ARTIFACTS_SCHEMA
    assert all(row["path"] != "common/national_focus/GER_sample.txt" for row in ledger["artifacts"])


def test_publication_ledger_tracks_a_durable_whole_project_launch_baseline(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)

    project.build(family="focus", emit_artifacts=True, emit_manifests=True)
    partial = build_publication.load_publication_state(
        project.build_root,
        project_id=project.project_id,
        project_root=project.root,
        output_root=project.output_root,
    )

    assert partial.complete is True
    assert partial.whole_project_baseline is False

    project.build(emit_artifacts=True, emit_manifests=True)
    whole = build_publication.load_publication_state(
        project.build_root,
        project_id=project.project_id,
        project_root=project.root,
        output_root=project.output_root,
    )

    assert whole.complete is True
    assert whole.whole_project_baseline is True

    project.build(module_id="focus/GER_sample", emit_artifacts=True, emit_manifests=True)
    later_partial = build_publication.load_publication_state(
        project.build_root,
        project_id=project.project_id,
        project_root=project.root,
        output_root=project.output_root,
    )

    assert later_partial.complete is True
    assert later_partial.whole_project_baseline is True


def test_previous_v2_publication_ledger_requires_one_new_whole_project_build(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["schema"] = "paradev.build.emitted-artifacts.v2"
    payload.pop("whole_project_baseline")
    ledger_path.write_text(json.dumps(payload), encoding="utf-8")

    state = build_publication.load_publication_state(
        project.build_root,
        project_id=project.project_id,
        project_root=project.root,
        output_root=project.output_root,
    )

    assert state.complete is True
    assert state.whole_project_baseline is False


def test_cached_build_supports_tracked_file_directory_shape_transitions(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    copy_root = tmp_path / "assets"
    copy_root.mkdir()
    shape_path = copy_root / "shape"
    shape_path.write_text("file", encoding="utf-8")
    manifest_path = tmp_path / "paradev.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\ncopy_roots:\n"
        + "  - id: shape_overlay\n"
        + "    source: assets\n"
        + "    target: .\n"
        + "    target_root: output\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    emitted_shape = project.output_root / "shape"
    assert emitted_shape.read_text(encoding="utf-8") == "file"

    shape_path.unlink()
    shape_path.mkdir()
    (shape_path / "child.txt").write_text("child", encoding="utf-8")
    project.build(emit_artifacts=True)
    assert emitted_shape.is_dir()
    assert (emitted_shape / "child.txt").read_text(encoding="utf-8") == "child"

    shutil.rmtree(shape_path)
    shape_path.write_text("file again", encoding="utf-8")
    project.build(emit_artifacts=True)
    assert emitted_shape.is_file()
    assert emitted_shape.read_text(encoding="utf-8") == "file again"


def test_cached_build_removes_the_previous_case_spelling_of_a_tracked_artifact(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    copy_root = tmp_path / "assets"
    copy_root.mkdir()
    source = copy_root / "OLD.txt"
    source.write_text("old", encoding="utf-8")
    nested_source = copy_root / "OLD/INNER/file.txt"
    nested_source.parent.mkdir(parents=True)
    nested_source.write_text("nested", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            (
                "project_id: case_only_artifact",
                "title: Case-only artifact",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "copy_roots:",
                "  - id: overlay",
                "    source: assets",
                "    target: .",
                "    target_root: output",
            )
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    old_spelling = project.output_root / "OLD.txt"
    new_spelling = project.output_root / "old.txt"
    old_nested_spelling = project.output_root / "OLD/INNER/file.txt"
    new_nested_spelling = project.output_root / "old/inner/file.txt"
    assert old_spelling.is_file()
    assert old_nested_spelling.is_file()
    original_bytes = {
        "flat": old_spelling.read_bytes(),
        "nested": old_nested_spelling.read_bytes(),
    }

    source.rename(copy_root / "old.txt")
    (copy_root / "OLD").rename(copy_root / "old")
    (copy_root / "old/INNER").rename(copy_root / "old/inner")
    project.build(emit_artifacts=True)
    project.build(emit_artifacts=True)

    assert sorted(path.name for path in project.output_root.iterdir()) == sorted(
        (
            build_publication.PUBLICATION_ROOT_NAME,
            "descriptor.mod",
            "old",
            "old.txt",
        )
    )
    assert [path.name for path in (project.output_root / "old").iterdir()] == ["inner"]
    assert [path.name for path in (project.output_root / "old/inner").iterdir()] == ["file.txt"]
    assert new_spelling.read_bytes() == original_bytes["flat"] == b"old"
    assert new_nested_spelling.read_bytes() == original_bytes["nested"] == b"nested"
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["state"] == "complete"
    copied_rows = [row for row in ledger["artifacts"] if row["owner"] == "copy_root:overlay"]
    assert [row["path"] for row in copied_rows] == [
        "old.txt",
        "old/inner/file.txt",
    ]
    assert all(not any(key.startswith("pending_") for key in row) for row in ledger["artifacts"])


@pytest.mark.parametrize("full_rebuild", [False, True])
def test_failed_artifact_write_leaves_a_pending_ledger_for_next_build_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_rebuild: bool,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    original_write_artifacts = build_artifacts._write_artifacts_anchored

    def write_one_then_fail(
        result: BuildResult,
        registry: BuildRegistry,
        root: object,
        **kwargs: object,
    ) -> None:
        original_write_artifacts(
            replace(result, artifacts=result.artifacts[:1]),
            registry,
            root,
            **kwargs,
        )
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(build_artifacts, "_write_artifacts_anchored", write_one_then_fail)

    with pytest.raises(RuntimeError, match="simulated writer failure"):
        project.build(emit_artifacts=True, full_rebuild=full_rebuild)

    generated = project.output_root / "common/national_focus/GER_sample.txt"
    assert generated.is_file()
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    pending = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert pending["state"] == "pending"
    assert [row["path"] for row in pending["artifacts"]] == ["common/national_focus/GER_sample.txt"]

    monkeypatch.setattr(build_artifacts, "_write_artifacts_anchored", original_write_artifacts)
    shutil.rmtree(tmp_path / "src/modules/focus/GER_sample")
    result = project.build(emit_artifacts=True)

    assert result.blocked is False
    assert not generated.exists()
    complete = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert complete["state"] == "complete"
    assert all(row["path"] != "common/national_focus/GER_sample.txt" for row in complete["artifacts"])


def test_module_target_prunes_removed_artifact_inside_scope_only(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    for object_id in ("ALPHA", "BETA"):
        module_root = tmp_path / f"src/modules/idea/{object_id}"
        module_root.mkdir(parents=True)
        (module_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
        (module_root / "def.txt").write_text(
            f"ideas = {{ country = {{ {object_id} = {{ picture = {object_id} }} }} }}",
            encoding="utf-8",
        )
        (module_root / "main.loc").write_text(
            f"en:\n  {object_id}: {object_id.title()}\n  {object_id}_desc: {object_id.title()} description\n",
            encoding="utf-8",
        )
        (module_root / "icon.dds").write_bytes(f"{object_id} icon".encode())
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True, emit_manifests=True)
    alpha_icon = project.output_root / "gfx/interface/ideas/idea_ALPHA.dds"
    beta_icon = project.output_root / "gfx/interface/ideas/idea_BETA.dds"
    assert alpha_icon.is_file()
    assert beta_icon.is_file()

    (tmp_path / "src/modules/idea/ALPHA/icon.dds").unlink()
    result = project.build(module_id="idea/ALPHA", emit_artifacts=True, emit_manifests=True)

    assert result.blocked is False
    assert not alpha_icon.exists()
    assert beta_icon.read_bytes() == b"BETA icon"
    assert (project.output_root / "interface/paradev_idea.gfx").is_file()


def test_family_target_prunes_artifacts_for_a_deleted_module(tmp_path: Path) -> None:
    _write_project(tmp_path)
    for object_id in ("ALPHA", "GONE"):
        module_root = tmp_path / f"src/modules/idea/{object_id}"
        module_root.mkdir(parents=True)
        (module_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
        (module_root / "def.txt").write_text(
            f"ideas = {{ country = {{ {object_id} = {{ picture = {object_id} }} }} }}",
            encoding="utf-8",
        )
        (module_root / "main.loc").write_text(
            f"en:\n  {object_id}: {object_id.title()}\n  {object_id}_desc: {object_id.title()} description\n",
            encoding="utf-8",
        )
        (module_root / "icon.dds").write_bytes(f"{object_id} icon".encode())
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    deleted_icon = project.output_root / "gfx/interface/ideas/idea_GONE.dds"
    assert deleted_icon.is_file()

    shutil.rmtree(tmp_path / "src/modules/idea/GONE")
    result = project.build(family="idea", emit_artifacts=True)

    assert result.blocked is False
    assert not deleted_icon.exists()
    assert (project.output_root / "gfx/interface/ideas/idea_ALPHA.dds").is_file()


def test_successor_module_target_preserves_descriptor_replaced_family_ledger_rows(
    tmp_path: Path,
) -> None:
    legacy_module = tmp_path / "src/modules/achievement_component/LEGACY_ACHIEVEMENTS"
    legacy_module.mkdir(parents=True)
    (legacy_module / "def.txt").write_text("legacy_achievement = { happened = yes }\n", encoding="utf-8")
    legacy_assets = tmp_path / "src/modules/achievement_asset_component/LEGACY_ACHIEVEMENT_ASSETS"
    legacy_icon = legacy_assets / "gfx/achievements/ACHIEVEMENT_TEST.dds"
    legacy_icon.parent.mkdir(parents=True)
    legacy_icon.write_bytes(b"OLD DDS")
    manifest_path = tmp_path / "paradev.yaml"
    manifest_path.write_text(
        "\n".join(
            [
                "project_id: achievement_cutover",
                "title: Achievement Cutover",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  achievement_component:",
                "    source_slots:",
                "      - {name: def, match: def.txt, kind: pdx, required: true}",
                "    templates:",
                "      pdx: common/achievements/custom_achievements_pihc_3154495198.txt",
                "  achievement_asset_component:",
                "    source_slots:",
                "      - name: compiled_icons",
                "        match: '^gfx/achievements/.*\\.dds$'",
                "        regex: true",
                "        kind: copy",
                "        many: true",
                "        required: true",
                "    templates:",
                "      copy: '{source_path}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    legacy_artifact = project.output_root / "common/achievements/custom_achievements_pihc_3154495198.txt"
    compiled_icon = project.output_root / "gfx/achievements/ACHIEVEMENT_TEST.dds"
    assert legacy_artifact.is_file()
    assert compiled_icon.read_bytes() == b"OLD DDS"

    shutil.rmtree(legacy_module)
    shutil.rmtree(legacy_assets)
    achievement_module = tmp_path / "src/modules/achievement/ACHIEVEMENT_TEST"
    achievement_module.mkdir(parents=True)
    (achievement_module / "def.txt").write_text("achievement_test = { happened = yes }\n", encoding="utf-8")
    achievement_icon = achievement_module / "gfx/achievements/ACHIEVEMENT_TEST.dds"
    achievement_icon.parent.mkdir(parents=True)
    achievement_icon.write_bytes(b"NEW DDS")
    extension_root = tmp_path / "extensions/achievement"
    extension_root.mkdir(parents=True)
    (extension_root / "__init__.py").write_text(
        """\
from paradev.build import SimpleSourceFamily, Slot


class AchievementFamily(SimpleSourceFamily):
    replaces_registered_family = True

    def __init__(self):
        super().__init__(
            family="achievement",
            source_slots=(
                Slot("def", "def.txt", kind="pdx", required=True),
                Slot(
                    "compiled_icons",
                    r"^gfx/achievements/.*\\.dds$",
                    regex=True,
                    kind="copy",
                    many=True,
                ),
            ),
            pdx_path_template="common/achievements/{object_id}.txt",
            copy_path_template="{source_path}",
        )


def build_family():
    return AchievementFamily()
""",
        encoding="utf-8",
    )
    (extension_root / "meta.yaml").write_text(
        """\
manifest_version: 2
coordinate: paradev-projects/achievement_cutover/achievement
version: 1.0.0
compatibility: {heavenbase: {min: 0.1.2.1, before: 0.1.3.0}}
items:
  - kind: extension
    identifier: achievement-cutover
    source: inline
    target: definition
    active: true
    meta:
      schema_version: 1
      definition:
        identifier: achievement-cutover
        name: Achievement Cutover
        version: 1.0.0
        desc: Test publication cutover.
        required: false
        requires: []
        entities: []
        meta: {}
        setup: null
        api: null
        api_name: null
  - kind: paradev_build_family
    identifier: achievement-cutover
    source: path
    target: {module: null, qualname: build_family}
    active: true
    meta:
      schema_version: 1
      dependencies: [extension:achievement-cutover]
      publication:
        replaces_families:
          - achievement_asset_component
          - achievement_component
""",
        encoding="utf-8",
    )
    manifest_path.write_text(
        "\n".join(
            [
                "project_id: achievement_cutover",
                "title: Achievement Cutover",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(module_id="achievement/ACHIEVEMENT_TEST", emit_artifacts=True)

    assert result.blocked is False
    assert legacy_artifact.is_file()
    assert (project.output_root / "common/achievements/ACHIEVEMENT_TEST.txt").is_file()
    assert compiled_icon.read_bytes() == b"NEW DDS"
    module_ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    compiled_row = next(row for row in module_ledger["artifacts"] if row["path"] == "gfx/achievements/ACHIEVEMENT_TEST.dds")
    assert compiled_row["family"] == "achievement"
    assert any(row.get("family") == "achievement_component" for row in module_ledger["artifacts"])
    assert all(row.get("family") != "achievement_asset_component" for row in module_ledger["artifacts"])

    result = project.build(family="achievement", emit_artifacts=True)

    assert result.blocked is False
    assert not legacy_artifact.exists()
    assert (project.output_root / "common/achievements/ACHIEVEMENT_TEST.txt").is_file()
    assert compiled_icon.read_bytes() == b"NEW DDS"
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert all(row.get("family") != "achievement_component" for row in ledger["artifacts"])


def test_cached_build_rejects_unsafe_emitted_artifact_ledger_paths(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("safe", encoding="utf-8")
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["artifacts"] = [
        {
            "target_root": "output",
            "path": "../sentinel.txt",
            "owner": "module:focus/GER_sample",
            "module_ids": ["focus/GER_sample"],
            "collection_keys": [],
        }
    ]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(ValueError, match="normalized relative path"):
        project.build(emit_artifacts=True)

    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_cached_build_refuses_to_delete_through_an_output_parent_symlink(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    user_root = project.output_root / "notes"
    user_root.mkdir(parents=True)
    victim = user_root / "victim.txt"
    victim.write_text("safe", encoding="utf-8")
    redirected_parent = project.output_root / "generated"
    try:
        redirected_parent.symlink_to(user_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["artifacts"].append(
        {
            "target_root": "output",
            "path": "generated/victim.txt",
            "owner": "module:focus/GER_sample",
            "module_ids": ["focus/GER_sample"],
            "collection_keys": [["focus", "GER_main"]],
            "family": "focus",
        }
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(ValueError, match="crosses a symlink"):
        project.build(family="focus", emit_artifacts=True)

    assert victim.read_text(encoding="utf-8") == "safe"


def test_project_build_rejects_an_output_root_symlink_before_publication(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_root = tmp_path / "user-output"
    user_root.mkdir()
    victim = user_root / "victim.txt"
    victim.write_text("safe", encoding="utf-8")
    project.output_root.parent.mkdir(parents=True)
    try:
        project.output_root.symlink_to(user_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="publication root crosses a symlink"):
        project.build(emit_artifacts=True)

    assert victim.read_text(encoding="utf-8") == "safe"
    assert not (user_root / "common/national_focus/GER_sample.txt").exists()


def test_project_build_refuses_to_write_through_an_output_parent_symlink(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_root = tmp_path / "user-focus"
    user_root.mkdir()
    victim = user_root / "GER_sample.txt"
    victim.write_text("safe", encoding="utf-8")
    parent = project.output_root / "common"
    parent.mkdir(parents=True)
    try:
        (parent / "national_focus").symlink_to(user_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="publication path crosses a symlink"):
        project.build(emit_artifacts=True)

    assert victim.read_text(encoding="utf-8") == "safe"


def test_project_build_refuses_an_untracked_leaf_symlink_without_writing_its_target(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_file = tmp_path / "user-focus.txt"
    user_file.write_text("safe", encoding="utf-8")
    generated = project.output_root / "common/national_focus/GER_sample.txt"
    generated.parent.mkdir(parents=True)
    try:
        generated.symlink_to(user_file)
    except OSError as error:
        pytest.skip(f"File symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="untracked existing artifact path"):
        project.build(emit_artifacts=True)

    assert user_file.read_text(encoding="utf-8") == "safe"
    assert generated.is_symlink()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Win32 retained handles prevent the publication root rename itself.",
)
def test_project_build_keeps_open_output_root_when_its_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_root = tmp_path / "user-output"
    user_root.mkdir()
    victim = user_root / "victim.txt"
    victim.write_text("safe", encoding="utf-8")
    detached = tmp_path / "detached-output"
    original_publish = build_fs.AnchoredDirectory.publish_file
    swapped = False

    def swap_root_then_publish(
        root: build_fs.AnchoredDirectory,
        relative_path: str,
        source: Path,
        *,
        replace: bool = True,
    ) -> Path:
        nonlocal swapped
        if root.path == project.output_root and not swapped:
            project.output_root.rename(detached)
            project.output_root.symlink_to(user_root, target_is_directory=True)
            swapped = True
        return original_publish(root, relative_path, source, replace=replace)

    monkeypatch.setattr(build_fs.AnchoredDirectory, "publish_file", swap_root_then_publish)

    with pytest.raises(ValueError, match="publication root changed during the build"):
        project.build(emit_artifacts=True)

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "safe"
    assert not (user_root / "common/national_focus/GER_sample.txt").exists()
    assert (detached / "common/national_focus/GER_sample.txt").is_file()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Win32 retained handles prevent the publication root rename itself.",
)
def test_ledger_and_manifests_keep_the_open_build_root_when_its_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    user_root = tmp_path / "user-build"
    user_root.mkdir()
    victim = user_root / build_publication.EMITTED_ARTIFACTS_NAME
    victim.write_text("safe", encoding="utf-8")
    detached = tmp_path / "detached-build"
    original_write = build_fs.AnchoredDirectory.write_bytes
    swapped = False

    def swap_root_then_write(
        root: build_fs.AnchoredDirectory,
        relative_path: str,
        payload: bytes,
        *,
        mode: int = 0o644,
        replace: bool = True,
    ) -> Path:
        nonlocal swapped
        if root.path == project.build_root and relative_path == build_publication.EMITTED_ARTIFACTS_NAME and not swapped:
            project.build_root.rename(detached)
            project.build_root.symlink_to(user_root, target_is_directory=True)
            swapped = True
        return original_write(root, relative_path, payload, mode=mode, replace=replace)

    monkeypatch.setattr(build_fs.AnchoredDirectory, "write_bytes", swap_root_then_write)

    with pytest.raises(ValueError, match="publication root changed during the build"):
        project.build(emit_artifacts=True, emit_manifests=True)

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "safe"
    assert (detached / build_publication.EMITTED_ARTIFACTS_NAME).is_file()
    assert (detached / "summary.json").is_file()


def test_stale_cleanup_keeps_its_open_parent_when_the_path_is_swapped_to_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    tracked_parent = output_root / "generated"
    user_parent = output_root / "notes"
    detached_parent = output_root / "detached-generated"
    tracked_parent.mkdir(parents=True)
    user_parent.mkdir(parents=True)
    (tracked_parent / "victim.txt").write_text("tracked", encoding="utf-8")
    user_victim = user_parent / "victim.txt"
    user_victim.write_text("user owned", encoding="utf-8")
    original_stat = build_publication.os.stat
    swapped = False

    def swap_parent_before_final_stat(
        path: object,
        *args: object,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal swapped
        if path == "victim.txt" and dir_fd is not None and not swapped:
            tracked_parent.rename(detached_parent)
            tracked_parent.symlink_to(user_parent, target_is_directory=True)
            swapped = True
        return original_stat(
            path,
            *args,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
            **kwargs,
        )

    monkeypatch.setattr(build_publication.os, "stat", swap_parent_before_final_stat)

    build_publication._delete_tracked_artifact(
        ("output", "generated/victim.txt"),
        output_root=output_root,
        build_root=build_root,
    )

    assert swapped is True
    assert user_victim.read_text(encoding="utf-8") == "user owned"
    assert not (detached_parent / "victim.txt").exists()


@pytest.mark.parametrize(
    "reserved_path",
    [
        "summary.json",
        "summary.json/child.json",
        f"{build_publication.EMITTED_ARTIFACTS_NAME}/child.json",
    ],
)
def test_cached_build_rejects_ledger_rows_in_reserved_build_namespaces(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True, emit_manifests=True)
    summary_path = project.build_root / "summary.json"
    original_summary = summary_path.read_bytes()
    ledger_path = project.build_root / build_publication.EMITTED_ARTIFACTS_NAME
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["artifacts"] = [
        {
            "target_root": "build",
            "path": reserved_path,
            "owner": "module:focus/GER_sample",
            "module_ids": ["focus/GER_sample"],
            "collection_keys": [],
            "family": "focus",
        }
    ]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot track"):
        project.build(emit_artifacts=True, emit_manifests=True)

    assert summary_path.read_bytes() == original_summary


def test_emitted_artifact_ledger_rejects_file_directory_path_collisions() -> None:
    result = BuildResult(
        project_id="structural_collision",
        artifacts=(
            Artifact(
                path="gfx/interface/icons",
                artifact_type="copy",
                owner="module:idea/ALPHA",
            ),
            Artifact(
                path="gfx/interface/icons/alpha.dds",
                artifact_type="copy",
                owner="module:idea/ALPHA",
            ),
        ),
    )

    with pytest.raises(ValueError, match="nested below file artifact"):
        build_publication.emitted_artifact_rows(result)


def test_partial_reconciliation_ignores_an_unrelated_invalid_planned_artifact() -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
        metadata={"family": "idea", "module_id": "idea/TARGET"},
    )
    current = BuildResult(
        project_id="safe_partial",
        modules=(Module(module_id="idea/TARGET", family="idea", root="src/modules/idea/TARGET"),),
        artifacts=(target,),
    )
    planned = replace(
        current,
        artifacts=(
            target,
            Artifact(
                path="../unrelated.txt",
                artifact_type="pdx",
                owner="module:event/BROKEN",
                metadata={"family": "event", "module_id": "event/BROKEN"},
            ),
        ),
    )
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}

    planned_rows = build_publication._planned_rows(
        planned,
        current_result=current,
        current_by_key=current_rows,
    )

    assert [row["path"] for row in planned_rows.values()] == ["common/ideas/TARGET.txt"]


def test_reconciliation_validates_an_extended_plan_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
        metadata={"family": "idea", "module_id": "idea/TARGET"},
    )
    generated = Artifact(
        path="gfx/interface/buildings/strip.dds",
        artifact_type="copy",
        owner="project:safe_full",
    )
    current = BuildResult(project_id="safe_full", artifacts=(target,))
    planned = replace(current, artifacts=(target, generated))
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}
    emitted_artifact_rows = build_publication.emitted_artifact_rows
    calls = 0

    def track_rows(result: BuildResult) -> tuple[dict[str, object], ...]:
        nonlocal calls
        calls += 1
        return emitted_artifact_rows(result)

    monkeypatch.setattr(build_publication, "emitted_artifact_rows", track_rows)

    planned_rows = build_publication._planned_rows(
        planned,
        current_result=current,
        current_by_key=current_rows,
    )

    assert calls == 1
    assert {row["path"] for row in planned_rows.values()} == {
        "common/ideas/TARGET.txt",
        "gfx/interface/buildings/strip.dds",
    }


def test_reconciliation_reuses_current_rows_for_an_identical_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
    )
    current = BuildResult(project_id="safe_identical", artifacts=(target,))
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}

    def unexpected_projection(_result: BuildResult) -> tuple[dict[str, object], ...]:
        raise AssertionError("an identical publication plan must reuse its validated current rows")

    monkeypatch.setattr(build_publication, "emitted_artifact_rows", unexpected_projection)

    planned_rows = build_publication._planned_rows(
        current,
        current_result=current,
        current_by_key=current_rows,
    )

    assert planned_rows == current_rows


def test_partial_reconciliation_tolerates_unrelated_duplicate_planned_artifacts() -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
    )
    duplicate_path = "events/unrelated.txt"
    current = BuildResult(project_id="safe_partial", artifacts=(target,))
    planned = replace(
        current,
        artifacts=(
            target,
            Artifact(path=duplicate_path, artifact_type="pdx", owner="module:event/ONE"),
            Artifact(path=duplicate_path, artifact_type="pdx", owner="module:event/TWO"),
        ),
    )
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}

    planned_rows = build_publication._planned_rows(
        planned,
        current_result=current,
        current_by_key=current_rows,
    )

    assert {row["path"] for row in planned_rows.values()} == {
        "common/ideas/TARGET.txt",
        duplicate_path,
    }


def test_partial_reconciliation_rejects_an_unrelated_structural_collision_with_the_target() -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
    )
    current = BuildResult(project_id="unsafe_partial", artifacts=(target,))
    planned = replace(
        current,
        artifacts=(
            target,
            Artifact(path="common/ideas", artifact_type="pdx", owner="module:event/BROKEN"),
        ),
    )
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}

    with pytest.raises(ValueError, match="structurally conflicts"):
        build_publication._planned_rows(
            planned,
            current_result=current,
            current_by_key=current_rows,
        )


def test_partial_reconciliation_rejects_out_of_scope_tracked_structural_blocker() -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
        metadata={"family": "idea", "module_id": "idea/TARGET"},
    )
    current = BuildResult(project_id="unsafe_retired_partial", artifacts=(target,))
    previous_rows = (
        {
            "target_root": "output",
            "path": "common/ideas",
            "owner": "module:idea_component/LEGACY",
            "family": "idea_component",
            "module_ids": ["idea_component/LEGACY"],
            "collection_keys": [],
        },
    )

    with pytest.raises(
        ValueError,
        match="out-of-scope tracked artifacts.*Run a family or full build",
    ):
        build_publication._reconciliation_rows(
            current,
            planned_result=current,
            previous_rows=previous_rows,
            target_modules={"idea/TARGET"},
            target_collections=set(),
            target_families={"idea"},
            family_wide=False,
            family_wide_families=set(),
        )


def test_partial_reconciliation_rejects_an_unrelated_artifact_colliding_with_the_target() -> None:
    target = Artifact(
        path="common/ideas/TARGET.txt",
        artifact_type="pdx",
        owner="module:idea/TARGET",
        metadata={"family": "idea", "module_id": "idea/TARGET"},
    )
    current = BuildResult(project_id="unsafe_partial", artifacts=(target,))
    planned = replace(
        current,
        artifacts=(
            target,
            Artifact(
                path="COMMON/IDEAS/target.TXT",
                artifact_type="pdx",
                owner="module:event/BROKEN",
                metadata={"family": "event", "module_id": "event/BROKEN"},
            ),
        ),
    )
    current_rows = {build_publication._ledger_key(row): row for row in build_publication.emitted_artifact_rows(current)}

    with pytest.raises(ValueError, match="conflicts with another planned artifact"):
        build_publication._planned_rows(
            planned,
            current_result=current,
            current_by_key=current_rows,
        )


def test_publication_root_identity_preserves_unicode_spelling_on_posix() -> None:
    composed = build_publication._publication_path_identity(Path("/tmp/\u00e9-root"))
    decomposed = build_publication._publication_path_identity(Path("/tmp/e\u0301-root"))

    assert composed != decomposed


def test_stale_cleanup_treats_a_missing_generated_root_as_already_clean(
    tmp_path: Path,
) -> None:
    build_publication._delete_tracked_artifact(
        ("output", "missing/file.txt"),
        output_root=tmp_path / "missing-output",
        build_root=tmp_path / "missing-build",
    )

    assert not (tmp_path / "missing-output").exists()


def test_cached_build_does_not_replay_a_ledger_after_output_root_changes(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/ALPHA"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = { country = { ALPHA = { picture = ALPHA } } }",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  ALPHA: Alpha\n  ALPHA_desc: Alpha description\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(b"generated icon")
    original_project = Project.load(tmp_path)
    original_project.build(emit_artifacts=True)
    shutil.rmtree(idea_root)

    manifest_path = tmp_path / "paradev.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("output_root: build/mod", "output_root: build/relocated"),
        encoding="utf-8",
    )
    relocated_project = Project.load(tmp_path)
    user_file = relocated_project.output_root / "gfx/interface/ideas/idea_ALPHA.dds"
    user_file.parent.mkdir(parents=True)
    user_file.write_bytes(b"user owned")

    result = relocated_project.build(family="idea", emit_artifacts=True)

    assert result.blocked is False
    assert user_file.read_bytes() == b"user owned"
    ledger = json.loads((relocated_project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    assert ledger["identity"]["roots"]["output"]["path"] == build_publication._publication_path_identity(relocated_project.output_root)


def test_cached_build_rejects_a_ledger_owned_by_another_project_root(
    tmp_path: Path,
) -> None:
    shared_build_root = tmp_path / "shared-build"
    project_roots = (tmp_path / "alpha", tmp_path / "beta")
    for project_root in project_roots:
        _write_project(project_root)
        manifest_path = project_root / "paradev.yaml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "build_root: .paradev/.cache/build",
                f"build_root: {shared_build_root}",
            ),
            encoding="utf-8",
        )
    alpha = Project.load(project_roots[0])
    beta = Project.load(project_roots[1])
    alpha.build(emit_artifacts=True)
    ledger_path = shared_build_root / build_publication.EMITTED_ARTIFACTS_NAME
    original_ledger = ledger_path.read_bytes()

    with pytest.raises(ValueError, match="belongs to project root"):
        beta.build(emit_artifacts=True)

    assert ledger_path.read_bytes() == original_ledger


def _metadata_contract(
    *extra_keys: str,
    settings: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    common_keys = [
        "after",
        "collection",
        "comment",
        "game_id",
        "inactive",
        "members",
        "owner",
        "priority",
        "requires",
        "settings",
        "tags",
        "title",
        "type",
    ]
    payload: dict[str, object] = {
        "keys": sorted({*common_keys, *extra_keys}),
        "common_keys": common_keys,
        "family_keys": sorted(extra_keys),
        "unknown_key_policy": {
            "code": "metadata.unknown_key",
            "loose_severity": "warning",
            "strict_severity": "error",
        },
    }
    if settings:
        payload["settings"] = settings
    return payload


def _identity_copy_contract() -> dict[str, object]:
    return {
        "identity_copy": {
            "supported": True,
            "rewriter": "paradev.token-identity.v1",
        }
    }


def _output_contract(
    artifact_type: str,
    template_key: str,
    template: str,
    owner_kinds: list[str],
    *,
    target_root: str = "output",
    route: str | None = None,
    route_setting: str | None = None,
    source_slots: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_type": artifact_type,
        "template_key": template_key,
        "template": template,
        "owner_kinds": owner_kinds,
        "target_root": target_root,
    }
    if route is not None:
        payload["route"] = route
    if route_setting is not None:
        payload["route_setting"] = route_setting
    if source_slots is not None:
        payload["source_slots"] = source_slots
    return payload


class FocusFamily:
    family = "focus"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner=f"collection:{collections[0].collection_id}",
                inputs=(Path(modules[0].root) / "def.txt",),
            ),
        )


class FocusViewFamily:
    family = "focus"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner="collection:GER_main",
                payload="focus = { id = GER_sample }",
            ),
            Artifact(
                path="views/focus-tree/GER_main.json",
                artifact_type="view",
                owner="collection:GER_main",
                target_root="build",
                metadata={"schema": "focus-tree.view.v1", "collection_id": "GER_main"},
            ),
        )


class OrderedArtifactFamily:
    def __init__(self, family: str) -> None:
        self.family = family

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path=f"common/{self.family}.txt",
                artifact_type="pdx",
                owner=f"module:{modules[0].module_id}",
            ),
        )


class BuildRootPDXFamily:
    family = "focus"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner="collection:GER_main",
                payload="focus = { id = GER_sample }",
            ),
            Artifact(
                path="debug/focus/GER_main.txt",
                artifact_type="pdx",
                owner="collection:GER_main",
                target_root="build",
                payload="focus debug",
            ),
        )


class DroppingNormalizeFamily:
    family = "focus"

    def normalize(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Module, ...]:
        return ()

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return ()


class FailingNormalizeFamily:
    family = "focus"

    def normalize(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Module, ...]:
        raise RuntimeError("metadata schema unavailable")

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner="module:focus/GER_sample",
            ),
        )


class FailingCheckFamily:
    family = "focus"

    def check(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Diagnostic, ...]:
        raise RuntimeError("rules service unavailable")

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner="module:focus/GER_sample",
            ),
        )


class InvalidCheckReturnFamily:
    family = "focus"

    def check(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[object, ...]:
        return ("not a diagnostic",)

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="common/national_focus/GER_main.txt",
                artifact_type="pdx",
                owner="module:focus/GER_sample",
            ),
        )


class FailingEmitFamily:
    family = "focus"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        raise RuntimeError("artifact template unavailable")


class InvalidEmitReturnFamily:
    family = "focus"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[object, ...]:
        return ("not an artifact",)


class MissingEmitFamily:
    family = "focus"


def _write_project(root: Path) -> None:
    (root / "src/modules/focus/GER_sample").mkdir(parents=True)
    (root / "src/modules/focus/GER_sample/def.txt").write_text("focus = { id = GER_sample }", encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_build",
                "title: Test Build",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )


def _write_localisation_postprocessor_project(root: Path) -> Project:
    system_root = root / "system"
    baseline_root = root / "src/modules/localization_component/BASE"
    alpha_root = root / "src/modules/native_loc/ALPHA"
    beta_root = root / "src/modules/native_loc/BETA"
    for path in (
        system_root,
        baseline_root / "localisation/english",
        alpha_root,
        beta_root,
    ):
        path.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "projects/PIHC3/extensions/localisation/__init__.py",
        system_root / "localisation_postprocessor.py",
    )
    (baseline_root / "localisation/english/baseline_l_english.yml").write_text(
        "\ufeffl_english:\n" ' SHARED_KEY:0 "Legacy shared"\n' ' BASE_ONLY:0 "Baseline only"\n',
        encoding="utf-8",
    )
    (alpha_root / "def.txt").write_text(
        "ALPHA = { value = 1 }\n",
        encoding="utf-8",
    )
    (alpha_root / "main.loc").write_text(
        "[en.SHARED_KEY]\nNative shared\n\n[en.ALPHA_ONLY]\nAlpha v1\n",
        encoding="utf-8",
    )
    (beta_root / "def.txt").write_text(
        "BETA = { value = 2 }\n",
        encoding="utf-8",
    )
    (beta_root / "main.loc").write_text(
        "[en.BETA_ONLY]\nBeta stable\n",
        encoding="utf-8",
    )
    (root / "paradev.yaml").write_text(
        "\n".join(
            (
                "project_id: localisation_partial",
                "title: Localisation Partial",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: output",
                "build_root: .paradev/.cache/build",
                "python_modules:",
                "  - system/localisation_postprocessor.py",
                "families:",
                "  localization_component:",
                "    visible: false",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: assets",
                '        match: "^localisation/.*\\\\.yml$"',
                "        regex: true",
                "        kind: copy",
                "        many: true",
                "        required: true",
                "    templates:",
                '      copy: "{source_path}"',
                "  native_loc:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "        required: true",
                "      - name: loc",
                '        match: "*.loc"',
                "        kind: loc",
                "        many: true",
                "        required: true",
                "    templates:",
                '      pdx: "common/native/{object_id}.txt"',
                '      loc: "localisation/{language_folder}/{object_id}_{language}.yml"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return Project.load(root)


def _write_collected_focus_module(
    root: Path,
    object_id: str,
    collection_id: str,
    *,
    requires: tuple[str, ...] = (),
) -> None:
    module_root = root / f"src/modules/focus/{object_id}"
    module_root.mkdir(parents=True, exist_ok=True)
    (module_root / "def.txt").write_text(f"focus = {{ id = {object_id} }}", encoding="utf-8")
    metadata = ["type: focus", f"collection: {collection_id}"]
    if requires:
        metadata.append("requires:")
        metadata.extend(f"  - {target}" for target in requires)
    (module_root / "meta.yaml").write_text("\n".join(metadata) + "\n", encoding="utf-8")


def _content_artifact_paths(result: BuildResult) -> list[str]:
    return [str(artifact.path) for artifact in _content_artifacts(result)]


def _content_artifacts(result: BuildResult) -> list[Artifact]:
    return [artifact for artifact in result.artifacts if artifact.artifact_type != "mod_descriptor"]


def test_module_target_emits_complete_collection_and_full_manifest_snapshot(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    _write_collected_focus_module(
        tmp_path,
        "GER_sample",
        "GER_main",
        requires=("focus/ITA_sample",),
    )
    _write_collected_focus_module(tmp_path, "FRA_sample", "GER_main")
    _write_collected_focus_module(tmp_path, "ITA_sample", "ITA_main")
    project = Project.load(tmp_path)
    preflight = project.build(module_id="focus/GER_sample")

    result = project.build(
        module_id="focus/GER_sample",
        emit_artifacts=True,
        emit_manifests=True,
    )

    aggregate = (project.output_root / "common/national_focus/GER_main.txt").read_text(encoding="utf-8")
    assert "id = GER_sample" in aggregate
    assert "id = FRA_sample" in aggregate
    assert "id = ITA_sample" not in aggregate
    assert [module.module_id for module in result.modules] == [
        "focus/FRA_sample",
        "focus/GER_sample",
    ]
    assert [collection.collection_id for collection in result.collections] == ["GER_main"]
    assert not any(diagnostic.code == "build.missing_dependency_target" for diagnostic in result.diagnostics)
    assert [module.module_id for module in preflight.modules] == [module.module_id for module in result.modules]
    assert [(collection.family, collection.collection_id) for collection in preflight.collections] == [
        (collection.family, collection.collection_id) for collection in result.collections
    ]
    assert [(artifact.path, artifact.owner, artifact.inputs) for artifact in preflight.artifacts] == [
        (artifact.path, artifact.owner, artifact.inputs) for artifact in result.artifacts
    ]

    modules_payload = json.loads((project.build_root / "modules.json").read_text(encoding="utf-8"))
    sources_payload = json.loads((project.build_root / "sources.json").read_text(encoding="utf-8"))
    summary_payload = json.loads((project.build_root / "summary.json").read_text(encoding="utf-8"))
    assert [row["module_id"] for row in modules_payload["modules"]] == [
        "focus/FRA_sample",
        "focus/GER_sample",
        "focus/ITA_sample",
    ]
    assert {row["module_id"] for row in sources_payload["sources"] if "module_id" in row} == {
        "focus/FRA_sample",
        "focus/GER_sample",
        "focus/ITA_sample",
    }
    assert summary_payload["summary"]["module_count"] == 3
    assert summary_payload["summary"]["collection_count"] == 2


def test_collection_target_emits_descriptor_and_all_members(tmp_path: Path) -> None:
    _write_project(tmp_path)
    _write_collected_focus_module(tmp_path, "GER_sample", "GER_main")
    _write_collected_focus_module(tmp_path, "FRA_sample", "GER_main")
    project = Project.load(tmp_path)

    result = project.build(
        family="focus",
        collection_id="GER_main",
        emit_artifacts=True,
    )

    aggregate = (project.output_root / "common/national_focus/GER_main.txt").read_text(encoding="utf-8")
    assert "id = GER_sample" in aggregate
    assert "id = FRA_sample" in aggregate
    assert [module.module_id for module in result.modules] == [
        "focus/FRA_sample",
        "focus/GER_sample",
    ]
    assert result.collections[0].module_ids == (
        "focus/FRA_sample",
        "focus/GER_sample",
    )


def test_collection_target_prunes_side_artifacts_for_a_deleted_member(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    for object_id in ("GER_sample", "FRA_sample"):
        _write_collected_focus_module(tmp_path, object_id, "GER_main")
        (tmp_path / f"src/modules/focus/{object_id}/main.loc").write_text(
            f"en:\n  {object_id}: {object_id}\n  {object_id}_desc: Description\n",
            encoding="utf-8",
        )
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    deleted_localization = project.output_root / "localisation/english/GER_sample_l_english.yml"
    assert deleted_localization.is_file()

    shutil.rmtree(tmp_path / "src/modules/focus/GER_sample")
    result = project.build(
        family="focus",
        collection_id="GER_main",
        emit_artifacts=True,
    )

    assert result.blocked is False
    assert not deleted_localization.exists()
    assert (project.output_root / "localisation/english/FRA_sample_l_english.yml").is_file()


def test_collection_target_preserves_artifact_moved_to_another_collection(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    for object_id in ("GER_sample", "FRA_sample"):
        _write_collected_focus_module(tmp_path, object_id, "GER_main")
        (tmp_path / f"src/modules/focus/{object_id}/main.loc").write_text(
            f"en:\n  {object_id}: {object_id}\n  {object_id}_desc: Description\n",
            encoding="utf-8",
        )
    project = Project.load(tmp_path)
    project.build(emit_artifacts=True)
    moved_localization = project.output_root / "localisation/english/GER_sample_l_english.yml"
    original_bytes = moved_localization.read_bytes()

    (tmp_path / "src/modules/focus/GER_sample/meta.yaml").write_text(
        "type: focus\ncollection: OTHER\n",
        encoding="utf-8",
    )
    result = project.build(
        family="focus",
        collection_id="GER_main",
        emit_artifacts=True,
    )

    assert result.blocked is False
    assert moved_localization.read_bytes() == original_bytes
    ledger = json.loads((project.build_root / build_publication.EMITTED_ARTIFACTS_NAME).read_text(encoding="utf-8"))
    moved_row = next(row for row in ledger["artifacts"] if row["target_root"] == "output" and row["path"] == "localisation/english/GER_sample_l_english.yml")
    assert moved_row["collection_keys"] == [["focus", "OTHER"]]


def test_module_target_preserves_collection_descriptor_outputs(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/news_event/GER_news"
    collection_root = tmp_path / "src/collections/news_event/germany"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: news_event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "header.txt").write_text("add_namespace = germany", encoding="utf-8")
    (collection_root / "strings.loc").write_text("en:\n  germany: Germany News\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_partial_collection_descriptor",
                "title: Test Partial Collection Descriptor",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  news_event:",
                "    kind: collection_source",
                "    source_slots:",
                "      - name: body",
                "        match: body.txt",
                "        kind: pdx",
                "        required: true",
                "    collection_source_slots:",
                "      - name: header",
                "        match: header.txt",
                "        kind: pdx",
                "      - name: strings",
                "        match: strings.loc",
                "        kind: loc",
                "    templates:",
                "      pdx: events/{collection_id}.txt",
                "      module_pdx: events/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(
        module_id="news_event/GER_news",
        emit_artifacts=True,
        emit_manifests=True,
    )

    event_output = (project.output_root / "events/germany.txt").read_text(encoding="utf-8")
    assert "add_namespace = germany" in event_output
    assert "country_event" in event_output
    assert (project.output_root / "localisation/english/germany_l_english.yml").is_file()
    assert _content_artifacts(result)[0].inputs == (
        collection_root / "header.txt",
        module_root / "body.txt",
    )
    assert _content_artifacts(result)[1].inputs == (collection_root / "strings.loc",)


def test_module_target_does_not_emit_same_collection_id_from_another_family(
    tmp_path: Path,
) -> None:
    for family, module_id, output_path in (
        ("bulletin", "GER_news", "events/{collection_id}.txt"),
        ("dossier", "GER_report", "common/dossiers/{collection_id}.txt"),
    ):
        module_root = tmp_path / f"src/modules/{family}/{module_id}"
        collection_root = tmp_path / f"src/collections/{family}/germany"
        module_root.mkdir(parents=True)
        collection_root.mkdir(parents=True)
        (module_root / "body.txt").write_text(f"{family} = {{ id = {module_id} }}", encoding="utf-8")
        (module_root / "meta.yaml").write_text(f"type: {family}\ncollection: germany\n", encoding="utf-8")
        (collection_root / "header.txt").write_text(f"namespace = {family}", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: duplicate_collection_ids",
                "title: Duplicate Collection IDs",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    source_slots:",
                "      - {name: body, match: body.txt, kind: pdx, required: true}",
                "    collection_source_slots:",
                "      - {name: header, match: header.txt, kind: pdx}",
                "    templates:",
                "      pdx: events/{collection_id}.txt",
                "  dossier:",
                "    kind: collection_source",
                "    source_slots:",
                "      - {name: body, match: body.txt, kind: pdx, required: true}",
                "    collection_source_slots:",
                "      - {name: header, match: header.txt, kind: pdx}",
                "    templates:",
                "      pdx: common/dossiers/{collection_id}.txt",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="is ambiguous; pass family"):
        project.build(collection_id="germany", emit_artifacts=True)
    with pytest.raises(ValueError, match="Unknown build module: bulletin/missing"):
        project.build(module_id="bulletin/missing", emit_artifacts=True)
    assert not project.output_root.exists()

    result = project.build(module_id="bulletin/GER_news", emit_artifacts=True)

    assert (project.output_root / "events/germany.txt").is_file()
    assert not (project.output_root / "common/dossiers/germany.txt").exists()
    assert [(collection.family, collection.collection_id) for collection in result.collections] == [("bulletin", "germany")]
    assert all(artifact.metadata.get("family") != "dossier" for artifact in result.artifacts)


def test_targeted_build_rejects_conflicting_or_incomplete_graph_selectors(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    injected = project.discover_modules().modules

    with pytest.raises(ValueError, match="belongs to family 'focus', not 'technology'"):
        project.build(
            family="technology",
            module_id="focus/GER_sample",
            emit_artifacts=True,
        )
    with pytest.raises(ValueError, match="only one targeted build selector"):
        project.build(module_id="focus/GER_sample", collection_id="GER_main")
    with pytest.raises(ValueError, match="cannot accept injected modules or collections"):
        project.build(module_id="focus/GER_sample", modules=injected)


def test_targeted_build_accepts_bare_module_id_when_family_is_explicit(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)

    result = project.build(family="focus", module_id="GER_sample")

    assert [module.module_id for module in result.modules] == ["focus/GER_sample"]


def test_project_build_rejects_full_rebuild_with_partial_target(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="full rebuild cannot be combined"):
        project.build(
            module_id="focus/GER_sample",
            emit_artifacts=True,
            full_rebuild=True,
        )


def test_project_build_returns_dry_run_result_without_writing_by_default(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)

    result = project.build()

    assert result.project_id == "test_build"
    assert result.to_dict()["profile"] == "hoi4"
    assert result.dry_run is True
    assert result.to_dict()["summary"]["artifact_count"] == 3
    assert _content_artifact_paths(result) == ["common/national_focus/GER_sample.txt"]
    assert not project.output_root.exists()
    assert not project.build_root.exists()


def test_project_build_can_emit_manifest_files_when_requested(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )
    collection = Collection(collection_id="GER_main", family="focus_tree", module_ids=(module.module_id,))
    registry = BuildRegistry().add(FocusFamily())

    result = project.build(
        registry=registry,
        modules=(module,),
        collections=(collection,),
        emit_manifests=True,
    )

    assert result.artifacts[0].path == "common/national_focus/GER_main.txt"
    summary_payload = json.loads((project.build_root / "summary.json").read_text(encoding="utf-8"))
    assert summary_payload["profile"] == "hoi4"
    assert summary_payload["summary"]["artifact_count"] == 1


def test_project_build_discovers_source_modules_from_source_roots(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    (tmp_path / "src/modules/focus/GER_sample/meta.yaml").write_text(
        "\n".join(
            [
                "type: focus",
                "collection: GER_main",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src/collections/focus/GER_main").mkdir(parents=True)
    (tmp_path / "src/collections/focus/GER_main/meta.yaml").write_text(
        "\n".join(
            [
                "type: focus_tree",
                "title: German Focus Tree",
                "owner: GER",
                "settings:",
                "  layout: historical",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(SimpleSourceFamily(family="focus", pdx_path_template="common/simple/{object_id}.txt"))

    result = project.build(registry=registry)

    assert result.summary()["module_count"] == 1
    assert result.summary()["collection_count"] == 1
    assert result.modules[0].module_id == "focus/GER_sample"
    assert result.collections[0].collection_id == "GER_main"
    assert result.collections[0].module_ids == ("focus/GER_sample",)
    assert result.collections[0].metadata["title"] == "German Focus Tree"
    assert result.collections[0].metadata["settings"] == {"layout": "historical"}
    assert result.modules[0].source_slots == {"def": ("def.txt",)}
    assert result.modules[0].metadata["object_id"] == "GER_sample"
    assert result.artifacts[0].path == "common/simple/GER_sample.txt"


def test_project_build_uses_registered_family_source_slots(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/event/GER_news"
    module_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text(
        "\n".join(
            [
                "type: event",
                "scope: country",
                "settings:",
                "  picture: news",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_custom_slots",
                "title: Test Custom Slots",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            metadata_keys=("scope",),
            settings_keys=("picture",),
            settings_values={"picture": ("feature", "news")},
            required_settings=("picture",),
        )
    )

    result = project.build(registry=registry)

    assert result.summary()["module_count"] == 1
    assert result.modules[0].source_slots == {"body": ("body.txt",)}
    assert result.modules[0].metadata["scope"] == "country"
    assert [diagnostic.code for diagnostic in result.diagnostics] == []
    assert result.artifacts[0].path == "events/GER_news.txt"
    assert result.artifacts[0].inputs == (module_root / "body.txt",)


def test_project_build_uses_manifest_declared_simple_family(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/superevent/FALL_OF_PARIS"
    module_root.mkdir(parents=True)
    (module_root / "script.pdx").write_text("country_event = { id = superevent.1 }", encoding="utf-8")
    (module_root / "main.loc").write_text("en:\n  FALL_OF_PARIS: Fall of Paris\n", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: superevent\nscope: country\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_family",
                "title: Test Manifest Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    metadata_keys: [scope]",
                "    source_slots:",
                "      - name: script",
                "        match: script.pdx",
                "        kind: pdx",
                "        required: true",
                "      - name: loc",
                "        match: '*.loc'",
                "        kind: loc",
                "        many: true",
                "    templates:",
                "      pdx: events/superevents/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    assert families["superevent"].pop("presentation") == {
        "id": "superevent",
        "title": "Superevent",
        "group": "other",
    }
    assert families["superevent"].pop("authoring") == _identity_copy_contract()
    result = project.build()

    assert families["superevent"] == {
        "family": "superevent",
        "kind": "simple_source",
        "metadata": _metadata_contract("scope"),
        "stages": ["discover", "load", "normalize", "check", "emit"],
        "source_slots": [
            {
                "name": "script",
                "match": "script.pdx",
                "required": True,
                "many": False,
                "regex": False,
                "kind": "pdx",
            },
            {
                "name": "loc",
                "match": "*.loc",
                "required": False,
                "many": True,
                "regex": False,
                "kind": "loc",
            },
        ],
        "outputs": [
            _output_contract("pdx", "pdx", "events/superevents/{object_id}.txt", ["module"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
            ),
        ],
        "templates": {
            "pdx": "events/superevents/{object_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
        },
    }
    assert result.modules[0].module_id == "superevent/FALL_OF_PARIS"
    assert result.modules[0].source_slots == {
        "script": ("script.pdx",),
        "loc": ("main.loc",),
    }
    assert result.modules[0].metadata["scope"] == "country"
    assert _content_artifact_paths(result) == [
        "events/superevents/FALL_OF_PARIS.txt",
        "localisation/english/FALL_OF_PARIS_l_english.yml",
    ]
    assert _content_artifacts(result)[0].inputs == (module_root / "script.pdx",)
    assert result.blocked is False


def test_project_build_uses_manifest_declared_collection_family(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/news_event/GER_news"
    collection_root = tmp_path / "src/collections/news_event/germany"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: news_event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "header.txt").write_text("add_namespace = germany", encoding="utf-8")
    (collection_root / "strings.loc").write_text("en:\n  germany: Germany News\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_collection_family",
                "title: Test Manifest Collection Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  news_event:",
                "    kind: collection_source",
                "    source_slots:",
                "      - name: body",
                "        match: body.txt",
                "        kind: pdx",
                "        required: true",
                "    collection_source_slots:",
                "      - name: header",
                "        match: header.txt",
                "        kind: pdx",
                "      - name: strings",
                "        match: strings.loc",
                "        kind: loc",
                "    templates:",
                "      pdx: events/{collection_id}.txt",
                "      module_pdx: events/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    assert families["news_event"].pop("presentation") == {
        "id": "news-event",
        "title": "News Event",
        "group": "other",
    }
    assert families["news_event"].pop("authoring") == _identity_copy_contract()
    result = project.build()
    collection = result.collections[0]
    payload = collection.payload

    assert families["news_event"] == {
        "family": "news_event",
        "kind": "collection_source",
        "metadata": _metadata_contract(),
        "stages": ["discover", "load", "normalize", "aggregate", "check", "emit"],
        "source_slots": [
            {
                "name": "body",
                "match": "body.txt",
                "required": True,
                "many": False,
                "regex": False,
                "kind": "pdx",
            }
        ],
        "collection_source_slots": [
            {
                "name": "header",
                "match": "header.txt",
                "required": False,
                "many": False,
                "regex": False,
                "kind": "pdx",
            },
            {
                "name": "strings",
                "match": "strings.loc",
                "required": False,
                "many": False,
                "regex": False,
                "kind": "loc",
            },
        ],
        "outputs": [
            _output_contract("pdx", "pdx", "events/{collection_id}.txt", ["collection"]),
            _output_contract("pdx", "module_pdx", "events/{object_id}.txt", ["module"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["collection", "module"],
            ),
        ],
        "templates": {
            "pdx": "events/{collection_id}.txt",
            "module_pdx": "events/{object_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
        },
    }
    assert isinstance(payload, CollectionSourceBundle)
    assert payload.source_slots == {
        "header": ("header.txt",),
        "strings": ("strings.loc",),
    }
    assert collection.collection_id == "germany"
    assert collection.module_ids == ("news_event/GER_news",)
    assert _content_artifact_paths(result) == [
        "events/germany.txt",
        "localisation/english/germany_l_english.yml",
    ]
    assert _content_artifacts(result)[0].inputs == (
        collection_root / "header.txt",
        module_root / "body.txt",
    )
    assert _content_artifacts(result)[1].inputs == (collection_root / "strings.loc",)
    assert result.blocked is False


def test_project_build_uses_manifest_declared_routed_family(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/character/GER_advisor"
    module_root.mkdir(parents=True)
    (module_root / "def.txt").write_text("GER_advisor = { available = yes }", encoding="utf-8")
    (module_root / "main.loc").write_text("en:\n  GER_advisor: German Advisor\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_routed_family",
                "title: Test Manifest Routed Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  character:",
                "    kind: routed_source",
                "    route_setting: settings.subtype",
                "    default_route: advisor",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "        required: true",
                "      - name: loc",
                "        match: '*.loc'",
                "        kind: loc",
                "        many: true",
                "    routes:",
                "      advisor:",
                "        pdx: common/advisors/{object_id}.txt",
                "        loc: localisation/{language_folder}/{object_id}_{language}.yml",
                "      commander:",
                "        pdx: common/commanders/{object_id}.txt",
                "        loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    assert families["character"].pop("presentation") == {
        "id": "character",
        "title": "Character",
        "group": "other",
    }
    assert families["character"].pop("authoring") == _identity_copy_contract()
    result = project.build()

    assert families["character"] == {
        "family": "character",
        "kind": "routed_source",
        "metadata": _metadata_contract(
            settings={
                "subtype": {
                    "required": False,
                    "values": ["advisor", "commander"],
                    "default": "advisor",
                }
            }
        ),
        "default_route": "advisor",
        "route_setting": "settings.subtype",
        "stages": ["discover", "load", "normalize", "check", "emit"],
        "source_slots": [
            {
                "name": "def",
                "match": "def.txt",
                "required": True,
                "many": False,
                "regex": False,
                "kind": "pdx",
            },
            {
                "name": "loc",
                "match": "*.loc",
                "required": False,
                "many": True,
                "regex": False,
                "kind": "loc",
            },
        ],
        "outputs": [
            _output_contract(
                "pdx",
                "pdx",
                "common/advisors/{object_id}.txt",
                ["module"],
                route="advisor",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
                route="advisor",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "pdx",
                "pdx",
                "common/commanders/{object_id}.txt",
                ["module"],
                route="commander",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
                route="commander",
                route_setting="settings.subtype",
            ),
        ],
        "routes": {
            "advisor": {
                "pdx": "common/advisors/{object_id}.txt",
                "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            },
            "commander": {
                "pdx": "common/commanders/{object_id}.txt",
                "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            },
        },
    }
    assert _content_artifact_paths(result) == [
        "common/advisors/GER_advisor.txt",
        "localisation/english/GER_advisor_l_english.yml",
    ]
    assert result.artifacts[0].inputs == (module_root / "def.txt",)
    assert result.artifacts[1].inputs == (module_root / "main.loc",)
    assert result.blocked is False


def test_project_build_uses_manifest_declared_routed_sprite_templates(
    tmp_path: Path,
) -> None:
    modules = {
        "GER_advisor": "advisor",
        "GER_commander": "commander",
    }
    for name, subtype in modules.items():
        module_root = tmp_path / f"src/modules/character/{name}"
        module_root.mkdir(parents=True)
        (module_root / "meta.yaml").write_text(f"type: character\nsettings:\n  subtype: {subtype}\n", encoding="utf-8")
        (module_root / "portrait.dds").write_bytes(f"{name} portrait bytes".encode("utf-8"))
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_routed_sprite_templates",
                "title: Test Manifest Routed Sprite Templates",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  character:",
                "    kind: routed_source",
                "    route_setting: settings.subtype",
                "    source_slots:",
                "      - name: portrait",
                "        match: '^portrait\\.(png|dds)$'",
                "        regex: true",
                "        kind: copy",
                "    sprite_slots: [portrait]",
                "    templates:",
                "      sprite_gfx: interface/paradev_{family}.gfx",
                "      sprite_name: GFX_character_{object_id}",
                "    routes:",
                "      advisor:",
                "        copy: gfx/leaders/advisors/{object_id}{source_suffix}",
                "      commander:",
                "        copy: gfx/leaders/commanders/{object_id}{source_suffix}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    result = project.build(emit_artifacts=True)

    assert families["character"]["sprite_slots"] == ["portrait"]
    assert families["character"]["templates"] == {
        "sprite_gfx": "interface/paradev_{family}.gfx",
        "sprite_name": "GFX_character_{object_id}",
    }
    assert result.blocked is False
    assert _content_artifact_paths(result) == [
        "gfx/leaders/advisors/GER_advisor.dds",
        "gfx/leaders/commanders/GER_commander.dds",
        "interface/paradev_character.gfx",
    ]
    assert result.artifacts[2].inputs == (
        tmp_path / "src/modules/character/GER_advisor/portrait.dds",
        tmp_path / "src/modules/character/GER_commander/portrait.dds",
    )
    expected_sprite_gfx = "\n".join(
        [
            "spriteTypes = {",
            "\tSpriteType = {",
            '\t\tname = "GFX_character_GER_advisor"',
            '\t\ttexturefile = "gfx/leaders/advisors/GER_advisor.dds"',
            "\t}",
            "\tSpriteType = {",
            '\t\tname = "GFX_character_GER_commander"',
            '\t\ttexturefile = "gfx/leaders/commanders/GER_commander.dds"',
            "\t}",
            "}",
        ]
    )
    assert (project.output_root / "interface/paradev_character.gfx").read_text(encoding="utf-8") == f"{expected_sprite_gfx}\n"


def test_project_build_uses_manifest_declared_asset_constraints(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/badge/GER_badge"
    payload = b"\x89PNG\r\n\x1a\n" + pack(">I4sIIBBBBBI", 13, b"IHDR", 40, 30, 8, 6, 0, 0, 0, 0)
    module_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: badge\n", encoding="utf-8")
    (module_root / "icon.png").write_bytes(payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_asset_constraints",
                "title: Test Manifest Asset Constraints",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: icon",
                "        match: '^icon\\.(png|dds)$'",
                "        regex: true",
                "        kind: copy",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "    asset_constraints:",
                "      icon:",
                "        formats: [dds]",
                "        width: 64",
                "        height: 64",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    result = project.build()

    assert families["badge"]["assets"] == {
        "slots": {
            "icon": {
                "formats": ["dds"],
                "width": 64,
                "height": 64,
            }
        }
    }
    assert result.blocked is True
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "badge.asset_format",
        "badge.asset_dimensions",
    ]
    assert result.diagnostics[0].source_path == "icon.png"
    assert result.artifacts[0].path == "gfx/badges/GER_badge.png"


def test_build_registry_rejects_unsafe_family_default_asset_paths() -> None:
    with pytest.raises(
        ValueError,
        match="default_assets.icon.path must be relative and stay inside the project",
    ):
        BuildRegistry().add(
            SimpleSourceFamily(
                family="badge",
                copy_path_template="gfx/badges/{object_id}{source_suffix}",
                default_assets={"icon": {"path": "../shared/icon.png"}},
            )
        )


def test_project_build_uses_manifest_declared_sprite_templates(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/badge/GER_badge"
    module_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: badge\n", encoding="utf-8")
    (module_root / "icon.dds").write_bytes(b"badge dds bytes")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_sprite_templates",
                "title: Test Manifest Sprite Templates",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: icon",
                "        match: '^icon\\.(png|dds)$'",
                "        regex: true",
                "        kind: copy",
                "    sprite_slots: [icon]",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "      sprite_gfx: interface/paradev_{family}.gfx",
                "      sprite_name: GFX_badge_{object_id}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    result = project.build(emit_artifacts=True)

    assert families["badge"]["templates"] == {
        "copy": "gfx/badges/{object_id}{source_suffix}",
        "sprite_gfx": "interface/paradev_{family}.gfx",
        "sprite_name": "GFX_badge_{object_id}",
    }
    assert result.blocked is False
    assert _content_artifact_paths(result) == [
        "gfx/badges/GER_badge.dds",
        "interface/paradev_badge.gfx",
    ]
    assert result.artifacts[1].inputs == (module_root / "icon.dds",)
    expected_sprite_gfx = "\n".join(
        [
            "spriteTypes = {",
            "\tSpriteType = {",
            '\t\tname = "GFX_badge_GER_badge"',
            '\t\ttexturefile = "gfx/badges/GER_badge.dds"',
            "\t}",
            "}",
        ]
    )
    assert (project.output_root / "interface/paradev_badge.gfx").read_text(encoding="utf-8") == f"{expected_sprite_gfx}\n"


def test_project_build_blocks_manifest_duplicate_sprite_names(tmp_path: Path) -> None:
    for name in ("GER_alpha", "GER_beta"):
        module_root = tmp_path / f"src/modules/badge/{name}"
        module_root.mkdir(parents=True)
        (module_root / "meta.yaml").write_text("type: badge\n", encoding="utf-8")
        (module_root / "icon.dds").write_bytes(f"{name} dds bytes".encode("utf-8"))
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_duplicate_sprite_names",
                "title: Test Manifest Duplicate Sprite Names",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: icon",
                "        match: '^icon\\.(png|dds)$'",
                "        regex: true",
                "        kind: copy",
                "    sprite_slots: [icon]",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "      sprite_gfx: interface/paradev_{family}.gfx",
                "      sprite_name: GFX_badge_shared",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()
    diagnostics = project.diagnostics()

    assert result.blocked is True
    assert result.diagnostics == (
        Diagnostic(
            code="badge.duplicate_sprite_name",
            message="Sprite name GFX_badge_shared is declared by badge/GER_alpha and badge/GER_beta.",
            severity="error",
            family="badge",
            module_id="badge/GER_beta",
            slot="icon",
            source_path="icon.dds",
        ),
    )
    assert diagnostics["diagnostics"][0]["source"] == {
        "path": str(tmp_path / "src/modules/badge/GER_beta/icon.dds"),
        "module_id": "badge/GER_beta",
        "family": "badge",
        "slot": "icon",
    }


def test_project_build_uses_manifest_declared_setting_normalizers(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/character/GER_advisor"
    module_root.mkdir(parents=True)
    (module_root / "def.txt").write_text("GER_advisor = { available = yes }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: character\nsettings:\n  subtype: Advisor Role\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_manifest_setting_normalizers",
                "title: Test Manifest Setting Normalizers",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  character:",
                "    kind: routed_source",
                "    route_setting: settings.subtype",
                "    settings_normalizers:",
                "      subtype: lower_snake",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "        required: true",
                "    routes:",
                "      advisor_role:",
                "        pdx: common/advisors/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is False
    assert result.modules[0].metadata["settings"] == {"subtype": "advisor_role"}
    assert _content_artifact_paths(result) == ["common/advisors/GER_advisor.txt"]


def test_project_build_uses_manifest_python_registry_module(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/notice/GER_notice"
    plugin_root = tmp_path / "tools"
    module_root.mkdir(parents=True)
    plugin_root.mkdir()
    (module_root / "def.txt").write_text("GER_notice = { visible = yes }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: notice\n", encoding="utf-8")
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily, Slot",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            source_slots=(Slot('def', 'def.txt', required=True, kind='pdx'),),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_python_registry_module",
                "title: Test Python Registry Module",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "python_modules:",
                "  - tools/families.py",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family_payload = project.families()
    families = {row["family"]: row for row in family_payload["families"]}
    assert families["notice"].pop("presentation") == {
        "id": "notice",
        "title": "Notice",
        "group": "other",
    }
    assert families["notice"].pop("authoring") == _identity_copy_contract()
    result = project.build()

    assert families["notice"] == {
        "family": "notice",
        "kind": "simple_source",
        "metadata": _metadata_contract(),
        "stages": ["discover", "load", "normalize", "check", "emit"],
        "source_slots": [
            {
                "name": "def",
                "match": "def.txt",
                "required": True,
                "many": False,
                "regex": False,
                "kind": "pdx",
            }
        ],
        "outputs": [_output_contract("pdx", "pdx", "common/notices/{object_id}.txt", ["module"])],
        "templates": {"pdx": "common/notices/{object_id}.txt"},
    }
    assert result.blocked is False
    assert _content_artifact_paths(result) == ["common/notices/GER_notice.txt"]


def test_project_python_module_supports_postponed_dataclass_annotations(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "from dataclasses import dataclass",
                "",
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "@dataclass(frozen=True, slots=True)",
                "class CompilerInputs:",
                "    family: str",
                "",
                "",
                "def register(registry):",
                "    inputs = CompilerInputs(family='notice')",
                "    registry.add(SimpleSourceFamily(family=inputs.family))",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: postponed_dataclass_plugin",
                "title: Postponed Dataclass Plugin",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "python_modules:",
                "  - tools/families.py",
            ]
        ),
        encoding="utf-8",
    )

    families = Project.load(tmp_path).families()["families"]

    assert any(row["family"] == "notice" for row in families)


def test_project_python_modules_do_not_write_bytecode_under_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_root = tmp_path / "system"
    system_root.mkdir()
    (system_root / "entity_compiler.py").write_text(
        "FAMILY = 'bytecode_free_entity'\n",
        encoding="utf-8",
    )
    (system_root / "entity_family.py").write_text(
        "\n".join(
            (
                "from paradev.build import SimpleSourceFamily",
                "",
                "from .entity_compiler import FAMILY",
                "",
                "",
                "def register(registry):",
                "    registry.add(SimpleSourceFamily(family=FAMILY))",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            (
                "project_id: bytecode_free_project",
                "title: Bytecode Free Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "python_modules:",
                "  - system/entity_family.py",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    families = Project.load(tmp_path).families()["families"]

    assert any(row["family"] == "bytecode_free_entity" for row in families)
    assert not tuple(tmp_path.rglob("__pycache__"))
    assert not tuple(tmp_path.rglob("*.pyc"))
    assert sys.dont_write_bytecode is False


def test_project_python_module_dependencies_are_isolated_and_reloaded(
    tmp_path: Path,
) -> None:
    def write_project(root: Path, project_id: str, family: str) -> None:
        system_root = root / "system"
        system_root.mkdir(parents=True, exist_ok=True)
        (system_root / "entity_compiler.py").write_text(f"FAMILY = {family!r}\n", encoding="utf-8")
        (system_root / "entity_family.py").write_text(
            "\n".join(
                (
                    "from paradev.build import SimpleSourceFamily",
                    "",
                    "from .entity_compiler import FAMILY",
                    "",
                    "",
                    "def register(registry):",
                    "    registry.add(SimpleSourceFamily(family=FAMILY))",
                )
            ),
            encoding="utf-8",
        )
        (root / "paradev.yaml").write_text(
            "\n".join(
                (
                    f"project_id: {project_id}",
                    f"title: {project_id}",
                    "game: hoi4",
                    "source_roots: [src]",
                    "output_root: build/mod",
                    "build_root: .paradev/.cache/build",
                    "python_modules:",
                    "  - system/entity_family.py",
                )
            ),
            encoding="utf-8",
        )

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    write_project(first_root, "first_project", "first_entity")
    write_project(second_root, "second_project", "second_entity")
    first = Project.load(first_root)
    second = Project.load(second_root)

    assert any(row["family"] == "first_entity" for row in first.families()["families"])
    assert any(row["family"] == "second_entity" for row in second.families()["families"])

    (first_root / "system/entity_compiler.py").write_text("FAMILY = 'reloaded_first_entity'\n", encoding="utf-8")

    assert any(row["family"] == "reloaded_first_entity" for row in first.families()["families"])
    assert any(row["family"] == "second_entity" for row in second.families()["families"])


def test_project_python_module_absolute_dependencies_remain_project_isolated(
    tmp_path: Path,
) -> None:
    def write_project(root: Path, project_id: str, family: str) -> Project:
        system_root = root / "system"
        system_root.mkdir(parents=True)
        (system_root / "entity_compiler.py").write_text(f"FAMILY = {family!r}\n", encoding="utf-8")
        (system_root / "entity_family.py").write_text(
            "\n".join(
                (
                    "from paradev.build import SimpleSourceFamily",
                    "from system.entity_compiler import FAMILY",
                    "",
                    "",
                    "def register(registry):",
                    "    registry.add(SimpleSourceFamily(family=FAMILY))",
                )
            ),
            encoding="utf-8",
        )
        (root / "paradev.yaml").write_text(
            "\n".join(
                (
                    f"project_id: {project_id}",
                    f"title: {project_id}",
                    "game: hoi4",
                    "source_roots: [src]",
                    "output_root: build/mod",
                    "build_root: .paradev/.cache/build",
                    "python_modules:",
                    "  - system/entity_family.py",
                )
            ),
            encoding="utf-8",
        )
        return Project.load(root)

    first = write_project(tmp_path / "absolute_first", "absolute_first", "absolute_first_entity")
    second = write_project(tmp_path / "absolute_second", "absolute_second", "absolute_second_entity")

    assert any(row["family"] == "absolute_first_entity" for row in first.families()["families"])
    assert any(row["family"] == "absolute_second_entity" for row in second.families()["families"])
    assert any(row["family"] == "absolute_first_entity" for row in first.families()["families"])


def test_project_build_uses_registered_collection_source_slots(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/event/GER_news"
    collection_root = tmp_path / "src/collections/event/germany"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "category.txt").write_text("add_namespace = germany", encoding="utf-8")
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_slots",
                "title: Test Collection Slots",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            module_pdx_path_template="events/{object_id}.txt",
            loc_path_template="localisation/{language_folder}/{object_id}_{language}.yml",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(
                Slot("category", "category.txt", kind="pdx"),
                Slot("strings", "strings.yml", kind="loc"),
            ),
        )
    )

    result = project.build(registry=registry)
    collection = result.collections[0]
    payload = collection.payload
    localization = project.localization(registry=registry)
    source_map = project.source_map(registry=registry, artifact_type="pdx")
    loc_source_map = project.source_map(registry=registry, artifact_type="loc")

    assert isinstance(payload, CollectionSourceBundle)
    assert payload.source_slots == {
        "category": ("category.txt",),
        "strings": ("strings.yml",),
    }
    assert [(source.slot, source.path) for source in payload.pdx_sources] == [("category", "category.txt")]
    assert payload.loc_entries[0].source_path == "strings.yml"
    assert result.artifacts[0].inputs == (
        collection_root / "category.txt",
        module_root / "body.txt",
    )
    assert source_map["source_map"][0]["sources"] == [
        {
            "path": str(collection_root / "category.txt"),
            "collection_id": "germany",
            "family": "event",
            "slot": "category",
        },
        {
            "path": str(module_root / "body.txt"),
            "module_id": "event/GER_news",
            "family": "event",
            "slot": "body",
        },
    ]
    assert loc_source_map["source_map"][0]["sources"] == [
        {
            "path": str(collection_root / "strings.yml"),
            "collection_id": "germany",
            "family": "event",
            "slot": "strings",
        }
    ]
    assert localization["localization"][0]["source"] == {
        "path": str(collection_root / "strings.yml"),
        "collection_id": "germany",
        "family": "event",
        "slot": "strings",
    }


def test_project_build_allows_manifest_declared_collection_metadata_keys(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "src/collections/bulletin/germany"
    collection_root.mkdir(parents=True)
    (collection_root / "meta.yaml").write_text("scope: country\n", encoding="utf-8")
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_metadata_keys",
                "title: Test Collection Metadata Keys",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    metadata_keys: [scope]",
                "    collection_source_slots:",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()
    diagnostics = project.diagnostics()

    assert result.diagnostics == ()
    assert result.collections[0].metadata == {
        "object_id": "germany",
        "scope": "country",
    }
    assert _content_artifact_paths(result) == ["localisation/english/germany_l_english.yml"]
    assert diagnostics["diagnostics"] == []


def test_project_build_validates_manifest_collection_setting_values(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "src/collections/bulletin/germany"
    collection_root.mkdir(parents=True)
    (collection_root / "meta.yaml").write_text(
        "\n".join(
            [
                "settings:",
                "  layout: experimental",
            ]
        ),
        encoding="utf-8",
    )
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_setting_values",
                "title: Test Collection Setting Values",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    settings_keys: [layout]",
                "    settings_values:",
                "      layout: [historical, alternate]",
                "    collection_source_slots:",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()
    diagnostics = project.diagnostics(collection_id="germany", code="family.unsupported_setting")

    assert result.blocked is True
    assert result.diagnostics == (
        Diagnostic(
            code="family.unsupported_setting",
            message="Collection germany settings.layout 'experimental' must be one of: alternate, historical.",
            severity="error",
            family="bulletin",
            collection_id="germany",
            source_path="meta.yaml",
        ),
    )
    assert diagnostics["diagnostics"] == [
        {
            "code": "family.unsupported_setting",
            "message": "Collection germany settings.layout 'experimental' must be one of: alternate, historical.",
            "severity": "error",
            "family": "bulletin",
            "collection_id": "germany",
            "source_path": "meta.yaml",
            "source": {
                "path": str(collection_root / "meta.yaml"),
                "collection_id": "germany",
                "family": "bulletin",
            },
        }
    ]


def test_project_build_scopes_manifest_collection_descriptor_contracts_by_family(
    tmp_path: Path,
) -> None:
    bulletin_root = tmp_path / "src/collections/bulletin/germany"
    dossier_root = tmp_path / "src/collections/dossier/france"
    (bulletin_root / "media").mkdir(parents=True)
    (dossier_root / "media").mkdir(parents=True)
    png_payload = b"\x89PNG\r\n\x1a\n" + pack(">I4sIIBBBBBI", 13, b"IHDR", 40, 30, 8, 6, 0, 0, 0, 0)
    dds_payload = bytearray(128)
    dds_payload[:4] = b"DDS "
    dds_payload[12:16] = pack("<I", 64)
    dds_payload[16:20] = pack("<I", 64)
    (bulletin_root / "meta.yaml").write_text(
        "\n".join(
            [
                "settings:",
                "  layout: historical",
            ]
        ),
        encoding="utf-8",
    )
    (bulletin_root / "strings.yml").write_text("en:\n  germany: Germany Bulletin\n", encoding="utf-8")
    (bulletin_root / "media/banner.png").write_bytes(png_payload)
    (dossier_root / "meta.yaml").write_text(
        "\n".join(
            [
                "settings:",
                "  layout: internal",
            ]
        ),
        encoding="utf-8",
    )
    (dossier_root / "strings.yml").write_text("en:\n  france_title: France Dossier\n", encoding="utf-8")
    (dossier_root / "media/banner.dds").write_bytes(dds_payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_contract_scope",
                "title: Test Collection Contract Scope",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    settings_keys: [layout]",
                "    settings_values:",
                "      layout: [historical]",
                "    required_loc_keys: ['{object_id}']",
                "    collection_source_slots:",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "      - name: media",
                "        match: media/*",
                "        many: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{family}_{object_id}_{language}.yml",
                "      copy: gfx/bulletins/{object_id}/{source_name}",
                "    asset_constraints:",
                "      media:",
                "        formats: [png]",
                "  dossier:",
                "    kind: collection_source",
                "    settings_keys: [layout]",
                "    settings_values:",
                "      layout: [internal]",
                "    required_loc_keys: ['{object_id}_title']",
                "    collection_source_slots:",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "      - name: media",
                "        match: media/*",
                "        many: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/dossiers/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{family}_{object_id}_{language}.yml",
                "      copy: gfx/dossiers/{object_id}/{source_name}",
                "    asset_constraints:",
                "      media:",
                "        formats: [dds]",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()
    diagnostics = project.diagnostics()

    assert result.blocked is False
    assert result.diagnostics == ()
    assert _content_artifact_paths(result) == [
        "localisation/english/bulletin_germany_l_english.yml",
        "gfx/bulletins/germany/banner.png",
        "localisation/english/dossier_france_l_english.yml",
        "gfx/dossiers/france/banner.dds",
    ]
    assert diagnostics["diagnostics"] == []


def test_project_build_validates_collection_required_localization_keys(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/event/GER_news"
    collection_root = tmp_path / "src/collections/event/germany"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_required_loc",
                "title: Test Collection Required Loc",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(Slot("strings", "strings.yml", kind="loc"),),
            required_loc_keys=("{collection_id}", "{collection_id}_desc"),
        )
    )

    result = project.build(registry=registry)
    diagnostics = project.diagnostics(registry=registry, collection_id="germany", code="event.missing_localization")

    assert result.blocked is True
    assert result.diagnostics == (
        Diagnostic(
            code="event.missing_localization",
            message="Event germany missing localization key 'germany_desc' for l_english.",
            severity="error",
            family="event",
            collection_id="germany",
            slot="strings",
            source_path="strings.yml",
        ),
    )
    assert diagnostics["diagnostics"] == [
        {
            "code": "event.missing_localization",
            "message": "Event germany missing localization key 'germany_desc' for l_english.",
            "severity": "error",
            "family": "event",
            "collection_id": "germany",
            "slot": "strings",
            "source_path": "strings.yml",
            "source": {
                "path": str(collection_root / "strings.yml"),
                "collection_id": "germany",
                "family": "event",
                "slot": "strings",
            },
        }
    ]
    blocked_emit = project.build(registry=registry, emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_project_build_tracks_registered_collection_copy_source_slots(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/event/GER_news"
    collection_root = tmp_path / "src/collections/event/germany"
    module_root.mkdir(parents=True)
    (collection_root / "media").mkdir(parents=True)
    payload = b"collection image"
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "media/banner.png").write_bytes(payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_copy_slots",
                "title: Test Collection Copy Slots",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            copy_path_template="gfx/events/{object_id}/{source_name}",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(Slot("media", "media/*", many=True, kind="copy"),),
        )
    )

    result = project.build(registry=registry)
    assets = project.assets(registry=registry)
    germany_assets = project.assets(registry=registry, collection_id="germany")
    france_assets = project.assets(registry=registry, collection_id="france")
    source_map = project.source_map(registry=registry, artifact_type="copy")
    germany_source_map = project.source_map(registry=registry, collection_id="germany", artifact_type="copy")
    france_source_map = project.source_map(registry=registry, collection_id="france", artifact_type="copy")

    assert _content_artifact_paths(result) == [
        "events/germany.txt",
        "gfx/events/germany/banner.png",
    ]
    assert assets["assets"] == [
        {
            "collection_id": "germany",
            "family": "event",
            "slot": "media",
            "source_path": "media/banner.png",
            "artifact_path": "gfx/events/germany/banner.png",
            "owner": "collection:germany",
            "target_root": "output",
            "sha256": sha256hash(payload),
            "size": 16,
            "source": {
                "path": str(collection_root / "media/banner.png"),
                "collection_id": "germany",
                "family": "event",
                "slot": "media",
            },
        }
    ]
    assert assets["index"] == {"germany": {"media": [0]}}
    assert germany_assets["assets"] == assets["assets"]
    assert germany_assets["index"] == {"germany": {"media": [0]}}
    assert france_assets["assets"] == []
    assert france_assets["index"] == {}
    assert source_map["index"] == {"germany": {"media": [0]}}
    assert germany_source_map["source_map"] == source_map["source_map"]
    assert germany_source_map["index"] == {"germany": {"media": [0]}}
    assert france_source_map["source_map"] == []
    assert france_source_map["index"] == {}
    assert source_map["source_map"][0]["sources"] == [
        {
            "path": str(collection_root / "media/banner.png"),
            "collection_id": "germany",
            "family": "event",
            "slot": "media",
        }
    ]


def test_project_build_emits_manifest_collection_descriptor_artifacts_without_modules(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "src/collections/bulletin/germany"
    (collection_root / "media").mkdir(parents=True)
    payload = b"collection image"
    (collection_root / "category.txt").write_text("add_namespace = germany", encoding="utf-8")
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (collection_root / "media/banner.png").write_bytes(payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_descriptor_only_collection",
                "title: Test Descriptor Only Collection",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    collection_source_slots:",
                "      - name: category",
                "        match: category.txt",
                "        kind: pdx",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "      - name: media",
                "        match: media/*",
                "        many: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
                "      copy: gfx/bulletins/{object_id}/{source_name}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_manifests=True)
    artifacts = project.artifacts(collection_id="germany")
    assets = project.assets(collection_id="germany")
    source_map = project.source_map(collection_id="germany")

    assert result.summary()["module_count"] == 0
    assert result.collections[0].module_ids == ()
    assert _content_artifact_paths(result) == [
        "common/bulletins/germany.txt",
        "localisation/english/germany_l_english.yml",
        "gfx/bulletins/germany/banner.png",
    ]
    assert artifacts["index"]["collection"] == {"germany": [0, 1, 2]}
    assert assets["assets"] == [
        {
            "collection_id": "germany",
            "family": "bulletin",
            "slot": "media",
            "source_path": "media/banner.png",
            "artifact_path": "gfx/bulletins/germany/banner.png",
            "owner": "collection:germany",
            "target_root": "output",
            "sha256": sha256hash(payload),
            "size": 16,
            "source": {
                "path": str(collection_root / "media/banner.png"),
                "collection_id": "germany",
                "family": "bulletin",
                "slot": "media",
            },
        }
    ]
    assert source_map["index"] == {"germany": {"category": [0], "strings": [1], "media": [2]}}
    assert [row["artifact_path"] for row in source_map["source_map"]] == [
        "common/bulletins/germany.txt",
        "localisation/english/germany_l_english.yml",
        "gfx/bulletins/germany/banner.png",
    ]
    assert json.loads((project.build_root / "summary.json").read_text(encoding="utf-8"))["summary"]["module_count"] == 0


def test_project_build_emits_descriptor_localization_and_copy_without_pdx(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "src/collections/bulletin/germany"
    (collection_root / "media").mkdir(parents=True)
    payload = b"collection image"
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (collection_root / "media/banner.png").write_bytes(payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_descriptor_only_loc_copy",
                "title: Test Descriptor Only Loc Copy",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    collection_source_slots:",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "      - name: media",
                "        match: media/*",
                "        many: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
                "      copy: gfx/bulletins/{object_id}/{source_name}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_manifests=True)
    localization = project.localization(collection_id="germany")
    assets = project.assets(collection_id="germany")
    source_map = project.source_map(collection_id="germany")

    assert result.summary()["module_count"] == 0
    assert result.collections[0].module_ids == ()
    assert _content_artifact_paths(result) == [
        "localisation/english/germany_l_english.yml",
        "gfx/bulletins/germany/banner.png",
    ]
    assert localization["index"] == {"l_english": {"germany": {"duplicate": False, "rows": [0]}}}
    assert assets["assets"] == [
        {
            "collection_id": "germany",
            "family": "bulletin",
            "slot": "media",
            "source_path": "media/banner.png",
            "artifact_path": "gfx/bulletins/germany/banner.png",
            "owner": "collection:germany",
            "target_root": "output",
            "sha256": sha256hash(payload),
            "size": 16,
            "source": {
                "path": str(collection_root / "media/banner.png"),
                "collection_id": "germany",
                "family": "bulletin",
                "slot": "media",
            },
        }
    ]
    assert source_map["index"] == {"germany": {"strings": [0], "media": [1]}}
    assert [row["artifact_path"] for row in source_map["source_map"]] == [
        "localisation/english/germany_l_english.yml",
        "gfx/bulletins/germany/banner.png",
    ]


def test_project_build_validates_manifest_collection_asset_constraints(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/bulletin/GER_news"
    collection_root = tmp_path / "src/collections/bulletin/germany"
    module_root.mkdir(parents=True)
    (collection_root / "media").mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n" + pack(">I4sIIBBBBBI", 13, b"IHDR", 40, 30, 8, 6, 0, 0, 0, 0)
    (module_root / "body.txt").write_text("bulletin = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: bulletin\ncollection: germany\n", encoding="utf-8")
    (collection_root / "media/banner.png").write_bytes(payload)
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_asset_constraints",
                "title: Test Collection Asset Constraints",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  bulletin:",
                "    kind: collection_source",
                "    source_slots:",
                "      - name: body",
                "        match: body.txt",
                "        kind: pdx",
                "    collection_source_slots:",
                "      - name: media",
                "        match: media/*",
                "        many: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
                "      copy: gfx/bulletins/{object_id}/{source_name}",
                "    asset_constraints:",
                "      media:",
                "        formats: [dds]",
                "        width: 64",
                "        height: 64",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()
    diagnostics = project.diagnostics(collection_id="germany", code="bulletin.asset_format")

    assert result.blocked is True
    assert result.diagnostics[:2] == (
        Diagnostic(
            code="bulletin.asset_format",
            message="Bulletin asset media/banner.png for slot media format 'png' must be one of: dds.",
            severity="error",
            family="bulletin",
            collection_id="germany",
            slot="media",
            source_path="media/banner.png",
        ),
        Diagnostic(
            code="bulletin.asset_dimensions",
            message="Bulletin asset media/banner.png for slot media dimensions 40x30 must be 64x64.",
            severity="error",
            family="bulletin",
            collection_id="germany",
            slot="media",
            source_path="media/banner.png",
        ),
    )
    assert diagnostics["diagnostics"] == [
        {
            "code": "bulletin.asset_format",
            "message": "Bulletin asset media/banner.png for slot media format 'png' must be one of: dds.",
            "severity": "error",
            "family": "bulletin",
            "collection_id": "germany",
            "slot": "media",
            "source_path": "media/banner.png",
            "source": {
                "path": str(collection_root / "media/banner.png"),
                "collection_id": "germany",
                "family": "bulletin",
                "slot": "media",
            },
        }
    ]
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_project_build_validates_registered_family_setting_values(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/event/GER_bad"
    module_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.2 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text(
        "\n".join(
            [
                "type: event",
                "settings:",
                "  picture: portrait",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_custom_settings",
                "title: Test Custom Settings",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            settings_keys=("picture",),
            settings_values={"picture": ("feature", "news")},
        )
    )

    result = project.build(registry=registry)

    assert result.blocked is True
    assert result.diagnostics[0].code == "family.unsupported_setting"
    assert result.diagnostics[0].module_id == "event/GER_bad"
    assert result.diagnostics[0].source_path == "meta.yaml"


def test_project_build_requires_registered_family_settings(tmp_path: Path) -> None:
    module_root = tmp_path / "src/modules/event/GER_missing"
    module_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.3 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: event\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_required_settings",
                "title: Test Required Settings",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            settings_keys=("picture",),
            required_settings=("picture",),
        )
    )

    result = project.build(registry=registry)

    assert result.blocked is True
    assert result.diagnostics[0].code == "family.missing_setting"
    assert result.diagnostics[0].module_id == "event/GER_missing"
    assert result.diagnostics[0].source_path == "meta.yaml"


def test_project_build_normalizes_family_settings_before_check_and_emit(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/trait/TRAIT_SAMPLE"
    module_root.mkdir(parents=True)
    (module_root / "def.txt").write_text("TRAIT_SAMPLE = { random = no }", encoding="utf-8")
    (module_root / "meta.yaml").write_text(
        "\n".join(
            [
                "type: trait",
                "settings:",
                "  subtype: Country Leader",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_setting_normalizers",
                "title: Test Setting Normalizers",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        RoutedSourceFamily(
            family="trait",
            routes={"country_leader": SourceRoute(pdx_path_template="common/country_leader/{object_id}.txt")},
            settings_normalizers={"subtype": lambda value: str(value).strip().lower().replace(" ", "_")},
        )
    )

    result = project.build(registry=registry)

    assert result.blocked is False
    assert result.modules[0].metadata["settings"] == {"subtype": "country_leader"}
    assert result.artifacts[0].path == "common/country_leader/TRAIT_SAMPLE.txt"


def test_project_build_reports_family_setting_normalization_errors(
    tmp_path: Path,
) -> None:
    def normalize_picture(value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.strip().lower()

    module_root = tmp_path / "src/modules/event/GER_bad"
    module_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.4 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text(
        "\n".join(
            [
                "type: event",
                "settings:",
                "  picture:",
                "    - news",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_bad_setting_normalizers",
                "title: Test Bad Setting Normalizers",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            settings_keys=("picture",),
            settings_values={"picture": ("feature", "news")},
            settings_normalizers={"picture": normalize_picture},
        )
    )

    result = project.build(registry=registry)

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="family.invalid_setting",
        message="Module event/GER_bad settings.picture cannot be normalized: must be a string.",
        severity="error",
        family="event",
        module_id="event/GER_bad",
        source_path="meta.yaml",
    )


def test_project_build_reports_unexpected_family_setting_normalization_errors(
    tmp_path: Path,
) -> None:
    def normalize_picture(value: object) -> object:
        raise TypeError("list values are unsupported")

    module_root = tmp_path / "src/modules/event/GER_bad"
    module_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.5 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text(
        "\n".join(
            [
                "type: event",
                "settings:",
                "  picture:",
                "    - news",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_unexpected_setting_normalizers",
                "title: Test Unexpected Setting Normalizers",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            settings_keys=("picture",),
            settings_values={"picture": ("feature", "news")},
            settings_normalizers={"picture": normalize_picture},
        )
    )

    result = project.build(registry=registry)

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="family.invalid_setting",
        message="Module event/GER_bad settings.picture cannot be normalized: TypeError: list values are unsupported.",
        severity="error",
        family="event",
        module_id="event/GER_bad",
        source_path="meta.yaml",
    )


def test_project_build_reports_normalize_contract_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(DroppingNormalizeFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_normalize_guard", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.normalize_failed",
        message="Build family 'focus' normalize failed: ValueError: Build family 'focus' normalize must return the same module ids.",
        severity="error",
        family="focus",
    )


def test_project_build_reports_normalize_hook_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(FailingNormalizeFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_normalize_hook_error", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.normalize_failed",
        message="Build family 'focus' normalize failed: RuntimeError: metadata schema unavailable.",
        severity="error",
        family="focus",
    )


def test_project_build_reports_check_hook_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(FailingCheckFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_check_hook_error", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.check_failed",
        message="Build family 'focus' check failed: RuntimeError: rules service unavailable.",
        severity="error",
        family="focus",
    )


def test_project_build_reports_check_contract_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(InvalidCheckReturnFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_check_contract_error", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.check_failed",
        message="Build family 'focus' check failed: ValueError: Build family 'focus' emitted a non-Diagnostic value: 'not a diagnostic'.",
        severity="error",
        family="focus",
    )


def test_project_build_reports_emit_hook_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(FailingEmitFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_emit_hook_error", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.emit_failed",
        message="Build family 'focus' emit failed: RuntimeError: artifact template unavailable.",
        severity="error",
        family="focus",
    )


def test_project_build_reports_emit_contract_errors_without_emitting_family_artifacts() -> None:
    registry = BuildRegistry().add(InvalidEmitReturnFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_emit_contract_error", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.emit_failed",
        message="Build family 'focus' emit failed: ValueError: Build family 'focus' emitted a non-Artifact value: 'not an artifact'.",
        severity="error",
        family="focus",
    )


def test_plan_build_accepts_parallelism_and_preserves_family_order() -> None:
    registry = BuildRegistry().add(OrderedArtifactFamily("beta")).add(OrderedArtifactFamily("alpha"))
    alpha = Module(
        module_id="alpha/GER_sample",
        family="alpha",
        root="src/modules/alpha/GER_sample",
    )
    beta = Module(module_id="beta/GER_sample", family="beta", root="src/modules/beta/GER_sample")

    result = plan_build(
        project_id="test_parallel_family_build",
        registry=registry,
        modules=(beta, alpha),
        parallelism=2,
    )

    assert [artifact.path for artifact in result.artifacts] == [
        "common/alpha.txt",
        "common/beta.txt",
    ]


def test_project_build_reports_missing_emit_hooks_without_aborting_build() -> None:
    registry = BuildRegistry().add(MissingEmitFamily())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )

    result = plan_build(project_id="test_missing_emit_hook", registry=registry, modules=(module,))

    assert result.blocked is True
    assert result.artifacts == ()
    assert result.diagnostics[0] == Diagnostic(
        code="family.emit_failed",
        message="Build family 'focus' emit failed: ValueError: Build family 'focus' must define emit(ctx, modules, collections).",
        severity="error",
        family="focus",
    )


def test_project_build_can_emit_artifact_files_when_requested(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(SimpleSourceFamily(family="focus", pdx_path_template="common/simple/{object_id}.txt")).add(PDXTextWriter())

    result = project.build(registry=registry, emit_artifacts=True)

    target = project.output_root / "common/simple/GER_sample.txt"
    assert result.dry_run is False
    assert target.read_text(encoding="utf-8") == "focus = {\n\tid = GER_sample\n}\n"


def test_project_build_routes_view_artifacts_to_build_root(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(FocusViewFamily()).add(PDXTextWriter()).add(JsonViewWriter())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )
    collection = Collection(collection_id="GER_main", family="focus", module_ids=(module.module_id,))

    result = project.build(
        registry=registry,
        modules=(module,),
        collections=(collection,),
        emit_artifacts=True,
    )

    assert result.dry_run is False
    assert (project.output_root / "common/national_focus/GER_main.txt").is_file()
    assert not (project.output_root / "views/focus-tree/GER_main.json").exists()
    assert json.loads((project.build_root / "views/focus-tree/GER_main.json").read_text(encoding="utf-8")) == {
        "schema": "focus-tree.view.v1",
        "family": "focus",
        "collection_id": "GER_main",
    }


def test_project_build_routes_artifacts_by_target_root(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(BuildRootPDXFamily()).add(PDXTextWriter())
    module = Module(
        module_id="focus/GER_sample",
        family="focus",
        root="src/modules/focus/GER_sample",
    )
    collection = Collection(collection_id="GER_main", family="focus", module_ids=(module.module_id,))

    result = project.build(
        registry=registry,
        modules=(module,),
        collections=(collection,),
        emit_artifacts=True,
    )

    assert result.dry_run is False
    assert result.artifacts[0].target_root == "output"
    assert result.artifacts[1].target_root == "build"
    assert (project.output_root / "common/national_focus/GER_main.txt").is_file()
    assert not (project.output_root / "debug/focus/GER_main.txt").exists()
    assert (project.build_root / "debug/focus/GER_main.txt").read_text(encoding="utf-8") == "focus debug"


def test_project_build_uses_game_profile_registry_when_registry_is_not_supplied(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    (tmp_path / "src/modules/focus/GER_sample/meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    pdx_target = project.output_root / "common/national_focus/GER_main.txt"
    view_target = project.build_root / "views/focus-tree/GER_main.json"
    assert result.dry_run is False
    assert _content_artifact_paths(result) == [
        "common/national_focus/GER_main.txt",
        "views/focus-tree/GER_main.json",
    ]
    assert result.artifacts[0].owner == "collection:GER_main"
    assert result.artifacts[1].artifact_type == "view"
    assert result.artifacts[1].owner == "collection:GER_main"
    assert pdx_target.read_text(encoding="utf-8") == "focus = {\n\tid = GER_sample\n}\n"
    assert not (project.output_root / "views/focus-tree/GER_main.json").exists()
    assert json.loads(view_target.read_text(encoding="utf-8")) == {
        "schema": "focus-tree.view.v1",
        "collection_id": "GER_main",
        "family": "focus",
        "nodes": [
            {
                "focus_id": "GER_sample",
                "module_id": "focus/GER_sample",
                "source_path": "def.txt",
                "span": {"line": 1, "column": 16},
                "order": 0,
                "prerequisites": [],
            }
        ],
    }


def test_hoi4_profile_plans_simple_modifier_artifacts(tmp_path: Path) -> None:
    _write_project(tmp_path)
    modifier_root = tmp_path / "src/modules/modifier/MODIFIER_SAMPLE"
    modifier_root.mkdir(parents=True)
    (modifier_root / "def.txt").write_text("MODIFIER_SAMPLE = { stability_factor = 0.05 }", encoding="utf-8")
    (modifier_root / "main.loc").write_text("en:\n  MODIFIER_SAMPLE: Sample Modifier\n", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    modifier_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "module:modifier/MODIFIER_SAMPLE"]
    assert result.blocked is False
    assert [artifact.path for artifact in modifier_artifacts] == [
        "common/modifiers/MODIFIER_SAMPLE.txt",
        "localisation/english/MODIFIER_SAMPLE_l_english.yml",
    ]
    assert [artifact.artifact_type for artifact in modifier_artifacts] == ["pdx", "loc"]


def test_hoi4_profile_groups_collected_modifier_artifacts(tmp_path: Path) -> None:
    _write_project(tmp_path)
    rain_root = tmp_path / "src/modules/modifier/weather_rain_light"
    snow_root = tmp_path / "src/modules/modifier/weather_snow"
    solo_root = tmp_path / "src/modules/modifier/MODIFIER_SAMPLE"
    rain_root.mkdir(parents=True)
    snow_root.mkdir(parents=True)
    solo_root.mkdir(parents=True)
    (rain_root / "meta.yaml").write_text("type: modifier\ncollection: 00_static_modifiers\n", encoding="utf-8")
    (snow_root / "meta.yaml").write_text("type: modifier\ncollection: 00_static_modifiers\n", encoding="utf-8")
    (solo_root / "meta.yaml").write_text("type: modifier\n", encoding="utf-8")
    (rain_root / "def.txt").write_text("weather_rain_light = { naval_speed_factor = -0.1 }", encoding="utf-8")
    (snow_root / "def.txt").write_text("weather_snow = { attrition = 0.05 }", encoding="utf-8")
    (solo_root / "def.txt").write_text("MODIFIER_SAMPLE = { stability_factor = 0.05 }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    pdx_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "pdx"]
    pdx_by_path = {str(artifact.path): artifact for artifact in pdx_artifacts}
    assert result.blocked is False
    assert "common/modifiers/00_static_modifiers.txt" in pdx_by_path
    assert "common/modifiers/MODIFIER_SAMPLE.txt" in pdx_by_path
    assert "common/modifiers/weather_rain_light.txt" not in pdx_by_path
    assert "common/modifiers/weather_snow.txt" not in pdx_by_path
    assert pdx_by_path["common/modifiers/00_static_modifiers.txt"].owner == "collection:00_static_modifiers"
    assert [path.name for path in pdx_by_path["common/modifiers/00_static_modifiers.txt"].inputs] == ["def.txt", "def.txt"]
    assert pdx_by_path["common/modifiers/00_static_modifiers.txt"].payload.to_str() == (
        "weather_rain_light = {\n" "\tnaval_speed_factor = -0.1\n" "}\n" "weather_snow = {\n" "\tattrition = 0.05\n" "}\n"
    )
    assert pdx_by_path["common/modifiers/MODIFIER_SAMPLE.txt"].owner == "module:modifier/MODIFIER_SAMPLE"


def test_hoi4_profile_emits_simple_modifier_files(tmp_path: Path) -> None:
    _write_project(tmp_path)
    modifier_root = tmp_path / "src/modules/modifier/MODIFIER_SAMPLE"
    modifier_root.mkdir(parents=True)
    (modifier_root / "def.txt").write_text("MODIFIER_SAMPLE = { stability_factor = 0.05 }", encoding="utf-8")
    (modifier_root / "main.loc").write_text("en:\n  MODIFIER_SAMPLE: Sample Modifier\n", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    assert result.dry_run is False
    assert result.blocked is False
    assert (project.output_root / "common/modifiers/MODIFIER_SAMPLE.txt").read_text(encoding="utf-8") == ("MODIFIER_SAMPLE = {\n\tstability_factor = 0.05\n}\n")
    assert (project.output_root / "localisation/english/MODIFIER_SAMPLE_l_english.yml").read_text(encoding="utf-8-sig") == (
        'l_english:\n MODIFIER_SAMPLE:0 "Sample Modifier"\n'
    )


def test_hoi4_profile_emits_project_descriptor_and_launcher_preview(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    modifier_root = tmp_path / "src/modules/modifier/MODIFIER_SAMPLE"
    modifier_root.mkdir(parents=True)
    (modifier_root / "def.txt").write_text("MODIFIER_SAMPLE = { stability_factor = 0.05 }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    descriptor_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "mod_descriptor"]
    assert result.blocked is False
    assert [artifact.to_dict() for artifact in descriptor_artifacts] == [
        {
            "path": "descriptor.mod",
            "type": "mod_descriptor",
            "owner": "project:test_build",
            "inputs": [],
            "mode": "plan",
            "target_root": "output",
            "metadata": {
                "name": "Test Build",
                "version": "0.1.0",
                "supported_version": "1.18.*",
                "launcher_preview": False,
            },
        },
        {
            "path": "launcher/test_build.mod",
            "type": "mod_descriptor",
            "owner": "project:test_build",
            "inputs": [],
            "mode": "plan",
            "target_root": "build",
            "metadata": {
                "name": "Test Build",
                "version": "0.1.0",
                "supported_version": "1.18.*",
                "path": str(project.output_root),
                "launcher_preview": True,
            },
        },
    ]
    assert (project.output_root / "descriptor.mod").read_text(encoding="utf-8") == ('version="0.1.0"\nname="Test Build"\nsupported_version="1.18.*"\n')
    assert (project.build_root / "launcher/test_build.mod").read_text(encoding="utf-8") == (
        'version="0.1.0"\n' 'name="Test Build"\n' 'supported_version="1.18.*"\n' f'path="{project.output_root}"\n'
    )


def test_hoi4_profile_emits_manifest_descriptor_metadata(tmp_path: Path) -> None:
    _write_project(tmp_path)
    manifest = tmp_path / "paradev.yaml"
    manifest.write_text(
        f"{manifest.read_text(encoding='utf-8')}\n"
        "mod_version: v0.2.3\n"
        "supported_version: 1.17.*\n"
        "picture: thumbnail.png\n"
        "remote_file_id: 3154495198\n"
        "tags:\n"
        "  - Alternative History\n"
        "replace_path:\n"
        "  - common/ideas\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    assert result.blocked is False
    assert (project.output_root / "descriptor.mod").read_text(encoding="utf-8") == (
        'version="0.2.3"\n'
        'name="Test Build"\n'
        'picture="thumbnail.png"\n'
        'supported_version="1.17.*"\n'
        'remote_file_id="3154495198"\n'
        "tags={\n"
        '\t"Alternative History"\n'
        "}\n"
        'replace_path="common/ideas"\n'
    )
    assert (project.build_root / "launcher/test_build.mod").read_text(encoding="utf-8") == (
        'version="0.2.3"\n'
        'name="Test Build"\n'
        'picture="thumbnail.png"\n'
        'supported_version="1.17.*"\n'
        f'path="{project.output_root}"\n'
        'remote_file_id="3154495198"\n'
        "tags={\n"
        '\t"Alternative History"\n'
        "}\n"
        'replace_path="common/ideas"\n'
    )


def test_project_build_emits_copy_root_files(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_file = legacy_root / "common/scripted_effects/PIHC_legacy.txt"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("legacy_effect = yes\n", encoding="utf-8")
    (legacy_root / "thumbnail.png").write_bytes(b"legacy thumbnail")
    _write_project(tmp_path)
    manifest = tmp_path / "paradev.yaml"
    manifest.write_text(
        f"{manifest.read_text(encoding='utf-8')}\n"
        "copy_roots:\n"
        "  - id: legacy\n"
        "    source: legacy\n"
        "    target: .\n"
        "    include:\n"
        '      - "**/*"\n',
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    copy_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "copy_root:legacy"]
    assert result.blocked is False
    assert [artifact.path for artifact in copy_artifacts] == [
        "common/scripted_effects/PIHC_legacy.txt",
        "thumbnail.png",
    ]
    assert copy_artifacts[0].metadata["source_root"] == str(legacy_root.resolve())
    assert (project.output_root / "common/scripted_effects/PIHC_legacy.txt").read_text(encoding="utf-8") == "legacy_effect = yes\n"
    assert (project.output_root / "thumbnail.png").read_bytes() == b"legacy thumbnail"


def test_project_build_generated_artifacts_shadow_copy_root_files(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_file = legacy_root / "common/modifiers/MODIFIER_SAMPLE.txt"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("legacy modifier\n", encoding="utf-8")
    _write_project(tmp_path)
    modifier_root = tmp_path / "src/modules/modifier/MODIFIER_SAMPLE"
    modifier_root.mkdir(parents=True)
    (modifier_root / "def.txt").write_text("MODIFIER_SAMPLE = { stability_factor = 0.05 }", encoding="utf-8")
    manifest = tmp_path / "paradev.yaml"
    manifest.write_text(
        f"{manifest.read_text(encoding='utf-8')}\n"
        "copy_roots:\n"
        "  - id: legacy\n"
        "    source: legacy\n"
        "    target: .\n"
        "    include:\n"
        "      - common/**\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    assert result.blocked is False
    assert [artifact.owner for artifact in result.artifacts if str(artifact.path) == "common/modifiers/MODIFIER_SAMPLE.txt"] == [
        "module:modifier/MODIFIER_SAMPLE"
    ]
    assert [diagnostic.to_dict() for diagnostic in result.diagnostics if diagnostic.code == "copy_root.shadowed_artifact"] == [
        {
            "code": "copy_root.shadowed_artifact",
            "message": "Copy root legacy file common/modifiers/MODIFIER_SAMPLE.txt is shadowed by generated artifact module:modifier/MODIFIER_SAMPLE.",
            "severity": "warning",
            "source_path": str(legacy_file.resolve()),
            "artifact_path": "common/modifiers/MODIFIER_SAMPLE.txt",
            "target_root": "output",
            "owners": ["copy_root:legacy", "module:modifier/MODIFIER_SAMPLE"],
        }
    ]
    assert (project.output_root / "common/modifiers/MODIFIER_SAMPLE.txt").read_text(encoding="utf-8") == ("MODIFIER_SAMPLE = {\n\tstability_factor = 0.05\n}\n")


def test_project_build_blocks_copy_generated_file_directory_collision_before_emission(
    tmp_path: Path,
) -> None:
    legacy_file = tmp_path / "legacy/common/national_focus"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("legacy file\n", encoding="utf-8")
    _write_project(tmp_path)
    manifest = tmp_path / "paradev.yaml"
    manifest.write_text(
        f"{manifest.read_text(encoding='utf-8')}\n" "copy_roots:\n" "  - id: legacy\n" "    source: legacy\n" "    target: .\n",
        encoding="utf-8",
    )

    result = Project.load(tmp_path).build()

    collision = next(diagnostic for diagnostic in result.diagnostics if diagnostic.code == "build.artifact_path_collision")
    assert result.blocked is True
    assert collision.artifact_path == "common/national_focus/GER_sample.txt"
    assert collision.owners == ("copy_root:legacy", "module:focus/GER_sample")
    assert "file ancestor" in collision.message
    assert not (tmp_path / "build/mod").exists()


def test_project_build_blocks_copy_artifact_without_registered_writer(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir(parents=True)
    copied_file = tmp_path / "legacy/notes/copied.txt"
    copied_file.parent.mkdir(parents=True)
    copied_file.write_text("copied\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            (
                "project_id: missing_copy_writer",
                "title: Missing Copy Writer",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: output",
                "build_root: .paradev/.cache/build",
                "copy_roots:",
                "  - id: legacy",
                "    source: legacy",
                "    target: .",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = Project.load(tmp_path).build(registry=BuildRegistry().add(PDXTextWriter()))

    assert result.blocked is True
    assert [artifact.owner for artifact in result.artifacts] == ["copy_root:legacy"]
    assert [diagnostic.to_dict() for diagnostic in result.diagnostics] == [
        {
            "code": "build.missing_artifact_writer",
            "message": "Artifact type 'copy' is planned but no artifact writer is registered.",
            "severity": "error",
            "artifact_path": "notes/copied.txt",
            "target_root": "output",
        }
    ]


def test_hoi4_profile_emits_idea_pdx_localization_and_icon(tmp_path: Path) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    icon_payload = b"sample idea icon bytes"
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = { country = { GER_industry_spirit = { picture = GER_industry_spirit } } }",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  GER_industry_spirit: German Industry Spirit\n  GER_industry_spirit_desc: Industrial production spirit.\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(icon_payload)
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    idea_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "module:idea/GER_industry_spirit"]
    assert result.blocked is False
    assert [artifact.path for artifact in idea_artifacts] == [
        "common/ideas/GER_industry_spirit.txt",
        "gfx/interface/ideas/idea_GER_industry_spirit.dds",
        "localisation/english/GER_industry_spirit_l_english.yml",
    ]
    assert [artifact.artifact_type for artifact in idea_artifacts] == [
        "pdx",
        "copy",
        "loc",
    ]
    assert idea_artifacts[1].metadata["sha256"] == sha256hash(icon_payload)
    sprite_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "sprite_gfx"]
    assert len(sprite_artifacts) == 1
    assert sprite_artifacts[0].path == "interface/paradev_idea.gfx"
    assert sprite_artifacts[0].owner == "project:test_build"
    assert sprite_artifacts[0].metadata == {"family": "idea", "sprite_count": 1}
    assert (project.output_root / "common/ideas/GER_industry_spirit.txt").read_text(encoding="utf-8") == (
        "ideas = {\n\tcountry = {\n\t\tGER_industry_spirit = {\n\t\t\tpicture = GER_industry_spirit\n\t\t}\n\t}\n}\n"
    )
    assert (project.output_root / "gfx/interface/ideas/idea_GER_industry_spirit.dds").read_bytes() == icon_payload
    assert (project.output_root / "interface/paradev_idea.gfx").read_text(encoding="utf-8") == (
        "spriteTypes = {\n"
        "\tSpriteType = {\n"
        '\t\tname = "GFX_idea_GER_industry_spirit"\n'
        '\t\ttexturefile = "gfx/interface/ideas/idea_GER_industry_spirit.dds"\n'
        "\t}\n"
        "}\n"
    )
    assert (project.output_root / "localisation/english/GER_industry_spirit_l_english.yml").read_text(encoding="utf-8-sig") == (
        'l_english:\n GER_industry_spirit:0 "German Industry Spirit"\n GER_industry_spirit_desc:0 "Industrial production spirit."\n'
    )


def test_hoi4_profile_building_module_target_generates_icon_strip(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/building/alpha"
    sibling_root = tmp_path / "src/modules/building/beta"
    module_root.mkdir(parents=True)
    sibling_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: building\n", encoding="utf-8")
    (sibling_root / "meta.yaml").write_text("type: building\n", encoding="utf-8")
    (module_root / "def.txt").write_text("buildings = { alpha = { icon_frame = 99 } }\n", encoding="utf-8")
    (sibling_root / "def.txt").write_text("buildings = { beta = { icon_frame = 99 } }\n", encoding="utf-8")
    with Image.new("RGBA", (8, 8), "red") as image:
        image.save(module_root / "icon.png")
    with Image.new("RGBA", (8, 8), "blue") as image:
        image.save(sibling_root / "icon.png")
    interface_source = tmp_path / "legacy/interface/countrystateview.gfx"
    interface_source.parent.mkdir(parents=True)
    interface_payload = (
        'spriteTypes = {\n\tspriteType = {\n\t\tname = "GFX_buildings_strip"\n'
        '\t\ttextureFile = "gfx/interface/buildings/building_icon_strip.dds"\n\t\tnoOfFrames = 99\n\t}\n}\n'
    ).encode()
    interface_source.write_bytes(interface_payload)
    legacy_strip_source = tmp_path / "legacy/gfx/interface/buildings/building_icon_strip.dds"
    legacy_strip_source.parent.mkdir(parents=True)
    legacy_strip_source.write_bytes(b"legacy building strip")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_building_icons",
                "title: Test Building Icons",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  building:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "        required: true",
                "      - name: icon",
                "        match: '^icon\\.(png|dds|tga)$'",
                "        regex: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/buildings/{object_id}.txt",
                "copy_roots:",
                "  - id: legacy_interface",
                "    source: legacy",
                "    target: .",
                "    include:",
                "      - interface/**",
                "      - gfx/**",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    events: list[dict[str, object]] = []

    plan = project.build(module_id="building/alpha")

    assert plan.dry_run is True
    planned_strips = [artifact for artifact in plan.artifacts if str(artifact.path) == "gfx/interface/buildings/building_icon_strip.dds"]
    assert len(planned_strips) == 1
    assert planned_strips[0].artifact_type == "building_icon_strip"
    assert planned_strips[0].owner == "project:test_building_icons"
    assert planned_strips[0].inputs == (
        module_root / "icon.png",
        sibling_root / "icon.png",
    )
    assert planned_strips[0].metadata == {
        "family": "building",
        "module_ids": ["building/alpha", "building/beta"],
        "postprocessor": "hoi4.building_icon_strip",
    }
    shadowed_strip_diagnostic = {
        "code": "copy_root.shadowed_artifact",
        "message": (
            "Copy root legacy_interface file gfx/interface/buildings/building_icon_strip.dds is shadowed by generated artifact " "project:test_building_icons."
        ),
        "severity": "warning",
        "source_path": str(legacy_strip_source),
        "artifact_path": "gfx/interface/buildings/building_icon_strip.dds",
        "target_root": "output",
        "owners": ["copy_root:legacy_interface", "project:test_building_icons"],
    }
    assert [diagnostic.to_dict() for diagnostic in plan.diagnostics if diagnostic.code == "copy_root.shadowed_artifact"] == [shadowed_strip_diagnostic]
    assert not (project.output_root / "gfx/interface/buildings/building_icon_strip.dds").exists()

    result = project.build(
        module_id="building/alpha",
        emit_artifacts=True,
        emit_manifests=True,
        progress=events.append,
    )

    strip = project.output_root / "gfx/interface/buildings/building_icon_strip.dds"
    payload = strip.read_bytes()
    height = int.from_bytes(payload[12:16], "little")
    width = int.from_bytes(payload[16:20], "little")
    assert result.blocked is False
    assert result.summary() == plan.summary()
    assert strip.is_file()
    assert [module.module_id for module in result.modules] == [
        "building/alpha",
        "building/beta",
    ]
    assert (width, height) == (92, 46)
    assert "noOfFrames = 2" in (project.output_root / "interface/countrystateview.gfx").read_text(encoding="utf-8")
    assert "icon_frame = 1" in (project.output_root / "common/buildings/alpha.txt").read_text(encoding="utf-8")
    assert "icon_frame = 2" in (project.output_root / "common/buildings/beta.txt").read_text(encoding="utf-8")
    assert any(event.get("label") == "Building icons" and event.get("detail") == "Generated building icon strip with 2 frames." for event in events)
    artifacts = {str(artifact.path): artifact for artifact in result.artifacts}
    artifact_manifest = json.loads((project.build_root / "artifacts.json").read_text(encoding="utf-8"))
    manifest_artifacts = {row["path"]: row for row in artifact_manifest["artifacts"]}
    postprocessed_paths = (
        "common/buildings/alpha.txt",
        "common/buildings/beta.txt",
        "gfx/interface/buildings/building_icon_strip.dds",
        "interface/countrystateview.gfx",
    )
    for relative_path in postprocessed_paths:
        assert sum(str(artifact.path) == relative_path for artifact in result.artifacts) == 1
        assert sum(row["path"] == relative_path for row in artifact_manifest["artifacts"]) == 1
        artifact = artifacts[relative_path]
        manifest_artifact = manifest_artifacts[relative_path]
        emitted_payload = (project.output_root / relative_path).read_bytes()
        assert artifact.mode == "emit"
        assert artifact.metadata["sha256"] == sha256hash(emitted_payload)
        assert artifact.metadata["size"] == len(emitted_payload)
        assert artifact.metadata["postprocessor"] == "hoi4.building_icon_strip"
        assert manifest_artifact["metadata"]["sha256"] == sha256hash(emitted_payload)
        assert manifest_artifact["metadata"]["size"] == len(emitted_payload)
    building_artifact = artifacts["common/buildings/alpha.txt"]
    assert building_artifact.artifact_type == "pdx"
    assert building_artifact.owner == "module:building/alpha"
    assert building_artifact.inputs == (module_root / "def.txt",)
    strip_artifact = artifacts["gfx/interface/buildings/building_icon_strip.dds"]
    assert strip_artifact.artifact_type == "building_icon_strip"
    assert strip_artifact.owner == "project:test_building_icons"
    assert strip_artifact.inputs == (
        module_root / "icon.png",
        sibling_root / "icon.png",
    )
    assert [diagnostic.to_dict() for diagnostic in result.diagnostics if diagnostic.code == "copy_root.shadowed_artifact"] == [shadowed_strip_diagnostic]
    interface_artifact = artifacts["interface/countrystateview.gfx"]
    assert interface_artifact.artifact_type == "copy"
    assert interface_artifact.owner == "copy_root:legacy_interface"
    assert interface_artifact.inputs == (interface_source,)
    assert interface_artifact.metadata["source_sha256"] == sha256hash(interface_payload)
    assert interface_artifact.metadata["source_size"] == len(interface_payload)
    assert interface_artifact.metadata["byte_size"] == len((project.output_root / "interface/countrystateview.gfx").read_bytes())
    source_map = json.loads((project.build_root / "source-map.json").read_text(encoding="utf-8"))
    strip_source_map = next(row for row in source_map["source_map"] if row["artifact_path"] == "gfx/interface/buildings/building_icon_strip.dds")
    assert strip_source_map["owner"] == "project:test_building_icons"
    assert [source["module_id"] for source in strip_source_map["sources"]] == [
        "building/alpha",
        "building/beta",
    ]
    original_mtimes = {relative_path: (project.output_root / relative_path).stat().st_mtime_ns for relative_path in postprocessed_paths}

    cached_result = project.build(module_id="building/alpha", emit_artifacts=True, emit_manifests=True)

    assert cached_result.blocked is False
    assert cached_result.summary() == plan.summary()
    assert {relative_path: (project.output_root / relative_path).stat().st_mtime_ns for relative_path in postprocessed_paths} == original_mtimes

    (module_root / "def.txt").write_text(
        "buildings = { alpha = { icon_frame = 99 max_level = 3 } }\n",
        encoding="utf-8",
    )
    changed_result = project.build(module_id="building/alpha", emit_artifacts=True, emit_manifests=True)
    changed_building = project.output_root / "common/buildings/alpha.txt"

    assert changed_result.blocked is False
    assert changed_building.stat().st_mtime_ns != original_mtimes["common/buildings/alpha.txt"]
    assert "icon_frame = 1" in changed_building.read_text(encoding="utf-8")
    assert "max_level = 3" in changed_building.read_text(encoding="utf-8")


def test_hoi4_profile_validates_idea_game_id_override(tmp_path: Path) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/GER_legacy_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\ngame_id: GER_industry_spirit\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = { country = { GER_industry_spirit = { picture = GER_industry_spirit } } }",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  GER_industry_spirit: German Industry Spirit\n  GER_industry_spirit_desc: Industrial production spirit.\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(b"sample idea icon bytes")
    project = Project.load(tmp_path)

    result = project.build()

    idea_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "module:idea/GER_legacy_spirit"]
    assert result.blocked is False
    assert result.diagnostics == ()
    assert [artifact.path for artifact in idea_artifacts] == [
        "common/ideas/GER_industry_spirit.txt",
        "gfx/interface/ideas/idea_GER_industry_spirit.dds",
        "localisation/english/GER_industry_spirit_l_english.yml",
    ]
    assert [artifact.path for artifact in result.artifacts if artifact.artifact_type == "sprite_gfx"] == ["interface/paradev_idea.gfx"]


def test_hoi4_profile_blocks_idea_id_mismatch(tmp_path: Path) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "\n".join(
            [
                "ideas = {",
                "\tcountry = {",
                "\t\tFRA_industry_spirit = { picture = GER_industry_spirit }",
                "\t}",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="idea.id_mismatch",
        message="Idea id FRA_industry_spirit must match module object id GER_industry_spirit.",
        severity="error",
        family="idea",
        module_id="idea/GER_industry_spirit",
        source_path="def.txt",
        span={"line": 3, "column": 3},
    )
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_blocks_missing_idea_localization_keys(tmp_path: Path) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = {\n\tcountry = {\n\t\tGER_industry_spirit = { picture = GER_industry_spirit }\n\t}\n}",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text("en:\n  GER_industry_spirit: German Industry Spirit\n", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="idea.missing_localization",
        message="Idea GER_industry_spirit missing localization key 'GER_industry_spirit_desc' for l_english.",
        severity="error",
        family="idea",
        module_id="idea/GER_industry_spirit",
        slot="loc",
        source_path="def.txt",
        span={"line": 3, "column": 3},
    )
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_blocks_idea_icon_picture_mismatch(tmp_path: Path) -> None:
    _write_project(tmp_path)
    idea_root = tmp_path / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = {\n\tcountry = {\n\t\tGER_industry_spirit = { picture = GER_wrong }\n\t}\n}",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  GER_industry_spirit: German Industry Spirit\n  GER_industry_spirit_desc: Industrial production spirit.\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(b"sample idea icon bytes")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="idea.picture_mismatch",
        message="Idea GER_industry_spirit picture GER_wrong must match icon object id GER_industry_spirit.",
        severity="error",
        family="idea",
        module_id="idea/GER_industry_spirit",
        source_path="def.txt",
        span={"line": 3, "column": 37},
    )


def test_hoi4_profile_plans_simple_opinion_modifier_artifacts(tmp_path: Path) -> None:
    _write_project(tmp_path)
    opinion_root = tmp_path / "src/modules/opinion_modifier/OPINION_SAMPLE"
    opinion_root.mkdir(parents=True)
    (opinion_root / "def.txt").write_text("OPINION_SAMPLE = { value = 10 }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    opinion_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "module:opinion_modifier/OPINION_SAMPLE"]
    assert result.blocked is False
    assert [artifact.path for artifact in opinion_artifacts] == ["common/opinion_modifiers/OPINION_SAMPLE.txt"]
    assert opinion_artifacts[0].artifact_type == "pdx"


def test_hoi4_profile_routes_trait_artifacts_by_subtype(tmp_path: Path) -> None:
    _write_project(tmp_path)
    country_root = tmp_path / "src/modules/trait/TRAIT_COUNTRY"
    unit_root = tmp_path / "src/modules/trait/TRAIT_UNIT"
    scientist_root = tmp_path / "src/modules/trait/TRAIT_SCIENTIST"
    country_root.mkdir(parents=True)
    unit_root.mkdir(parents=True)
    scientist_root.mkdir(parents=True)
    (country_root / "meta.yaml").write_text("type: trait\nsettings:\n  subtype: country_leader\n", encoding="utf-8")
    (country_root / "def.txt").write_text("TRAIT_COUNTRY = { random = no }", encoding="utf-8")
    (unit_root / "meta.yaml").write_text("type: trait\nsettings:\n  subtype: unit_leader\n", encoding="utf-8")
    (unit_root / "def.txt").write_text("TRAIT_UNIT = { attack_skill = 1 }", encoding="utf-8")
    (scientist_root / "meta.yaml").write_text("type: trait\nsettings:\n  subtype: scientist\n", encoding="utf-8")
    (scientist_root / "def.txt").write_text("TRAIT_SCIENTIST = { random = no }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    trait_artifacts = [artifact for artifact in result.artifacts if artifact.owner.startswith("module:trait/")]
    assert result.blocked is False
    assert [artifact.path for artifact in trait_artifacts] == [
        "common/country_leader/TRAIT_COUNTRY.txt",
        "common/scientist_traits/TRAIT_SCIENTIST.txt",
        "common/unit_leader/TRAIT_UNIT.txt",
    ]
    assert [artifact.artifact_type for artifact in trait_artifacts] == [
        "pdx",
        "pdx",
        "pdx",
    ]


def test_hoi4_profile_plans_event_namespace_collection_artifacts(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    first_root = tmp_path / "src/modules/event/GER_news_1"
    second_root = tmp_path / "src/modules/event/GER_news_2"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (second_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (first_root / "def.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (second_root / "def.txt").write_text("country_event = { id = germany.2 }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    event_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "collection:germany"]
    assert result.blocked is False
    assert [artifact.path for artifact in event_artifacts] == ["events/germany.txt"]
    assert event_artifacts[0].metadata == {
        "module_ids": ["event/GER_news_1", "event/GER_news_2"],
        "family": "event",
        "collection_id": "germany",
    }


def test_hoi4_profile_blocks_duplicate_event_ids(tmp_path: Path) -> None:
    _write_project(tmp_path)
    first_root = tmp_path / "src/modules/event/GER_news_1"
    second_root = tmp_path / "src/modules/event/GER_news_2"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (second_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (first_root / "def.txt").write_text("country_event = {\n\tid = germany.1\n}", encoding="utf-8")
    (second_root / "def.txt").write_text("country_event = {\n\tid = germany.1\n}", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="event.duplicate_id",
        message="Event id germany.1 is declared by event/GER_news_1 and event/GER_news_2.",
        severity="error",
        family="event",
        module_id="event/GER_news_2",
        source_path="def.txt",
        span={"line": 2, "column": 7},
    )


def test_hoi4_profile_blocks_duplicate_event_ids_inside_one_source(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    event_root = tmp_path / "src/modules/event/GER_news"
    event_root.mkdir(parents=True)
    (event_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (event_root / "def.txt").write_text(
        "country_event = {\n\tid = germany.1\n}\ncountry_event = {\n\tid = germany.1\n}",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="event.duplicate_id",
        message="Event id germany.1 is declared by event/GER_news and event/GER_news.",
        severity="error",
        family="event",
        module_id="event/GER_news",
        source_path="def.txt",
        span={"line": 5, "column": 7},
    )


def test_hoi4_profile_blocks_event_namespace_mismatch(tmp_path: Path) -> None:
    _write_project(tmp_path)
    event_root = tmp_path / "src/modules/event/GER_news"
    event_root.mkdir(parents=True)
    (event_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (event_root / "def.txt").write_text("country_event = {\n\tid = france.1\n}", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="event.namespace_mismatch",
        message="Event id france.1 must use namespace germany.",
        severity="error",
        family="event",
        module_id="event/GER_news",
        source_path="def.txt",
        span={"line": 2, "column": 7},
    )


def test_hoi4_profile_merges_decisions_into_category_artifact(tmp_path: Path) -> None:
    _write_project(tmp_path)
    first_root = tmp_path / "src/modules/decision/GER_decision_one"
    second_root = tmp_path / "src/modules/decision/GER_decision_two"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (second_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (first_root / "def.txt").write_text(
        "GER_industry = { GER_decision_one = { icon = generic_political_reform } }",
        encoding="utf-8",
    )
    (second_root / "def.txt").write_text(
        "GER_industry = { GER_decision_two = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    decision_artifacts = [artifact for artifact in result.artifacts if artifact.owner == "collection:GER_industry"]
    assert result.blocked is False
    assert [artifact.path for artifact in decision_artifacts] == ["common/decisions/GER_industry.txt"]
    assert decision_artifacts[0].metadata == {
        "module_ids": ["decision/GER_decision_one", "decision/GER_decision_two"],
        "family": "decision",
        "collection_id": "GER_industry",
    }
    assert (project.output_root / "common/decisions/GER_industry.txt").read_text(encoding="utf-8") == (
        "GER_industry = {\n"
        "\tGER_decision_one = {\n"
        "\t\ticon = generic_political_reform\n"
        "\t}\n"
        "\tGER_decision_two = {\n"
        "\t\ticon = generic_industry\n"
        "\t}\n"
        "}\n"
    )


def test_hoi4_profile_blocks_decision_category_mismatch(tmp_path: Path) -> None:
    _write_project(tmp_path)
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    decision_root.mkdir(parents=True)
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "FRA_industry = {\n\tGER_decision = { icon = generic_industry }\n}",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.category_mismatch",
        message="Decision category FRA_industry must match collection GER_industry.",
        severity="error",
        family="decision",
        module_id="decision/GER_decision",
        source_path="def.txt",
        span={"line": 1, "column": 1},
    )


def test_hoi4_profile_blocks_duplicate_decision_ids(tmp_path: Path) -> None:
    _write_project(tmp_path)
    first_root = tmp_path / "src/modules/decision/GER_decision_one"
    second_root = tmp_path / "src/modules/decision/GER_decision_two"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (second_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (first_root / "def.txt").write_text(
        "GER_industry = {\n\tGER_shared_decision = { icon = generic_political_reform }\n}",
        encoding="utf-8",
    )
    (second_root / "def.txt").write_text(
        "GER_industry = {\n\tGER_shared_decision = { icon = generic_industry }\n}",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.duplicate_id",
        message="Decision id GER_shared_decision is declared by decision/GER_decision_one and decision/GER_decision_two.",
        severity="error",
        family="decision",
        module_id="decision/GER_decision_two",
        source_path="def.txt",
        span={"line": 2, "column": 2},
    )


def test_hoi4_profile_blocks_duplicate_decision_ids_inside_one_source(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    decision_root.mkdir(parents=True)
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "\n".join(
            [
                "GER_industry = {",
                "\tGER_shared_decision = { icon = generic_political_reform }",
                "\tGER_shared_decision = { icon = generic_industry }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.duplicate_id",
        message="Decision id GER_shared_decision is declared by decision/GER_decision and decision/GER_decision.",
        severity="error",
        family="decision",
        module_id="decision/GER_decision",
        source_path="def.txt",
        span={"line": 3, "column": 2},
    )


def test_hoi4_profile_emits_decision_category_descriptor_artifact(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    collection_root = tmp_path / "src/collections/decision/GER_industry"
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    collection_root.mkdir(parents=True)
    decision_root.mkdir(parents=True)
    (collection_root / "def.txt").write_text(
        "GER_industry = { icon = generic_industry picture = GFX_decision_cat_picture_generic }",
        encoding="utf-8",
    )
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    assert result.blocked is False
    assert result.diagnostics == ()
    assert [artifact.path for artifact in result.artifacts if artifact.owner == "collection:GER_industry"] == [
        "common/decisions/categories/GER_industry.txt",
        "common/decisions/GER_industry.txt",
    ]
    assert (project.output_root / "common/decisions/categories/GER_industry.txt").read_text(encoding="utf-8") == (
        "GER_industry = {\n" "\ticon = generic_industry\n" "\tpicture = GFX_decision_cat_picture_generic\n" "}\n"
    )


def test_hoi4_profile_blocks_decision_category_descriptor_mismatch(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    collection_root = tmp_path / "src/collections/decision/GER_industry"
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    collection_root.mkdir(parents=True)
    decision_root.mkdir(parents=True)
    (collection_root / "def.txt").write_text(
        "FRA_industry = {\n\ticon = generic_industry\n}",
        encoding="utf-8",
    )
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.category_descriptor_mismatch",
        message="Decision category descriptor FRA_industry must match collection GER_industry.",
        severity="error",
        family="decision",
        collection_id="GER_industry",
        source_path="def.txt",
        span={"line": 1, "column": 1},
    )


def test_hoi4_profile_emits_decision_category_localization_artifact(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    collection_root = tmp_path / "src/collections/decision/GER_industry"
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    collection_root.mkdir(parents=True)
    decision_root.mkdir(parents=True)
    (collection_root / "main.loc").write_text(
        "\n".join(
            [
                "en:",
                "  GER_industry: German Industry",
                "  GER_industry_desc: Industrial mobilization decisions.",
            ]
        ),
        encoding="utf-8",
    )
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    assert result.blocked is False
    assert result.diagnostics == ()
    assert [artifact.path for artifact in result.artifacts if artifact.owner == "collection:GER_industry"] == [
        "common/decisions/GER_industry.txt",
        "localisation/english/GER_industry_l_english.yml",
    ]
    assert (project.output_root / "localisation/english/GER_industry_l_english.yml").read_text(encoding="utf-8-sig") == (
        'l_english:\n GER_industry:0 "German Industry"\n GER_industry_desc:0 "Industrial mobilization decisions."\n'
    )


def test_hoi4_profile_blocks_missing_decision_category_localization_keys(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    collection_root = tmp_path / "src/collections/decision/GER_industry"
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    collection_root.mkdir(parents=True)
    decision_root.mkdir(parents=True)
    (collection_root / "main.loc").write_text("en:\n  GER_industry: German Industry\n", encoding="utf-8")
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    payload = project.diagnostics(
        collection_id="GER_industry",
        severity="error",
        code="decision.category_missing_localization",
        source_path="main.loc",
        slot="loc",
    )
    empty_payload = project.diagnostics(
        collection_id="FRA_industry",
        severity="error",
        code="decision.category_missing_localization",
    )
    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.category_missing_localization",
        message="Decision category GER_industry missing localization key 'GER_industry_desc' for l_english.",
        severity="error",
        family="decision",
        collection_id="GER_industry",
        slot="loc",
        source_path="main.loc",
    )
    assert payload["diagnostics"] == [
        {
            "code": "decision.category_missing_localization",
            "message": "Decision category GER_industry missing localization key 'GER_industry_desc' for l_english.",
            "severity": "error",
            "family": "decision",
            "collection_id": "GER_industry",
            "slot": "loc",
            "source_path": "main.loc",
            "source": {
                "path": str(collection_root / "main.loc"),
                "collection_id": "GER_industry",
                "family": "decision",
                "slot": "loc",
            },
        }
    ]
    assert payload["index"] == {"error": {"decision.category_missing_localization": [0]}}
    assert empty_payload["diagnostics"] == []
    assert empty_payload["index"] == {}
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_manifest_includes_decision_category_localization_rows(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    collection_root = tmp_path / "src/collections/decision/GER_industry"
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    collection_root.mkdir(parents=True)
    decision_root.mkdir(parents=True)
    (collection_root / "main.loc").write_text(
        "\n".join(
            [
                "en:",
                "  GER_industry: German Industry",
                "  GER_industry_desc: Industrial mobilization decisions.",
            ]
        ),
        encoding="utf-8",
    )
    (decision_root / "meta.yaml").write_text("type: decision\ncollection: GER_industry\n", encoding="utf-8")
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    sdk_localization = project.localization(collection_id="GER_industry")
    empty_localization = project.localization(collection_id="FRA_industry")
    project.build(emit_manifests=True)

    localization = json.loads((project.build_root / "localization.json").read_text(encoding="utf-8"))
    source_map = json.loads((project.build_root / "source-map.json").read_text(encoding="utf-8"))
    assert [row["key"] for row in sdk_localization["localization"]] == [
        "GER_industry",
        "GER_industry_desc",
    ]
    assert sdk_localization["localization"][0]["collection_id"] == "GER_industry"
    assert sdk_localization["index"] == {
        "l_english": {
            "GER_industry": {"duplicate": False, "rows": [0]},
            "GER_industry_desc": {"duplicate": False, "rows": [1]},
        }
    }
    assert empty_localization["localization"] == []
    assert empty_localization["index"] == {}
    assert localization["localization"] == [
        {
            "collection_id": "GER_industry",
            "family": "decision",
            "language": "l_english",
            "key": "GER_industry",
            "text": "German Industry",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "collection_id": "GER_industry",
                "family": "decision",
                "path": str(collection_root / "main.loc"),
                "slot": "loc",
            },
        },
        {
            "collection_id": "GER_industry",
            "family": "decision",
            "language": "l_english",
            "key": "GER_industry_desc",
            "text": "Industrial mobilization decisions.",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "collection_id": "GER_industry",
                "family": "decision",
                "path": str(collection_root / "main.loc"),
                "slot": "loc",
            },
        },
    ]
    assert localization["index"] == {
        "l_english": {
            "GER_industry": {"duplicate": False, "rows": [0]},
            "GER_industry_desc": {"duplicate": False, "rows": [1]},
        }
    }
    assert next(row for row in source_map["source_map"] if row["artifact_path"] == "localisation/english/GER_industry_l_english.yml") == {
        "artifact_path": "localisation/english/GER_industry_l_english.yml",
        "type": "loc",
        "target_root": "output",
        "owner": "collection:GER_industry",
        "inputs": [str(collection_root / "main.loc")],
        "sources": [
            {
                "path": str(collection_root / "main.loc"),
                "collection_id": "GER_industry",
                "family": "decision",
                "slot": "loc",
            }
        ],
    }


def test_hoi4_profile_blocks_decision_without_collection(tmp_path: Path) -> None:
    _write_project(tmp_path)
    decision_root = tmp_path / "src/modules/decision/GER_decision"
    decision_root.mkdir(parents=True)
    (decision_root / "def.txt").write_text(
        "GER_industry = { GER_decision = { icon = generic_industry } }",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0] == Diagnostic(
        code="decision.missing_collection",
        message="Decision module decision/GER_decision must set collection to the decision category id.",
        severity="error",
        family="decision",
        module_id="decision/GER_decision",
        source_path="meta.yaml",
    )


def test_hoi4_profile_blocks_trait_without_subtype(tmp_path: Path) -> None:
    _write_project(tmp_path)
    trait_root = tmp_path / "src/modules/trait/TRAIT_SAMPLE"
    trait_root.mkdir(parents=True)
    (trait_root / "meta.yaml").write_text("type: trait\n", encoding="utf-8")
    (trait_root / "def.txt").write_text("TRAIT_SAMPLE = { random = no }", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0].code == "family.missing_route"
    assert result.diagnostics[0].module_id == "trait/TRAIT_SAMPLE"
    assert result.diagnostics[0].source_path == "meta.yaml"
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_preserves_static_copy_relative_paths(tmp_path: Path) -> None:
    _write_project(tmp_path)
    module_root = tmp_path / "src/modules/focus/GER_sample"
    (module_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    asset_payload = b"asset icon bytes"
    copy_payload = b"copy icon bytes"
    (module_root / "assets/interface").mkdir(parents=True)
    (module_root / "copy/interface").mkdir(parents=True)
    (module_root / "assets/interface/icon.png").write_bytes(asset_payload)
    (module_root / "copy/interface/icon.png").write_bytes(copy_payload)
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True, emit_manifests=True)

    copy_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "copy"]
    asset_manifest = json.loads((project.build_root / "assets.json").read_text(encoding="utf-8"))
    asset_target = project.output_root / "gfx/paradev/GER_sample/assets/interface/icon.png"
    copy_target = project.output_root / "gfx/paradev/GER_sample/copy/interface/icon.png"
    assert result.dry_run is False
    assert result.blocked is False
    assert [artifact.path for artifact in copy_artifacts] == [
        "gfx/paradev/GER_sample/assets/interface/icon.png",
        "gfx/paradev/GER_sample/copy/interface/icon.png",
    ]
    assert [artifact.inputs for artifact in copy_artifacts] == [
        (module_root / "assets/interface/icon.png",),
        (module_root / "copy/interface/icon.png",),
    ]
    assert [artifact.metadata["sha256"] for artifact in copy_artifacts] == [
        sha256hash(asset_payload),
        sha256hash(copy_payload),
    ]
    assert asset_target.read_bytes() == asset_payload
    assert copy_target.read_bytes() == copy_payload
    assert asset_manifest == {
        "schema": "paradev.build.assets.v1",
        "project_id": "test_build",
        "profile": "hoi4",
        "assets": [
            {
                "module_id": "focus/GER_sample",
                "family": "focus",
                "slot": "assets",
                "source_path": "assets/interface/icon.png",
                "artifact_path": "gfx/paradev/GER_sample/assets/interface/icon.png",
                "owner": "module:focus/GER_sample",
                "target_root": "output",
                "sha256": sha256hash(asset_payload),
                "size": len(asset_payload),
                "source": {
                    "path": str(module_root / "assets/interface/icon.png"),
                    "module_id": "focus/GER_sample",
                    "family": "focus",
                    "slot": "assets",
                },
            },
            {
                "module_id": "focus/GER_sample",
                "family": "focus",
                "slot": "copy",
                "source_path": "copy/interface/icon.png",
                "artifact_path": "gfx/paradev/GER_sample/copy/interface/icon.png",
                "owner": "module:focus/GER_sample",
                "target_root": "output",
                "sha256": sha256hash(copy_payload),
                "size": len(copy_payload),
                "source": {
                    "path": str(module_root / "copy/interface/icon.png"),
                    "module_id": "focus/GER_sample",
                    "family": "focus",
                    "slot": "copy",
                },
            },
        ],
        "index": {"focus/GER_sample": {"assets": [0], "copy": [1]}},
    }


def test_hoi4_profile_emits_each_localization_language(tmp_path: Path) -> None:
    _write_project(tmp_path)
    module_root = tmp_path / "src/modules/focus/GER_sample"
    (module_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    (module_root / "main.loc").write_text(
        "\n".join(
            [
                "en:",
                "  GER_sample: Sample Focus",
                "  GER_sample_desc: Sample description",
                "fr:",
                "  GER_sample: Exemple",
                "  GER_sample_desc: Description exemple",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build(emit_artifacts=True)

    loc_artifacts = [artifact for artifact in result.artifacts if artifact.artifact_type == "loc"]
    assert result.dry_run is False
    assert result.blocked is False
    assert [artifact.path for artifact in loc_artifacts] == [
        "localisation/english/GER_sample_l_english.yml",
        "localisation/french/GER_sample_l_french.yml",
    ]
    assert (project.output_root / "localisation/english/GER_sample_l_english.yml").read_text(encoding="utf-8-sig") == (
        'l_english:\n GER_sample:0 "Sample Focus"\n GER_sample_desc:0 "Sample description"\n'
    )
    assert (project.output_root / "localisation/french/GER_sample_l_french.yml").read_text(encoding="utf-8-sig") == (
        'l_french:\n GER_sample:0 "Exemple"\n GER_sample_desc:0 "Description exemple"\n'
    )


def test_hoi4_profile_blocks_missing_focus_localization_keys(tmp_path: Path) -> None:
    _write_project(tmp_path)
    module_root = tmp_path / "src/modules/focus/GER_sample"
    (module_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    (module_root / "main.loc").write_text("en:\n  GER_sample: Sample Focus\n", encoding="utf-8")
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0].code == "focus.missing_localization"
    assert result.diagnostics[0].module_id == "focus/GER_sample"
    assert result.diagnostics[0].slot == "loc"
    assert result.diagnostics[0].source_path == "def.txt"
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_blocks_duplicate_canonical_localization_keys(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    module_root = tmp_path / "src/modules/focus/GER_sample"
    (module_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    (module_root / "main.loc").write_text(
        "\n".join(
            [
                "en:",
                "  GER_sample: Sample Focus",
            ]
        ),
        encoding="utf-8",
    )
    (module_root / "extra.loc").write_text(
        "\n".join(
            [
                "l_english:",
                "  GER_sample: Duplicate Focus",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert result.diagnostics[0].code == "loc.duplicate_key"
    assert result.diagnostics[0].module_id == "focus/GER_sample"
    assert result.diagnostics[0].slot == "loc"
    assert result.diagnostics[0].source_path == "main.loc"
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_blocks_project_level_duplicate_localization_keys(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    first_root = tmp_path / "src/modules/focus/GER_sample"
    second_root = tmp_path / "src/modules/focus/GER_extra"
    first_root.mkdir(parents=True, exist_ok=True)
    second_root.mkdir(parents=True)
    (first_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    (first_root / "main.loc").write_text(
        "\n".join(
            [
                "en:",
                "  GER_sample: Sample Focus",
                "  GER_sample_desc: Sample description",
                "  GER_shared: First Focus",
            ]
        ),
        encoding="utf-8",
    )
    (second_root / "meta.yaml").write_text("type: focus\ncollection: GER_main\n", encoding="utf-8")
    (second_root / "def.txt").write_text("focus = { id = GER_extra }", encoding="utf-8")
    (second_root / "main.loc").write_text(
        "\n".join(
            [
                "l_english:",
                "  GER_extra: Extra Focus",
                "  GER_extra_desc: Extra description",
                "  GER_shared: Second Focus",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    result = project.build()

    assert result.blocked is True
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["loc.project_duplicate_key"]
    assert result.diagnostics[0].module_id == "focus/GER_sample"
    assert result.diagnostics[0].slot == "loc"
    assert result.diagnostics[0].source_path == "main.loc"
    blocked_emit = project.build(emit_artifacts=True)
    assert blocked_emit.blocked is True
    assert blocked_emit.dry_run is True


def test_hoi4_profile_registers_view_artifact_writer() -> None:
    registry = registry_for_profile("hoi4")

    assert registry.writer("view").artifact_type == "view"


def test_build_cli_outputs_dry_run_json(tmp_path: Path) -> None:
    _write_project(tmp_path)

    result = CliRunner().invoke(build_app(), ["build", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project_id"] == "test_build"
    assert payload["dry_run"] is True
    assert payload["summary"]["artifact_count"] == 3
    assert [row["path"] for row in payload["artifacts"] if row["type"] != "mod_descriptor"] == ["common/national_focus/GER_sample.txt"]
    assert [row["path"] for row in payload["artifacts"] if row["type"] == "mod_descriptor"] == [
        "descriptor.mod",
        "launcher/test_build.mod",
    ]
    assert payload["modules"][0]["module_id"] == "focus/GER_sample"
    assert payload["summary"]["blocked"] is False


def test_build_cli_can_emit_project_artifacts_without_launcher_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    mod_root = tmp_path / "hoi4-mod"
    mod_root.mkdir()
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    launcher = mod_root / "test_build.mod"
    launcher.write_text("USER LAUNCHER DATA\n", encoding="utf-8")

    result = CliRunner().invoke(
        build_app(),
        [
            "build",
            str(tmp_path),
            "--emit-artifacts",
            "--emit-manifests",
            "--no-sync-launcher-descriptor",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["summary"]["blocked"] is False
    assert launcher.read_text(encoding="utf-8") == "USER LAUNCHER DATA\n"
    assert (Project.load(tmp_path).output_root / "descriptor.mod").is_file()
