from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from struct import pack

import pytest
import yaml
from heavenbase.utils import copy_dir, delete_dir, sha256hash
from typer.testing import CliRunner

import paradev.hb as paradev_hb
import paradev.sdk._source_recovery as source_recovery_sdk
import paradev.sdk.project as project_sdk
import paradev.sdk.templates as templates_sdk
from paradev.build import (
    Artifact,
    BuildContext,
    BuildRegistry,
    BuildResult,
    Collection,
    CollectionSourceFamily,
    Module,
    SimpleSourceFamily,
    Slot,
)
from paradev.cli import build_app
from paradev.config import CM_PARADEV
from paradev.sdk import (
    Project,
    ProjectManifestError,
    desktop_state,
    open_project,
    registered_projects,
)
from paradev.sdk.templates import (
    ModuleTemplate,
    TemplateArg,
    TemplateFile,
    module_scaffold_plan,
)

PROJECT_ROOT = Path("demos/assets/projects/minimal").resolve()
NESTED_SOURCE = PROJECT_ROOT / "src/modules/focus/GER_sample/def.txt"
ONE_PIXEL_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _imagemagick_converter_available() -> bool:
    if shutil.which("magick") is not None:
        return True
    if project_sdk.sys.platform == "win32":
        return False
    return project_sdk._legacy_convert_is_imagemagick()


def _install_source_unlink_crash(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    *,
    occurrence: int = 1,
) -> Callable[..., None]:
    """Raise an abrupt-exit signal after unlinking one retained source name."""

    unlink = project_sdk.os.unlink
    matched = 0

    def crash_after_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal matched
        if os.fspath(path) == source.name and kwargs.get("dir_fd") is not None and matched < occurrence:
            matched += 1
            unlink(path, *args, **kwargs)
            if matched == occurrence:
                raise SystemExit("simulated abrupt displacement exit")
            return
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(project_sdk.os, "unlink", crash_after_unlink)
    return unlink


def _default_source_slots_view() -> list[dict[str, object]]:
    return [
        {
            "name": "def",
            "match": "def.txt",
            "required": False,
            "many": False,
            "regex": False,
            "kind": "pdx",
        },
        {
            "name": "loc",
            "match": "**/*.loc",
            "required": False,
            "many": True,
            "regex": False,
            "kind": "loc",
        },
        {
            "name": "icon",
            "match": r"^(icon|goal|portrait|picture)\.(png|dds|tga)$",
            "required": False,
            "many": False,
            "regex": True,
            "kind": "copy",
        },
        {
            "name": "copy",
            "match": "copy/*",
            "required": False,
            "many": True,
            "regex": False,
            "kind": "copy",
        },
        {
            "name": "assets",
            "match": "assets/*",
            "required": False,
            "many": True,
            "regex": False,
            "kind": "copy",
        },
    ]


def test_project_load_merges_hidden_system_manifest_with_user_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            (
                "project_id: layered",
                "title: Layered Project",
                "game: hoi4",
                "source_roots: [src]",
                "supported_version: 1.20.*",
                "",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / ".paradev.yaml").write_text(
        "\n".join(
            (
                "build_root: .paradev/cache/build",
                "supported_version: 1.19.*",
                "replace_path:",
                "  - common/example",
                "",
            )
        ),
        encoding="utf-8",
    )

    project = Project.load(tmp_path)

    assert project.build_root == (tmp_path / ".paradev/cache/build").resolve()
    assert project.descriptor_metadata["supported_version"] == "1.20.*"
    assert project.descriptor_metadata["replace_path"] == ["common/example"]


def _idea_source_slots_view() -> list[dict[str, object]]:
    return [
        {
            "name": "def",
            "match": "def.txt",
            "required": False,
            "many": False,
            "regex": False,
            "kind": "pdx",
        },
        {
            "name": "loc",
            "match": "**/*.loc",
            "required": False,
            "many": True,
            "regex": False,
            "kind": "loc",
        },
        {
            "name": "icon",
            "match": "icon.png",
            "required": False,
            "many": False,
            "regex": False,
            "kind": "copy",
        },
        {
            "name": "icon",
            "match": r"^icon\.(png|dds|tga)$",
            "required": False,
            "many": False,
            "regex": True,
            "kind": "copy",
        },
    ]


def _modifier_source_slots_view() -> list[dict[str, object]]:
    return [
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
            "match": "**/*.loc",
            "required": False,
            "many": True,
            "regex": False,
            "kind": "loc",
        },
        {
            "name": "assets",
            "match": r"^(gfx/interface/modifiers/.+\.dds|interface/modifiers/.+\.gfx)$",
            "required": False,
            "many": True,
            "regex": True,
            "kind": "copy",
        },
    ]


def _metadata_contract(
    *extra_keys: str,
    settings: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    keys = [
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
        "keys": sorted({*keys, *extra_keys}),
        "common_keys": keys,
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


def _localization_contract() -> dict[str, object]:
    return {
        "required_keys": ["{object_id}", "{object_id}_desc"],
        "required_when": "loc_authored",
    }


def _asset_contract() -> dict[str, object]:
    return {
        "slots": {
            "icon": {
                "formats": ["dds", "png"],
                "width": 64,
                "height": 64,
            }
        }
    }


def _idea_default_asset() -> dict[str, object]:
    return {
        "slot": "icon",
        "asset_id": "hoi4:idea/default_icon",
        "title": "Default HoI4 idea icon",
        "source": "builtin",
        "path": "hoi4:idea/default_icon",
        "editable": True,
        "injected": False,
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


def _source_family_stages() -> list[str]:
    return ["discover", "load", "normalize", "check", "emit"]


def _collection_family_stages() -> list[str]:
    return ["discover", "load", "normalize", "aggregate", "check", "emit"]


class ExplainFailingNormalizeFamily:
    family = "notice"

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
    ) -> tuple[object, ...]:
        raise AssertionError("emit should not run after normalize failure")


class ExplainArtifactCollisionFamily:
    family = "collision"

    def emit(
        self,
        ctx: BuildContext,
        modules: tuple[Module, ...],
        collections: tuple[Collection, ...],
    ) -> tuple[Artifact, ...]:
        return (
            Artifact(
                path="views/shared.json",
                artifact_type="view",
                owner="module:collision/output_a",
            ),
            Artifact(
                path="views/shared.json",
                artifact_type="view",
                owner="module:collision/output_b",
            ),
            Artifact(
                path="views/shared.json",
                artifact_type="view",
                owner="project:diagnostic_explain",
                target_root="build",
            ),
        )


def _write_notice_project(root: Path) -> None:
    module_root = root / "src/modules/notice/ALERT"
    module_root.mkdir(parents=True)
    (module_root / "def.txt").write_text("notice = { id = ALERT }", encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: diagnostic_explain",
                "title: Diagnostic Explain",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )


def _write_collection_slot_project(root: Path) -> None:
    module_root = root / "src/modules/bulletin/GER_news"
    collection_root = root / "src/collections/bulletin/germany"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "body.txt").write_text("bulletin = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: bulletin\ncollection: germany\n", encoding="utf-8")
    (collection_root / "category.txt").write_text("add_namespace = germany", encoding="utf-8")
    (collection_root / "strings.yml").write_text("en:\n  germany: Germany Events\n", encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: collection_slot_project",
                "title: Collection Slot Project",
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
                "      - name: category",
                "        match: category.txt",
                "        kind: pdx",
                "      - name: strings",
                "        match: strings.yml",
                "        kind: loc",
                "    templates:",
                "      pdx: common/bulletins/{collection_id}.txt",
            ]
        ),
        encoding="utf-8",
    )


def _write_required_slot_project(root: Path) -> None:
    module_root = root / "src/modules/badge/GER_missing_icon"
    module_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: badge\ntitle: Missing Icon Badge\n", encoding="utf-8")
    (module_root / "body.txt").write_text("badge = { id = GER_missing_icon }", encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: required_slot_project",
                "title: Required Slot Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: body.txt",
                "        required: true",
                "        kind: pdx",
                "      - name: icon",
                "        match: icon.png",
                "        required: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/badges/{object_id}.txt",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "templates:",
                "  badge/basic:",
                "    title: Basic Badge",
                "    family: badge",
                "    files:",
                "      meta.yaml: 'type: badge'",
                "      body.txt: 'badge = {{ id = {object_id} }}'",
            ]
        ),
        encoding="utf-8",
    )


def _write_repeated_slot_project(root: Path) -> None:
    module_root = root / "src/modules/notice/ALERT"
    (module_root / "loc").mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: notice\ntitle: Alert\n", encoding="utf-8")
    (module_root / "main.loc").write_text("en:\n  ALERT: Alert\n", encoding="utf-8")
    (module_root / "loc/english.loc").write_text("en:\n  ALERT_desc: Alert description.\n", encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: repeated_slot_project",
                "title: Repeated Slot Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  notice:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: loc",
                "        match: main.loc",
                "        many: true",
                "        kind: loc",
                "      - name: loc",
                "        match: loc/*.loc",
                "        many: true",
                "        kind: loc",
                "    templates:",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )


def _write_pihc3_batch_authoring_project(root: Path) -> None:
    for object_id, title in (
        ("TECHNOLOGY_AIR_CLOUDSHIP", "Cloudship Design"),
        ("TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR", "Solar Fixed Wing"),
    ):
        module_root = root / f"src/modules/technology/{object_id}"
        module_root.mkdir(parents=True)
        (module_root / "meta.yaml").write_text(f"type: technology\ntitle: {title}\n", encoding="utf-8")
        (module_root / "main.loc").write_text(f'l_english:\n  {object_id}: "{title}"\n', encoding="utf-8")
    (root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: pihc3_batch_authoring",
                "title: PIHC3 Batch Authoring",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )


def _write_notice_python_project(root: Path) -> None:
    _write_notice_project(root)
    plugin_root = root / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "class FailingNormalizeFamily:",
                "    family = 'notice'",
                "",
                "    def normalize(self, ctx, modules, collections):",
                "        raise RuntimeError('metadata schema unavailable')",
                "",
                "    def emit(self, ctx, modules, collections):",
                "        raise AssertionError('emit should not run after normalize failure')",
                "",
                "",
                "def register(registry):",
                "    registry.add(FailingNormalizeFamily())",
            ]
        ),
        encoding="utf-8",
    )
    manifest = root / "paradev.yaml"
    manifest.write_text(
        f"{manifest.read_text(encoding='utf-8')}\npython_modules:\n  - tools/families.py\n",
        encoding="utf-8",
    )


def test_project_load_reads_manifest_from_root() -> None:
    project = Project.load(PROJECT_ROOT)
    view = project.to_view()

    assert project.root == PROJECT_ROOT
    assert project.project_id == "minimal_hoi4"
    assert project.title == "Minimal HOI4 Project"
    assert project.game == "hoi4"
    assert project.preferred_language == "en"
    assert view["preferred_language"] == "en"
    assert view["source_roots"] == [str(PROJECT_ROOT / "src")]
    assert view["output_root"] == str(PROJECT_ROOT / "build/mod")
    assert view["build_root"] == str(PROJECT_ROOT / ".paradev/.cache/build")


def test_project_preferred_language_resolves_template_defaults_and_explicit_values(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "chinese-authoring")
    project.manifest_path.write_text(
        f"{project.manifest_path.read_text(encoding='utf-8')}preferred_language: zh\n",
        encoding="utf-8",
    )
    project = Project.load(project.root)

    catalog = project.templates(template_id="hoi4:idea/basic")
    template = catalog["templates"][0]
    default_plan = project.create_module(
        "idea",
        "IDEA_CHINESE_DEFAULT",
        values={"title": "中文标题"},
        write=True,
    )
    explicit_plan = project.create_module(
        "idea",
        "IDEA_ENGLISH_OVERRIDE",
        values={"title": "English title", "language": "en"},
        write=False,
    )

    assert project.preferred_language == "zh"
    assert project.to_view()["preferred_language"] == "zh"
    assert catalog["preferred_language"] == "zh"
    assert template["args"]["language"]["default"] == "zh"
    assert template["form"]["fields"][3]["default"] == "zh"
    assert default_plan["written"] is True
    assert default_plan["values"]["language"] == "zh"
    assert "[zh.IDEA_CHINESE_DEFAULT]" in (Path(str(default_plan["root"])) / "main.loc").read_text(encoding="utf-8")
    assert explicit_plan["values"]["language"] == "en"


@pytest.mark.parametrize("value", ["", "klingon", "l_klingon", "../zh", 7])
def test_project_rejects_invalid_preferred_language(
    tmp_path: Path,
    value: object,
) -> None:
    project = Project.create(tmp_path / "invalid-authoring-language")
    manifest = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))
    manifest["preferred_language"] = value
    project.manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="preferred_language"):
        Project.load(project.root)


def test_project_load_defaults_output_root_to_macos_hoi4_mod_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "default-output-project"
    (project_root / "src").mkdir(parents=True)
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_build",
                "title: Test Build",
                "game: hoi4",
                "source_roots: [src]",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(project_sdk.HOI4_MOD_ROOT_ENV, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(project_sdk.sys, "platform", "darwin")

    project = Project.load(project_root)

    expected = home / "Documents/Paradox Interactive/Hearts of Iron IV/mod/test_build"
    assert project.output_root == project_sdk._absolute_lexical_path(expected)


def test_project_load_defaults_output_root_to_windows_hoi4_mod_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    documents = tmp_path / "OneDrive - Example Studio/Documents"
    project_root = tmp_path / "default-output-project"
    (project_root / "src").mkdir(parents=True)
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_build",
                "title: Test Build",
                "game: hoi4",
                "source_roots: [src]",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(project_sdk.HOI4_MOD_ROOT_ENV, raising=False)
    monkeypatch.setattr(project_sdk.sys, "platform", "win32")
    monkeypatch.setattr(project_sdk, "_windows_documents_from_known_folder", lambda: documents)

    project = Project.load(project_root)

    assert project.output_root == (documents / "Paradox Interactive/Hearts of Iron IV/mod/test_build").resolve()


def test_windows_documents_falls_back_from_known_folder_to_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = tmp_path / "Registry Redirect/Documents"
    calls: list[str] = []

    def known_folder() -> Path:
        calls.append("known-folder")
        raise OSError("shell API unavailable")

    def registry() -> Path:
        calls.append("registry")
        return documents

    def profile() -> Path:
        raise AssertionError("profile fallback must not run after a registry result")

    monkeypatch.setattr(project_sdk, "_windows_documents_from_known_folder", known_folder)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_registry", registry)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_profile", profile)

    assert project_sdk._windows_documents_directory() == documents
    assert calls == ["known-folder", "registry"]


def test_windows_known_folder_lookup_uses_shell_api_and_releases_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = tmp_path / "Known Folder Redirect/Documents"
    path_buffer = ctypes.create_unicode_buffer(str(documents))
    released: list[int | None] = []

    class FakeFunction:
        def __init__(self, callback: Callable[..., object]) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)

    class FakeLibrary:
        pass

    def get_known_folder_path(_folder_id: object, _flags: object, _token: object, output: object) -> int:
        output_pointer = ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))
        output_pointer[0] = ctypes.addressof(path_buffer)
        return 0

    def free_memory(pointer: ctypes.c_void_p) -> None:
        released.append(pointer.value)

    shell32 = FakeLibrary()
    shell32.SHGetKnownFolderPath = FakeFunction(get_known_folder_path)
    ole32 = FakeLibrary()
    ole32.CoTaskMemFree = FakeFunction(free_memory)

    def load_library(name: str, *, use_last_error: bool) -> FakeLibrary:
        assert use_last_error is True
        return {"shell32": shell32, "ole32": ole32}[name]

    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)

    assert project_sdk._windows_documents_from_known_folder() == documents
    assert released == [ctypes.addressof(path_buffer)]


def test_windows_documents_falls_back_to_user_profile_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = tmp_path / "profile/Documents"
    calls: list[str] = []

    def known_folder() -> Path:
        calls.append("known-folder")
        raise OSError("shell API unavailable")

    def registry() -> Path:
        calls.append("registry")
        raise OSError("registry unavailable")

    def profile() -> Path:
        calls.append("profile")
        return documents

    monkeypatch.setattr(project_sdk, "_windows_documents_from_known_folder", known_folder)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_registry", registry)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_profile", profile)

    assert project_sdk._windows_documents_directory() == documents
    assert calls == ["known-folder", "registry", "profile"]


def test_windows_documents_failure_explains_explicit_mod_root_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> Path:
        raise OSError("unavailable")

    monkeypatch.setattr(project_sdk, "_windows_documents_from_known_folder", unavailable)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_registry", unavailable)
    monkeypatch.setattr(project_sdk, "_windows_documents_from_profile", unavailable)

    with pytest.raises(ProjectManifestError) as error:
        project_sdk._windows_documents_directory()

    message = str(error.value)
    assert "Unable to locate the Windows Documents folder" in message
    assert project_sdk.HOI4_MOD_ROOT_ENV in message
    assert "full writable HoI4 'mod' directory" in message
    assert "Known Folder API" in message
    assert "User Shell Folders registry" in message
    assert "user profile" in message


def test_windows_hoi4_mod_root_preserves_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod_root = tmp_path / "custom-hoi4-mod"

    def unexpected_lookup() -> Path:
        raise AssertionError("Windows Documents discovery must not run for an explicit override")

    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))
    monkeypatch.setattr(project_sdk.sys, "platform", "win32")
    monkeypatch.setattr(project_sdk, "_windows_documents_directory", unexpected_lookup)

    assert project_sdk._hoi4_user_mod_root("hoi4") == mod_root.resolve()


def test_project_create_defaults_output_root_to_hoi4_mod_root_and_syncs_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod_root = tmp_path / "hoi4-mod"
    monkeypatch.setenv(project_sdk.HOI4_MOD_ROOT_ENV, str(mod_root))

    project = Project.create(tmp_path / "starter-mod", title="Starter Mod")
    starter_meta = project.source_roots[0] / "modules/modifier/starter_mod_starter_modifier/meta.yaml"
    result = project.build(emit_artifacts=True, emit_manifests=True)

    assert result.blocked is False
    assert starter_meta.read_text(encoding="utf-8") == "title: Starter Modifier\n"
    assert project.output_root == (mod_root / "starter_mod").resolve()
    assert "output_root:" not in project.manifest_path.read_text(encoding="utf-8")
    assert (project.output_root / "descriptor.mod").is_file()
    launcher_text = (mod_root / "starter_mod.mod").read_text(encoding="utf-8")
    assert f'path="{project.output_root}"' in launcher_text


def test_project_load_discovers_root_from_nested_path() -> None:
    project = Project.load(NESTED_SOURCE)

    assert project.root == PROJECT_ROOT
    assert project.manifest_path == PROJECT_ROOT / "paradev.yaml"


def test_project_find_returns_project_view_from_nested_path() -> None:
    payload = Project.find(NESTED_SOURCE)

    assert payload["schema"] == "paradev.project.find.v1"
    assert payload["found"] is True
    assert payload["query_path"] == str(NESTED_SOURCE)
    assert payload["project"]["project_id"] == "minimal_hoi4"
    assert payload["project"]["root"] == str(PROJECT_ROOT)


def test_project_find_returns_missing_manifest_diagnostic(tmp_path: Path) -> None:
    payload = Project.find(tmp_path)

    assert payload["schema"] == "paradev.project.find.v1"
    assert payload["found"] is False
    assert payload["query_path"] == str(tmp_path)
    assert payload["diagnostics"] == [
        {
            "severity": "error",
            "code": "project.manifest_missing",
            "message": f"Missing paradev.yaml while discovering project from {tmp_path}.",
        }
    ]


def test_open_project_uses_project_manifest_contract() -> None:
    assert open_project(PROJECT_ROOT).to_view()["project_id"] == "minimal_hoi4"


def test_registered_projects_accepts_explicit_and_search_root_projects(
    tmp_path: Path,
) -> None:
    explicit = Project.create(tmp_path / "explicit-mod", title="Explicit Mod")
    searched = Project.create(tmp_path / "workspace/searched-mod", title="Searched Mod")

    payload = registered_projects(project_paths=(explicit.root / "src",), search_roots=(tmp_path / "workspace",))
    projects = {row["project_id"]: row for row in payload["projects"]}

    assert payload["schema"] == "paradev.sdk.projects.v1"
    assert [row["project_id"] for row in payload["projects"]] == [
        "explicit_mod",
        "searched_mod",
    ]
    assert projects["explicit_mod"]["title"] == "Explicit Mod"
    assert projects["explicit_mod"]["root"] == str(explicit.root)
    assert projects["searched_mod"]["root"] == str(searched.root)
    assert payload["index"]["project_id"] == {"explicit_mod": [0], "searched_mod": [1]}
    assert payload["index"]["root"][str(explicit.root)] == [0]
    assert payload["diagnostics"] == []


def test_registered_projects_exposes_descriptor_metadata_and_version(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "versioned-mod", title="Versioned Mod")
    project.manifest_path.write_text(
        f"{project.manifest_path.read_text(encoding='utf-8')}\n" "mod_version: v0.2.3\n" "supported_version: 1.19.*\n",
        encoding="utf-8",
    )

    payload = registered_projects(project_paths=(project.root,))
    row = payload["projects"][0]
    state = desktop_state(project.root, include_browser=False)

    assert row["version"] == "0.2.3"
    assert row["descriptor"] == {"mod_version": "v0.2.3", "supported_version": "1.19.*"}
    assert state["active_project"]["version"] == "0.2.3"
    assert state["active_project"]["descriptor"] == row["descriptor"]
    assert state["active_view"]["version"] == "0.2.3"
    assert state["active_view"]["descriptor"] == row["descriptor"]


def test_registered_projects_reports_missing_explicit_and_search_roots(
    tmp_path: Path,
) -> None:
    payload = registered_projects(
        project_paths=(tmp_path / "missing-project",),
        search_roots=(tmp_path / "missing-root",),
    )

    assert payload["projects"] == []
    assert [row["code"] for row in payload["diagnostics"]] == [
        "project.manifest_missing",
        "project.search_root_missing",
    ]


def test_desktop_state_returns_active_project_view_and_registry(tmp_path: Path) -> None:
    active = Project.create(tmp_path / "active-mod", title="Active Mod")
    Project.create(tmp_path / "known-mod", title="Known Mod")

    payload = desktop_state(active.root / "src", search_roots=(tmp_path,))

    assert payload["schema"] == "paradev.desktop.state.v1"
    assert payload["active_project"]["project_id"] == "active_mod"
    assert payload["active_view"]["project_id"] == "active_mod"
    assert payload["browser"]["schema"] == "paradev.sdk.project-browser.v1"
    assert payload["templates"]["schema"] == "paradev.sdk.templates.v1"
    assert {row["project_id"] for row in payload["projects"]} == {
        "active_mod",
        "known_mod",
    }
    assert payload["registry"]["index"]["project_id"]["active_mod"] == [0]


def test_desktop_state_can_skip_active_browser_payload(tmp_path: Path) -> None:
    active = Project.create(tmp_path / "active-mod", title="Active Mod")

    payload = desktop_state(active.root, include_browser=False)

    assert payload["schema"] == "paradev.desktop.state.v1"
    assert payload["active_project"]["project_id"] == "active_mod"
    assert payload["active_view"]["project_id"] == "active_mod"
    assert payload["browser"] is None
    assert payload["templates"]["schema"] == "paradev.sdk.templates.v1"


def test_project_cli_desktop_state_can_skip_active_browser_payload(
    tmp_path: Path,
) -> None:
    active = Project.create(tmp_path / "active-mod", title="Active Mod")

    result = CliRunner().invoke(
        build_app(),
        ["desktop-state", "--project", str(active.root), "--no-browser", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.desktop.state.v1"
    assert payload["browser"] is None
    assert payload["templates"]["schema"] == "paradev.sdk.templates.v1"


def test_project_rename_updates_manifest_title_without_moving_project(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    (project.root / ".paradev.yaml").write_text(
        "system_only: hidden\n",
        encoding="utf-8",
    )

    renamed = project.rename("Renamed Starter")

    assert project.title == "Starter Mod"
    assert renamed.title == "Renamed Starter"
    assert renamed.project_id == "starter"
    assert renamed.root == project.root
    assert Project.load(project.root).title == "Renamed Starter"
    manifest_text = project.manifest_path.read_text(encoding="utf-8")
    assert "title: Renamed Starter" in manifest_text
    assert "system_only" not in manifest_text


def test_project_preferred_language_plan_apply_is_atomic_and_keeps_system_metadata_hidden(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    (project.root / ".paradev.yaml").write_text(
        "system_only: hidden\n",
        encoding="utf-8",
    )
    project = Project.load(project.root)
    before = project.manifest_path.read_bytes()

    plan = project.set_preferred_language("zh")

    assert plan["schema"] == "paradev.project.preferred-language.v1"
    assert plan["previous_language"] == "en"
    assert plan["preferred_language"] == "zh"
    assert plan["changed"] is True
    assert plan["written"] is False
    assert plan["blocked"] is False
    assert project.manifest_path.read_bytes() == before

    missing_hash = project.set_preferred_language("zh", write=True)
    assert missing_hash["blocked"] is True
    assert missing_hash["diagnostics"][0]["code"] == ("project_preferred_language.plan_hash_required")
    assert project.manifest_path.read_bytes() == before

    applied = project.set_preferred_language(
        "zh",
        write=True,
        plan_hash=str(plan["plan_hash"]),
    )
    visible = yaml.safe_load(project.manifest_path.read_text(encoding="utf-8"))

    assert applied["blocked"] is False
    assert applied["written"] is True
    assert applied["project"]["preferred_language"] == "zh"
    assert visible["preferred_language"] == "zh"
    assert "system_only" not in visible
    assert Project.load(project.root).preferred_language == "zh"


def test_project_preferred_language_apply_rejects_stale_manifest_plan(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    plan = project.set_preferred_language("zh")
    project.manifest_path.write_text(
        f"{project.manifest_path.read_text(encoding='utf-8')}comment: external edit\n",
        encoding="utf-8",
    )

    stale = project.set_preferred_language(
        "zh",
        write=True,
        plan_hash=str(plan["plan_hash"]),
    )

    assert stale["blocked"] is True
    assert stale["diagnostics"][0]["code"] == ("project_preferred_language.plan_hash_mismatch")
    assert "comment: external edit" in project.manifest_path.read_text(encoding="utf-8")


def test_project_preferred_language_normalizes_aliases_and_keeps_noop_bytes(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    plan = project.set_preferred_language("l_simp_chinese")

    assert plan["preferred_language"] == "zh"
    applied = project.set_preferred_language(
        "l_simp_chinese",
        write=True,
        plan_hash=str(plan["plan_hash"]),
    )
    before_noop = project.manifest_path.read_bytes()
    noop = Project.load(project.root).set_preferred_language("simp_chinese")

    assert applied["written"] is True
    assert noop["preferred_language"] == "zh"
    assert noop["changed"] is False
    assert project.manifest_path.read_bytes() == before_noop


def test_project_rename_rejects_blank_title(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    with pytest.raises(ValueError, match="Project title must be non-empty"):
        project.rename("  ")


def test_module_rename_moves_source_module_without_rewriting_content(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    previous_root = project.root / "src/modules/modifier/starter_starter_modifier"
    previous_content = (previous_root / "def.txt").read_text(encoding="utf-8")

    payload = project.rename_module("modifier/starter_starter_modifier", "starter_renamed_modifier")

    new_root = project.root / "src/modules/modifier/starter_renamed_modifier"
    assert payload["schema"] == "paradev.module.rename.v1"
    assert payload["previous_module_id"] == "modifier/starter_starter_modifier"
    assert payload["module_id"] == "modifier/starter_renamed_modifier"
    assert payload["family"] == "modifier"
    assert payload["previous_root"] == str(previous_root)
    assert payload["root"] == str(new_root)
    assert payload["content_rewritten"] is False
    assert payload["module"]["module_id"] == "modifier/starter_renamed_modifier"
    assert payload["module"]["source_slots"] == {
        "def": ["def.txt"],
        "loc": ["main.loc"],
    }
    assert not previous_root.exists()
    assert (new_root / "def.txt").read_text(encoding="utf-8") == previous_content
    assert Project.load(project.root).modules(module_id="modifier/starter_renamed_modifier")["modules"][0]["root"] == str(new_root)


def test_module_rename_synchronizes_readable_title_without_changing_object_id(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    previous_root = project.root / "src/modules/modifier/starter_starter_modifier"
    previous_content = (previous_root / "def.txt").read_bytes()

    payload = project.rename_module(
        "modifier/starter_starter_modifier",
        "starter_starter_modifier",
        title="Friendly Modifier",
    )

    new_root = project.root / "src/modules/modifier/starter_starter_modifier - Friendly Modifier"
    assert payload["previous_module_id"] == payload["module_id"]
    assert payload["root"] == str(new_root)
    assert payload["content_rewritten"] is False
    assert not previous_root.exists()
    assert (new_root / "def.txt").read_bytes() == previous_content


def test_module_rename_rejects_blank_readable_title(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    with pytest.raises(ValueError, match="Module title must be non-empty"):
        project.rename_module(
            "modifier/starter_starter_modifier",
            "starter_starter_modifier",
            title="  ",
        )


def test_renamed_module_record_moves_only_slots_anchored_under_previous_root(
    tmp_path: Path,
) -> None:
    previous_root = tmp_path / "src/modules/modifier/old"
    root = previous_root.parent / "renamed"
    shared_source = tmp_path / "src/localisation/shared.loc"
    module = Module(
        module_id="modifier/old",
        family="modifier",
        root=previous_root,
        source_slots={
            "absolute": (previous_root / "def.txt",),
            "relative": (Path("main.loc"),),
            "shared": (shared_source,),
        },
    )

    renamed = project_sdk._renamed_module_record(
        module,
        module_id="modifier/renamed",
        previous_root=previous_root,
        root=root,
    )

    assert renamed.source_slots == {
        "absolute": (root / "def.txt",),
        "relative": (Path("main.loc"),),
        "shared": (shared_source,),
    }


def test_module_rename_rejects_existing_destination(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    existing = project.root / "src/modules/modifier/existing_modifier"
    existing.mkdir(parents=True)
    (existing / "def.txt").write_text("existing_modifier = { stability_factor = 0.01 }", encoding="utf-8")

    with pytest.raises(ValueError, match="Module target already exists"):
        project.rename_module("modifier/starter_starter_modifier", "existing_modifier")


def test_module_rename_can_select_duplicate_module_id_source_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "multi-root-project"
    for source_root in ("src", "imports"):
        module_root = project_root / source_root / "modules/modifier/shared_modifier"
        module_root.mkdir(parents=True)
        (module_root / "def.txt").write_text("shared_modifier = { stability_factor = 0.01 }", encoding="utf-8")
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: multi_root_project",
                "title: Multi Root Project",
                "game: hoi4",
                "source_roots: [src, imports]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(project_root)

    with pytest.raises(ValueError, match="multiple source roots"):
        project.rename_module("modifier/shared_modifier", "renamed_modifier")

    payload = project.rename_module("modifier/shared_modifier", "renamed_modifier", source_root="imports")

    assert payload["previous_relative_path"] == "imports/modules/modifier/shared_modifier"
    assert payload["relative_path"] == "imports/modules/modifier/renamed_modifier"
    assert (project_root / "src/modules/modifier/shared_modifier").is_dir()
    assert not (project_root / "imports/modules/modifier/shared_modifier").exists()
    assert (project_root / "imports/modules/modifier/renamed_modifier/def.txt").is_file()


def test_module_rename_blocks_sibling_module_symlink_without_moving_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    family_root = project.root / "src/modules/modifier"
    target = family_root / "protected_modifier"
    target.mkdir()
    protected_file = target / "def.txt"
    protected_file.write_text("protected_modifier = { stability_factor = 0.05 }", encoding="utf-8")
    link = family_root / "linked_modifier"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="Module rename path is unsafe.*symbolic link"):
        project.rename_module("modifier/linked_modifier", "renamed_modifier")

    assert link.is_symlink()
    assert target.is_dir()
    assert protected_file.read_text(encoding="utf-8") == "protected_modifier = { stability_factor = 0.05 }"
    assert not (family_root / "renamed_modifier").exists()


def test_module_rename_rejects_broken_destination_symlink_without_moving_source(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    source = project.root / "src/modules/modifier/starter_starter_modifier"
    destination = project.root / "src/modules/modifier/renamed_modifier"
    outside = tmp_path / "outside-renamed-module"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Module target already exists"):
        project.rename_module("modifier/starter_starter_modifier", "renamed_modifier")

    assert source.is_dir()
    assert destination.is_symlink()
    assert not outside.exists()


def test_module_rename_is_anchored_when_family_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    family_root = project.root / "src/modules/modifier"
    displaced_family = project.root / "displaced-modifier-family"
    external_family = tmp_path / "external-modifier-family"
    external_family.mkdir()
    protected_file = external_family / "protected.txt"
    protected_file.write_text("keep", encoding="utf-8")
    original_rename = os.rename
    swapped = False

    def swapping_rename(source, target, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("src_dir_fd") is not None:
            swapped = True
            original_rename(family_root, displaced_family)
            family_root.symlink_to(external_family, target_is_directory=True)
        return original_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "rename", swapping_rename)

    payload = project.rename_module("modifier/starter_starter_modifier", "renamed_modifier")

    assert payload["module_id"] == "modifier/renamed_modifier"
    assert family_root.is_symlink()
    assert not (displaced_family / "starter_starter_modifier").exists()
    assert (displaced_family / "renamed_modifier/def.txt").is_file()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_module_rename_fails_closed_without_anchored_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    source = project.root / "src/modules/modifier/starter_starter_modifier"
    monkeypatch.setattr(project_sdk, "_ANCHORED_MODULE_MUTATION_SUPPORTED", False)

    with pytest.raises(ValueError, match="descriptor-anchored module rename is unavailable"):
        project.rename_module("modifier/starter_starter_modifier", "renamed_modifier")

    assert source.is_dir()
    assert not (source.parent / "renamed_modifier").exists()


def test_module_rename_does_not_mask_success_when_anchor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    original_open_family = project_sdk._open_module_family_fd
    original_close = os.close
    family_descriptor: list[int] = []

    def capture_family_descriptor(source_root: Path, family: str) -> int:
        descriptor = original_open_family(source_root, family)
        family_descriptor.append(descriptor)
        return descriptor

    def fail_family_close(descriptor: int) -> None:
        if family_descriptor and descriptor == family_descriptor[-1]:
            raise OSError("injected post-rename close failure")
        original_close(descriptor)

    monkeypatch.setattr(project_sdk, "_open_module_family_fd", capture_family_descriptor)
    monkeypatch.setattr(os, "close", fail_family_close)
    try:
        payload = project.rename_module("modifier/starter_starter_modifier", "renamed_modifier")
    finally:
        if family_descriptor:
            original_close(family_descriptor[-1])

    assert payload["module_id"] == "modifier/renamed_modifier"
    assert (project.root / "src/modules/modifier/renamed_modifier/def.txt").is_file()


def test_module_remove_plans_and_removes_source_module(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    module_root = project.root / "src/modules/modifier/starter_starter_modifier"

    dry = project.remove_module("modifier/starter_starter_modifier")

    assert dry["schema"] == "paradev.module.remove.v1"
    assert dry["module_id"] == "modifier/starter_starter_modifier"
    assert dry["family"] == "modifier"
    assert dry["source_root"] == str(project.root / "src")
    assert dry["root"] == str(module_root)
    assert dry["relative_path"] == "src/modules/modifier/starter_starter_modifier"
    assert dry["blocked"] is False
    assert dry["removed"] is False
    assert dry["module"]["module_id"] == "modifier/starter_starter_modifier"
    assert [item["module_relative_path"] for item in dry["files"]] == [
        "def.txt",
        "main.loc",
        "meta.yaml",
    ]
    assert all(item["will_remove"] is True for item in dry["files"])
    assert module_root.is_dir()

    removed = project.remove_module("modifier/starter_starter_modifier", write=True)

    assert removed["blocked"] is False
    assert removed["removed"] is True
    assert not module_root.exists()
    assert Project.load(project.root).modules(module_id="modifier/starter_starter_modifier")["modules"] == []


def test_module_remove_can_select_duplicate_module_id_source_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "multi-root-project"
    for source_root in ("src", "imports"):
        module_root = project_root / source_root / "modules/modifier/shared_modifier"
        module_root.mkdir(parents=True)
        (module_root / "def.txt").write_text("shared_modifier = { stability_factor = 0.01 }", encoding="utf-8")
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: multi_root_project",
                "title: Multi Root Project",
                "game: hoi4",
                "source_roots: [src, imports]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(project_root)

    with pytest.raises(ValueError, match="multiple source roots"):
        project.remove_module("modifier/shared_modifier")

    payload = project.remove_module("modifier/shared_modifier", source_root="imports", write=True)

    assert payload["removed"] is True
    assert payload["relative_path"] == "imports/modules/modifier/shared_modifier"
    assert (project_root / "src/modules/modifier/shared_modifier").is_dir()
    assert not (project_root / "imports/modules/modifier/shared_modifier").exists()


def test_module_remove_blocks_sibling_module_symlink_without_deleting_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    family_root = project.root / "src/modules/modifier"
    target = family_root / "protected_modifier"
    target.mkdir()
    protected_file = target / "def.txt"
    protected_file.write_text("protected_modifier = { stability_factor = 0.05 }", encoding="utf-8")
    link = family_root / "linked_modifier"
    link.symlink_to(target, target_is_directory=True)

    payload = project.remove_module("modifier/linked_modifier", write=True)

    assert payload["blocked"] is True
    assert payload["removed"] is False
    assert payload["root"] == str(link)
    assert payload["files"] == []
    assert [item["code"] for item in payload["diagnostics"]] == ["module_remove.path_symlink"]
    assert link.is_symlink()
    assert protected_file.read_text(encoding="utf-8") == "protected_modifier = { stability_factor = 0.05 }"


def test_module_remove_blocks_symlinked_family_without_deleting_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    target = project.root / "protected-family"
    target_module = target / "protected_modifier"
    target_module.mkdir(parents=True)
    protected_file = target_module / "def.txt"
    protected_file.write_text("protected_modifier = { stability_factor = 0.05 }", encoding="utf-8")
    family_link = project.root / "src/modules/linked_family"
    family_link.symlink_to(target, target_is_directory=True)

    payload = project.remove_module("linked_family/protected_modifier", write=True)

    assert payload["blocked"] is True
    assert payload["removed"] is False
    assert payload["root"] == str(family_link / "protected_modifier")
    assert payload["files"] == []
    assert [item["code"] for item in payload["diagnostics"]] == ["module_remove.path_component_symlink"]
    assert family_link.is_symlink()
    assert protected_file.read_text(encoding="utf-8") == "protected_modifier = { stability_factor = 0.05 }"


def test_module_remove_blocks_external_module_symlink_without_deleting_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    target = tmp_path / "external-module"
    target.mkdir()
    protected_file = target / "def.txt"
    protected_file.write_text("external_modifier = { stability_factor = 0.05 }", encoding="utf-8")
    link = project.root / "src/modules/modifier/external_modifier"
    link.symlink_to(target, target_is_directory=True)

    payload = project.remove_module("modifier/external_modifier", write=True)

    assert payload["blocked"] is True
    assert payload["removed"] is False
    assert payload["root"] == str(link)
    assert payload["files"] == []
    assert [item["code"] for item in payload["diagnostics"]] == ["module_remove.path_symlink"]
    assert link.is_symlink()
    assert protected_file.read_text(encoding="utf-8") == "external_modifier = { stability_factor = 0.05 }"


def test_module_remove_is_anchored_when_family_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    family_root = project.root / "src/modules/modifier"
    displaced_family = project.root / "displaced-modifier-family"
    external_family = tmp_path / "external-modifier-family"
    external_module = external_family / "starter_starter_modifier"
    external_module.mkdir(parents=True)
    protected_file = external_module / "protected.txt"
    protected_file.write_text("keep", encoding="utf-8")
    original_remove = project_sdk._remove_directory_tree_at
    swapped = False

    def swapping_remove(parent_fd: int, name: str) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            family_root.rename(displaced_family)
            family_root.symlink_to(external_family, target_is_directory=True)
        original_remove(parent_fd, name)

    monkeypatch.setattr(project_sdk, "_remove_directory_tree_at", swapping_remove)

    payload = project.remove_module("modifier/starter_starter_modifier", write=True)

    assert payload["removed"] is True
    assert family_root.is_symlink()
    assert not (displaced_family / "starter_starter_modifier").exists()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_module_remove_commits_before_best_effort_quarantine_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    module_root = project.root / "src/modules/modifier/starter_starter_modifier"

    def partially_failing_remove(quarantine_fd: int, name: str) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        module_fd = os.open(name, flags, dir_fd=quarantine_fd)
        try:
            os.unlink("def.txt", dir_fd=module_fd)
        finally:
            os.close(module_fd)
        raise OSError("injected quarantine cleanup failure")

    monkeypatch.setattr(project_sdk, "_remove_directory_tree_at", partially_failing_remove)

    payload = project.remove_module("modifier/starter_starter_modifier", write=True)

    cleanup = payload["cleanup"]
    quarantine = Path(cleanup["path"])
    assert payload["removed"] is True
    assert payload["blocked"] is False
    assert payload["catalog_mutation"]["status"] == "not_configured"
    assert not module_root.exists()
    assert quarantine.is_dir()
    assert not (quarantine / "def.txt").exists()
    assert (quarantine / "main.loc").is_file()
    assert cleanup["status"] == "pending"
    assert [item["code"] for item in payload["diagnostics"]] == ["module_remove.cleanup_pending"]
    assert payload["diagnostics"][0]["severity"] == "warning"
    assert "injected quarantine cleanup failure" in cleanup["message"]


def test_module_remove_synchronizes_catalog_before_quarantine_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    events: list[str] = []
    original_sync = paradev_hb._sync_module_catalog_projection
    original_remove = project_sdk._remove_directory_tree_at

    def tracking_sync(*args, **kwargs):
        events.append("catalog")
        return original_sync(*args, **kwargs)

    def tracking_remove(parent_fd: int, name: str) -> None:
        events.append("cleanup")
        original_remove(parent_fd, name)

    monkeypatch.setattr(paradev_hb, "_sync_module_catalog_projection", tracking_sync)
    monkeypatch.setattr(project_sdk, "_remove_directory_tree_at", tracking_remove)

    payload = project.remove_module("modifier/starter_starter_modifier", write=True)

    assert payload["removed"] is True
    assert events == ["catalog", "cleanup"]


def test_module_remove_does_not_report_cleanup_pending_after_completed_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    original_remove = project_sdk._remove_directory_tree_at

    def cleanup_then_fail(parent_fd: int, name: str) -> None:
        original_remove(parent_fd, name)
        raise OSError("injected error after completed cleanup")

    monkeypatch.setattr(project_sdk, "_remove_directory_tree_at", cleanup_then_fail)

    payload = project.remove_module("modifier/starter_starter_modifier", write=True)

    assert payload["removed"] is True
    assert "cleanup" not in payload
    assert payload["diagnostics"] == []


def test_module_remove_legacy_anchored_cleanup_preserves_symlink_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    target = parent / "target"
    nested = target / "nested"
    protected = tmp_path / "protected"
    nested.mkdir(parents=True)
    protected.mkdir()
    (nested / "data.txt").write_text("remove", encoding="utf-8")
    protected_file = protected / "keep.txt"
    protected_file.write_text("keep", encoding="utf-8")
    (target / "external").symlink_to(protected, target_is_directory=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(parent, flags)
    monkeypatch.setattr(project_sdk, "_SHUTIL_RMTREE_SUPPORTS_DIR_FD", False)

    try:
        project_sdk._remove_directory_tree_at(parent_fd, "target")
    finally:
        os.close(parent_fd)

    assert not target.exists()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_module_remove_fails_closed_without_anchored_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    module_root = project.root / "src/modules/modifier/starter_starter_modifier"
    monkeypatch.setattr(project_sdk, "_ANCHORED_MODULE_MUTATION_SUPPORTED", False)

    payload = project.remove_module("modifier/starter_starter_modifier", write=True)

    assert payload["removed"] is False
    assert payload["blocked"] is True
    assert [item["code"] for item in payload["diagnostics"]] == ["module_remove.unsupported_platform"]
    assert "descriptor-anchored module removal is unavailable" in payload["diagnostics"][0]["message"]
    assert module_root.is_dir()


def test_module_remove_does_not_mask_success_when_anchor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    module_root = project.root / "src/modules/modifier/starter_starter_modifier"
    original_open_family = project_sdk._open_module_family_fd
    original_close = os.close
    family_descriptor: list[int] = []

    def capture_family_descriptor(source_root: Path, family: str) -> int:
        descriptor = original_open_family(source_root, family)
        family_descriptor.append(descriptor)
        return descriptor

    def fail_family_close(descriptor: int) -> None:
        if family_descriptor and descriptor == family_descriptor[-1]:
            raise OSError("injected post-removal close failure")
        original_close(descriptor)

    monkeypatch.setattr(project_sdk, "_open_module_family_fd", capture_family_descriptor)
    monkeypatch.setattr(os, "close", fail_family_close)
    try:
        payload = project.remove_module("modifier/starter_starter_modifier", write=True)
    finally:
        if family_descriptor:
            original_close(family_descriptor[-1])

    assert payload["removed"] is True
    assert payload["blocked"] is False
    assert not module_root.exists()


def test_module_file_read_and_write_text_source(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    source = project.read_module_file("modifier/starter_starter_modifier", "def.txt")

    assert source["schema"] == "paradev.module.file.v1"
    assert source["module_id"] == "modifier/starter_starter_modifier"
    assert source["module_relative_path"] == "def.txt"
    assert source["relative_path"] == "src/modules/modifier/starter_starter_modifier/def.txt"
    assert source["encoding"] == "utf-8"
    assert source["exists"] is True
    assert "stability_factor" in source["text"]
    metadata = (project.root / "src/modules/modifier/starter_starter_modifier/def.txt").stat()
    assert source["size"] == metadata.st_size
    assert source["mtime_ns"] == str(metadata.st_mtime_ns)

    text = "starter_starter_modifier = {\n\tstability_factor = 0.10\n}\n"
    written = project.write_module_file("modifier/starter_starter_modifier", "def.txt", text)

    assert written["schema"] == "paradev.module.file.v1"
    assert written["written"] is True
    assert written["text"] == text
    assert (project.root / "src/modules/modifier/starter_starter_modifier/def.txt").read_text(encoding="utf-8") == text


def test_module_asset_snapshot_uses_registry_slots_and_draft_guards(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ASSET",
        values={"title": "Asset Idea"},
        write=True,
    )
    icon = project.root / "src/modules/idea/IDEA_ASSET/icon.png"
    icon.write_bytes(b"\x89PNG\r\n\x1a\nasset")

    metadata = project.read_module_asset("idea/IDEA_ASSET", "icon.png")
    content = project.read_module_asset(
        "idea/IDEA_ASSET",
        "icon.png",
        include_content=True,
    )

    assert metadata["schema"] == "paradev.module.asset.v1"
    assert metadata["module_id"] == "idea/IDEA_ASSET"
    assert metadata["module_relative_path"] == "icon.png"
    assert metadata["mime_type"] == "image/png"
    assert metadata["file_format"] == "png"
    assert metadata["source_slots"] == [{"name": "icon", "kinds": ["copy"]}]
    assert metadata["size"] == icon.stat().st_size
    assert metadata["mtime_ns"] == str(icon.stat().st_mtime_ns)
    assert metadata["sha256"] == hashlib.sha256(icon.read_bytes()).hexdigest()
    assert metadata["content_included"] is False
    assert "content_base64" not in metadata
    assert metadata["draft_guard"] == {
        "path": "src/modules/idea/IDEA_ASSET/icon.png",
        "expected_size": metadata["size"],
        "expected_mtime_ns": metadata["mtime_ns"],
    }
    assert content["content_included"] is True
    assert base64.b64decode(content["content_base64"], validate=True) == icon.read_bytes()

    project.apply_source_draft(
        source_replacements=[
            {
                **metadata["draft_guard"],
                "content_base64": base64.b64encode(b"replacement").decode("ascii"),
            }
        ]
    )

    assert icon.read_bytes() == b"replacement"


def test_module_asset_rejects_non_asset_sources_paths_and_oversize_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ASSET",
        values={"title": "Asset Idea"},
        write=True,
    )
    icon = project.root / "src/modules/idea/IDEA_ASSET/icon.png"
    icon.write_bytes(b"oversize")

    with pytest.raises(ValueError, match="registered copy or image source slot"):
        project.read_module_asset("idea/IDEA_ASSET", "def.txt")
    with pytest.raises(ValueError, match="stay inside module root"):
        project.read_module_asset("idea/IDEA_ASSET", "../paradev.yaml")

    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_BINARY_BYTES", 3)
    with pytest.raises(ValueError, match="larger than 3 bytes"):
        project.read_module_asset("idea/IDEA_ASSET", "icon.png")


def test_module_file_write_can_create_nested_text_source(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    payload = project.write_module_file("modifier/starter_starter_modifier", "notes/design.txt", "notes\n", create=True)

    assert payload["module_relative_path"] == "notes/design.txt"
    assert payload["written"] is True
    assert (project.root / "src/modules/modifier/starter_starter_modifier/notes/design.txt").read_text(encoding="utf-8") == "notes\n"


def test_project_reads_project_contained_source_text(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    source_path = "src/modules/idea/GER_industry_spirit/def.txt"

    payload = project.read_source_text(source_path)

    assert payload["schema"] == "paradev.rest.source_text.v1"
    assert payload["project_id"] == project.project_id
    assert payload["path"] == str(project.root / source_path)
    assert payload["relative_path"] == source_path
    assert payload["encoding"] == "utf-8"
    assert "GER_industry_spirit" in payload["text"]
    metadata = (project.root / source_path).stat()
    assert payload["size"] == metadata.st_size
    assert payload["mtime_ns"] == str(metadata.st_mtime_ns)


def test_project_reads_project_contained_binary_source(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    source_path = "src/icon.dds"
    path = project.root / source_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"DDS binary")

    payload = project.read_source_binary(source_path)

    assert payload["schema"] == "paradev.project.source-binary.v1"
    assert payload["project_id"] == project.project_id
    assert payload["path"] == str(path)
    assert payload["relative_path"] == source_path
    assert payload["file_format"] == "dds"
    assert payload["mime_type"] == "image/vnd.ms-dds"
    assert payload["size"] == path.stat().st_size
    assert payload["mtime_ns"] == str(path.stat().st_mtime_ns)
    assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert base64.b64decode(payload["content_base64"], validate=True) == path.read_bytes()
    assert payload["draft_guard"] == {
        "path": source_path,
        "expected_size": payload["size"],
        "expected_mtime_ns": payload["mtime_ns"],
    }


def test_project_source_text_revision_rejects_a_later_external_edit(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    source_path = "src/modules/idea/GER_industry_spirit/def.txt"
    path = project.root / source_path
    snapshot = project.read_source_text(source_path)
    path.write_text("external edit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after the draft was opened"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": source_path,
                    "text": "stale ParaDev edit\n",
                    "expected_size": snapshot["size"],
                    "expected_mtime_ns": snapshot["mtime_ns"],
                }
            ]
        )

    assert path.read_text(encoding="utf-8") == "external edit\n"


def test_project_source_text_rejects_a_file_mutated_during_the_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    source_path = "src/modules/idea/GER_industry_spirit/def.txt"
    path = project.root / source_path
    path.write_text("x" * (1024 * 1024 + 32), encoding="utf-8")
    original_read = project_sdk.os.read
    mutated = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            path.write_text("external edit during read\n", encoding="utf-8")
        return chunk

    monkeypatch.setattr(project_sdk.os, "read", racing_read)

    with pytest.raises(ValueError, match="changed while it was read"):
        project.read_source_text(source_path)


def test_project_apply_source_draft_writes_binary_and_removes_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    def_path = "src/modules/idea/GER_industry_spirit/def.txt"
    icon_path = "src/modules/idea/GER_industry_spirit/icon.png"
    loc_path = "src/modules/idea/GER_industry_spirit/main.loc"
    edited = "ideas = {\n\tcountry = {\n\t\tGER_industry_spirit = {}\n\t}\n}\n"
    monkeypatch.setattr(
        project_sdk,
        "cmd",
        lambda *_args, **_kwargs: pytest.fail("raw binary replacement invoked ImageMagick"),
    )

    payload = project.apply_source_draft(
        source_edits=[{"path": def_path, "text": edited}],
        source_replacements=[{"path": icon_path, "content_base64": "cG5nLWJ5dGVz"}],
        source_removals=[loc_path],
    )

    assert payload["schema"] == "paradev.rest.draft_apply.v1"
    assert payload["project_id"] == project.project_id
    assert payload["written"] is True
    assert payload["catalog_mutation"] == {
        "schema": "paradev.hb.catalog-mutation.v1",
        "status": "not_configured",
        "code": "catalog.mutation.not_configured",
        "database": str(project.root / ".paradev/.cache/hb/catalog.sqlite"),
    }
    assert payload["files"] == [
        {
            "path": str(project.root / def_path),
            "relative_path": def_path,
            "operation": "write_text",
            "encoding": "utf-8",
        },
        {
            "path": str(project.root / icon_path),
            "relative_path": icon_path,
            "operation": "replace_bytes",
        },
        {
            "path": str(project.root / loc_path),
            "relative_path": loc_path,
            "operation": "remove_file",
        },
    ]
    assert (project.root / def_path).read_text(encoding="utf-8") == edited
    assert (project.root / icon_path).read_bytes() == b"png-bytes"
    assert not (project.root / loc_path).exists()


def test_project_apply_source_draft_commits_localization_and_folder_title_together(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    previous_root = project.root / "src/modules/idea/IDEA_ALPHA"
    localization = previous_root / "main.loc"
    revised_localization = "[en.IDEA_ALPHA]\nRenamed Alpha\n"

    payload = project.apply_source_draft(
        source_edits=[
            {
                "path": str(localization),
                "text": revised_localization,
            }
        ],
        module_rename={
            "module_id": "idea/IDEA_ALPHA",
            "object_id": "IDEA_ALPHA",
            "title": "Renamed Alpha",
        },
    )

    renamed_root = project.root / "src/modules/idea/IDEA_ALPHA - Renamed Alpha"
    rename_payload = payload["module_rename"]
    assert isinstance(rename_payload, dict)
    assert not previous_root.exists()
    assert renamed_root.is_dir()
    assert (renamed_root / "main.loc").read_text(encoding="utf-8") == revised_localization
    assert rename_payload["previous_root"] == str(previous_root)
    assert rename_payload["root"] == str(renamed_root)
    assert rename_payload["catalog_mutation"] == payload["catalog_mutation"]


def test_project_apply_source_draft_rolls_back_files_when_folder_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    localization = module_root / "main.loc"
    original_localization = localization.read_bytes()

    def fail_rename(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated guarded rename failure")

    monkeypatch.setattr(project_sdk, "_rename_module_directory", fail_rename)

    with pytest.raises(
        ValueError,
        match="Module rename path changed before it could be moved",
    ):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(localization),
                    "text": "[en.IDEA_ALPHA]\nRenamed Alpha\n",
                }
            ],
            module_rename={
                "module_id": "idea/IDEA_ALPHA",
                "object_id": "IDEA_ALPHA",
                "title": "Renamed Alpha",
            },
        )

    assert module_root.is_dir()
    assert localization.read_bytes() == original_localization
    assert not (project.root / "src/modules/idea/IDEA_ALPHA - Renamed Alpha").exists()


def test_project_apply_source_draft_accepts_a_rename_only_transaction(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )

    payload = project.apply_source_draft(
        module_rename={
            "module_id": "idea/IDEA_ALPHA",
            "object_id": "IDEA_ALPHA",
            "title": "Readable Alpha",
        }
    )

    assert payload["files"] == []
    assert payload["module_rename"]["root"] == str(project.root / "src/modules/idea/IDEA_ALPHA - Readable Alpha")


def test_project_apply_source_draft_rejects_unknown_module_rename_fields(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    with pytest.raises(
        ValueError,
        match="module_rename.*unsupported fields",
    ):
        project.apply_source_draft(
            module_rename={
                "module_id": "idea/IDEA_ALPHA",
                "object_id": "IDEA_ALPHA",
                "legacy_title": "Alpha",
            }
        )


def test_project_apply_source_draft_supports_root_level_files(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    notes = project.root / "notes.txt"
    removed = project.root / "obsolete.bin"
    created = project.root / "created.bin"
    notes.write_text("old", encoding="utf-8")
    removed.write_bytes(b"obsolete")

    payload = project.apply_source_draft(
        source_edits=[{"path": str(notes), "text": "new"}],
        source_replacements=[{"path": str(created), "content_base64": "Y3JlYXRlZA=="}],
        source_removals=[str(removed)],
    )

    assert payload["written"] is True
    assert notes.read_text(encoding="utf-8") == "new"
    assert created.read_bytes() == b"created"
    assert not removed.exists()


def test_project_apply_source_draft_rejects_stale_expected_revisions_before_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/GER_industry_spirit"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    original_definition = definition.read_bytes()
    stale_localization_revision = localization.stat()
    localization.write_text('l_english:\n GER_industry_spirit:0 "External edit"\n', encoding="utf-8")
    external_localization = localization.read_bytes()
    current_definition_revision = definition.stat()

    with pytest.raises(ValueError, match="Source draft conflict"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "replacement",
                    "expected_size": current_definition_revision.st_size,
                    "expected_mtime_ns": str(current_definition_revision.st_mtime_ns),
                },
                {
                    "path": str(localization),
                    "text": 'l_english:\n GER_industry_spirit:0 "Draft edit"\n',
                    "expected_size": stale_localization_revision.st_size,
                    "expected_mtime_ns": str(stale_localization_revision.st_mtime_ns),
                },
            ]
        )

    assert definition.read_bytes() == original_definition
    assert localization.read_bytes() == external_localization


def test_project_apply_source_draft_conditionally_replaces_current_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    icon = project.root / "src/modules/idea/IDEA_ALPHA/icon.png"
    icon.write_bytes(b"initial-image")
    stale_revision = icon.stat()
    icon.write_bytes(b"external-image")
    monkeypatch.setattr(
        project_sdk,
        "cmd",
        lambda *_args, **_kwargs: pytest.fail("raw binary replacement invoked ImageMagick"),
    )

    with pytest.raises(ValueError, match="Source draft conflict"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(icon),
                    "content_base64": "ZHJhZnQtaW1hZ2U=",
                    "expected_size": stale_revision.st_size,
                    "expected_mtime_ns": str(stale_revision.st_mtime_ns),
                }
            ]
        )
    assert icon.read_bytes() == b"external-image"

    current_revision = icon.stat()
    payload = project.apply_source_draft(
        source_replacements=[
            {
                "path": str(icon),
                "content_base64": "ZHJhZnQtaW1hZ2U=",
                "expected_size": current_revision.st_size,
                "expected_mtime_ns": str(current_revision.st_mtime_ns),
            }
        ]
    )
    assert payload["written"] is True
    assert icon.read_bytes() == b"draft-image"


def test_project_apply_source_draft_requires_expected_absence_for_new_replacement(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    icon = project.root / "src/modules/idea/IDEA_ALPHA/new-icon.png"

    payload = project.apply_source_draft(
        source_replacements=[
            {
                "path": str(icon),
                "content_base64": "ZHJhZnQtaW1hZ2U=",
                "expected_absent": True,
            }
        ]
    )
    assert payload["written"] is True
    assert icon.read_bytes() == b"draft-image"

    icon.write_bytes(b"external-image")
    with pytest.raises(ValueError, match="appeared after the draft was opened"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(icon),
                    "content_base64": "c3RhbGUtZHJhZnQ=",
                    "expected_absent": True,
                }
            ]
        )
    assert icon.read_bytes() == b"external-image"


def test_project_apply_source_draft_conditionally_removes_current_source(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    source = project.root / "src/modules/idea/IDEA_ALPHA/main.loc"
    stale_revision = source.stat()
    source.write_text("external localization", encoding="utf-8")

    with pytest.raises(ValueError, match="Source draft conflict"):
        project.apply_source_draft(
            source_removals=[
                {
                    "path": str(source),
                    "expected_size": stale_revision.st_size,
                    "expected_mtime_ns": str(stale_revision.st_mtime_ns),
                }
            ]
        )
    assert source.read_text(encoding="utf-8") == "external localization"

    current_revision = source.stat()
    payload = project.apply_source_draft(
        source_removals=[
            {
                "path": str(source),
                "expected_size": current_revision.st_size,
                "expected_mtime_ns": str(current_revision.st_mtime_ns),
            }
        ]
    )
    assert payload["written"] is True
    assert not source.exists()


def test_project_apply_source_draft_rejects_unknown_guard_fields(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    source = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    revision = source.stat()

    with pytest.raises(ValueError, match="unsupported fields"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(source),
                    "text": "replacement",
                    "expectedSize": revision.st_size,
                    "expectedMtimeNs": str(revision.st_mtime_ns),
                }
            ]
        )


def test_project_apply_source_draft_guard_survives_change_during_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    source = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    revision = source.stat()
    external = b"external edit during install"
    link = project_sdk.os.link
    injected = False

    def inject_external_edit(
        source_name: str,
        target_name: str,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        link(source_name, target_name, **kwargs)
        if not injected and source_name == source.name and target_name.endswith(".displaced"):
            injected = True
            source.write_bytes(external)

    monkeypatch.setattr(project_sdk.os, "link", inject_external_edit)

    with pytest.raises(ValueError, match="Source draft conflict"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(source),
                    "text": "ParaDev draft",
                    "expected_size": revision.st_size,
                    "expected_mtime_ns": str(revision.st_mtime_ns),
                }
            ]
        )

    assert injected is True
    assert source.read_bytes() == external
    assert list(source.parent.glob(".paradev-draft-*.displaced")) == []


def test_project_apply_source_draft_reports_displaced_directory_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    source = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    revision = source.stat()
    original = source.read_bytes()
    link = project_sdk.os.link
    injected = False

    def inject_external_directory(
        source_name: str,
        target_name: str,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        link(source_name, target_name, **kwargs)
        if not injected and source_name == source.name and target_name.endswith(".displaced"):
            injected = True
            source.unlink()
            source.mkdir()
            (source / "external.txt").write_text("external", encoding="utf-8")

    monkeypatch.setattr(project_sdk.os, "link", inject_external_directory)

    with pytest.raises(
        ValueError,
        match="rollback was incomplete",
    ):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(source),
                    "text": "ParaDev draft",
                    "expected_size": revision.st_size,
                    "expected_mtime_ns": str(revision.st_mtime_ns),
                }
            ]
        )

    quarantines = list(source.parent.glob(".paradev-draft-*.displaced"))
    assert injected is True
    assert source.is_dir()
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == original
    assert (source / "external.txt").read_text(encoding="utf-8") == "external"
    assert (project.root / ".paradev/source-draft-transaction/recovery.json").is_file()


def test_project_apply_source_draft_rolls_back_earlier_files_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    original_definition = definition.read_bytes()
    original_localization = localization.read_bytes()
    write_source_draft_content = project_sdk._write_source_draft_content

    def fail_localization_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        if path == localization:
            raise ValueError("simulated late source write failure")
        return write_source_draft_content(project_root, path, content, **kwargs)

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        fail_localization_write,
    )

    with pytest.raises(ValueError, match="simulated late source write failure"):
        project.apply_source_draft(
            source_edits=[
                {"path": str(definition), "text": "replacement"},
                {
                    "path": str(localization),
                    "text": 'l_english:\n IDEA_ALPHA:0 "Draft"\n',
                },
            ]
        )

    assert definition.read_bytes() == original_definition
    assert localization.read_bytes() == original_localization


def test_project_apply_source_draft_does_not_overwrite_external_edit_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    original_localization = localization.read_bytes()
    external_definition = b"external edit after ParaDev write"
    write_source_draft_content = project_sdk._write_source_draft_content

    def change_first_then_fail_second(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        if path == localization:
            raise ValueError("simulated late source write failure")
        mutation = write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )
        definition.write_bytes(external_definition)
        return mutation

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        change_first_then_fail_second,
    )

    with pytest.raises(
        project_sdk._SourceDraftRollbackIncomplete,
        match="rollback was incomplete",
    ) as captured:
        project.apply_source_draft(
            source_edits=[
                {"path": str(definition), "text": "ParaDev draft"},
                {
                    "path": str(localization),
                    "text": 'l_english:\n IDEA_ALPHA:0 "Draft"\n',
                },
            ]
        )

    assert definition.read_bytes() == external_definition
    assert localization.read_bytes() == original_localization
    recovery_root = captured.value.recovery_path
    assert recovery_root == project.root / ".paradev/source-draft-transaction"
    assert (recovery_root / "recovery.json").is_file()
    assert (recovery_root / "backups/0000.backup").is_file()


def test_project_apply_source_draft_recovers_an_abrupt_write_before_next_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    original_definition = definition.read_bytes()
    write_source_draft_content = project_sdk._write_source_draft_content

    def crash_after_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )
        raise SystemExit("simulated abrupt process exit")

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        crash_after_write,
    )
    with pytest.raises(SystemExit, match="abrupt process exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "interrupted draft",
                }
            ]
        )

    assert definition.read_text(encoding="utf-8") == "interrupted draft"
    recovery_root = project.root / ".paradev/source-draft-transaction"
    assert (recovery_root / "recovery.json").is_file()

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        write_source_draft_content,
    )
    project.apply_source_draft(
        source_edits=[
            {
                "path": str(localization),
                "text": 'l_english:\n IDEA_ALPHA:0 "Recovered"\n',
            }
        ]
    )

    assert definition.read_bytes() == original_definition
    assert localization.read_text(encoding="utf-8").endswith('IDEA_ALPHA:0 "Recovered"\n')
    assert list(recovery_root.iterdir()) == []


def test_project_build_recovers_a_guarded_write_displaced_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if project_sdk._uses_win32_source_draft_authority():
        pytest.skip("Adjacent displacement recovery is POSIX-specific.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    source = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    original = source.read_bytes()
    revision = source.stat()
    unlink = _install_source_unlink_crash(monkeypatch, source)

    with pytest.raises(SystemExit, match="abrupt displacement exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(source),
                    "text": "interrupted guarded draft",
                    "expected_size": revision.st_size,
                    "expected_mtime_ns": str(revision.st_mtime_ns),
                }
            ]
        )

    displacements = list(source.parent.glob(".paradev-draft-*.displaced"))
    assert not source.exists()
    assert len(displacements) == 1
    assert displacements[0].read_bytes() == original
    assert list(source.parent.glob(".paradev-draft-*.tmp")) == []
    recovery_root = project.root / ".paradev/source-draft-transaction"
    assert (recovery_root / "recovery.json").is_file()

    monkeypatch.setattr(project_sdk.os, "unlink", unlink)
    result = project.build()

    assert result.blocked is False
    assert source.read_bytes() == original
    assert list(source.parent.glob(".paradev-draft-*.displaced")) == []
    assert list(recovery_root.iterdir()) == []


def test_project_build_recovers_a_removal_displaced_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if project_sdk._uses_win32_source_draft_authority():
        pytest.skip("Adjacent displacement recovery is POSIX-specific.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    source = project.root / "src/modules/idea/IDEA_ALPHA/main.loc"
    original = source.read_bytes()
    revision = source.stat()
    unlink = _install_source_unlink_crash(monkeypatch, source)

    with pytest.raises(SystemExit, match="abrupt displacement exit"):
        project.apply_source_draft(
            source_removals=[
                {
                    "path": str(source),
                    "expected_size": revision.st_size,
                    "expected_mtime_ns": str(revision.st_mtime_ns),
                }
            ]
        )

    displacements = list(source.parent.glob(".paradev-draft-*.displaced"))
    assert not source.exists()
    assert len(displacements) == 1
    assert displacements[0].read_bytes() == original

    monkeypatch.setattr(project_sdk.os, "unlink", unlink)
    result = project.build()

    assert result.blocked is False
    assert source.read_bytes() == original
    assert list(source.parent.glob(".paradev-draft-*.displaced")) == []
    assert list((project.root / ".paradev/source-draft-transaction").iterdir()) == []


def test_project_build_recovers_an_abrupt_guarded_rollback_displacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if project_sdk._uses_win32_source_draft_authority():
        pytest.skip("Adjacent displacement recovery is POSIX-specific.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    original_definition = definition.read_bytes()
    original_localization = localization.read_bytes()
    definition_revision = definition.stat()
    write_source_draft_content = project_sdk._write_source_draft_content

    def fail_localization_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        if path == localization:
            raise ValueError("simulated late source write failure")
        return write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        fail_localization_write,
    )
    unlink = _install_source_unlink_crash(
        monkeypatch,
        definition,
        occurrence=2,
    )

    with pytest.raises(SystemExit, match="abrupt displacement exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "draft requiring rollback",
                    "expected_size": definition_revision.st_size,
                    "expected_mtime_ns": str(definition_revision.st_mtime_ns),
                },
                {
                    "path": str(localization),
                    "text": "late localization draft",
                },
            ]
        )

    displacements = list(definition.parent.glob(".paradev-draft-*.displaced"))
    assert not definition.exists()
    assert len(displacements) == 1
    assert displacements[0].read_bytes() == b"draft requiring rollback"
    assert localization.read_bytes() == original_localization

    monkeypatch.setattr(project_sdk.os, "unlink", unlink)
    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        write_source_draft_content,
    )
    result = project.build()

    assert result.blocked is False
    assert definition.read_bytes() == original_definition
    assert localization.read_bytes() == original_localization
    assert list(definition.parent.glob(".paradev-draft-*.displaced")) == []
    assert list((project.root / ".paradev/source-draft-transaction").iterdir()) == []


def test_project_recovery_preserves_external_file_after_abrupt_displacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if project_sdk._uses_win32_source_draft_authority():
        pytest.skip("Adjacent displacement recovery is POSIX-specific.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    source = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    original = source.read_bytes()
    revision = source.stat()
    unlink = _install_source_unlink_crash(monkeypatch, source)

    with pytest.raises(SystemExit, match="abrupt displacement exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(source),
                    "text": "interrupted guarded draft",
                    "expected_size": revision.st_size,
                    "expected_mtime_ns": str(revision.st_mtime_ns),
                }
            ]
        )

    monkeypatch.setattr(project_sdk.os, "unlink", unlink)
    source.write_bytes(b"newer external source")
    with pytest.raises(ValueError, match="changed outside ParaDev"):
        project.build()

    displacements = list(source.parent.glob(".paradev-draft-*.displaced"))
    assert source.read_bytes() == b"newer external source"
    assert len(displacements) == 1
    assert displacements[0].read_bytes() == original
    assert (project.root / ".paradev/source-draft-transaction/recovery.json").is_file()


def test_project_build_recovers_an_abrupt_source_write_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    definition = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    original_definition = definition.read_bytes()
    write_source_draft_content = project_sdk._write_source_draft_content

    def crash_after_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )
        raise SystemExit("simulated abrupt process exit")

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        crash_after_write,
    )
    with pytest.raises(SystemExit):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "interrupted draft",
                }
            ]
        )
    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        write_source_draft_content,
    )

    result = project.build()

    assert result.blocked is False
    assert definition.read_bytes() == original_definition
    assert list((project.root / ".paradev/source-draft-transaction").iterdir()) == []


def test_project_apply_source_draft_recovers_an_abrupt_module_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    family_root = project.root / "src/modules/idea"
    previous_root = family_root / "IDEA_ALPHA"
    target_root = family_root / "IDEA_BETA"
    commit_prepared_rename = project_sdk._commit_prepared_module_rename

    def crash_after_rename(
        draft_project: Project,
        prepared: project_sdk._PreparedModuleRename,
    ) -> None:
        commit_prepared_rename(draft_project, prepared)
        raise SystemExit("simulated abrupt rename exit")

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_module_rename",
        crash_after_rename,
    )
    with pytest.raises(SystemExit, match="abrupt rename exit"):
        project.apply_source_draft(
            module_rename={
                "module_id": "idea/IDEA_ALPHA",
                "object_id": "IDEA_BETA",
            }
        )

    assert not previous_root.exists()
    assert target_root.is_dir()

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_module_rename",
        commit_prepared_rename,
    )
    project.apply_source_draft(
        source_edits=[
            {
                "path": str(previous_root / "def.txt"),
                "text": "recovered rename",
            }
        ]
    )

    assert previous_root.is_dir()
    assert not target_root.exists()
    assert (previous_root / "def.txt").read_text(encoding="utf-8") == ("recovered rename")


def test_source_draft_recovery_reads_v3_module_rename_as_modules_container() -> None:
    journal = source_recovery_sdk._journal_from_mapping(
        {
            "schema": "paradev.source-draft-recovery.v3",
            "transaction_id": "0" * 32,
            "project_root_identity": [1, 2],
            "phase": "applying",
            "files": [],
            "rename": {
                "source_root": "src",
                "family": "idea",
                "previous_name": "OLD",
                "target_name": "NEW",
                "previous_object_id": "OLD",
                "target_object_id": "NEW",
                "directory_identity": [3, 4],
                "state": "applying",
            },
        }
    )

    assert journal.rename is not None
    assert journal.rename.container == "modules"
    assert journal.rename.operation == "rename"
    payload = journal.to_dict()
    assert payload["schema"] == "paradev.source-draft-recovery.v5"
    assert payload["rename"] == journal.rename.to_dict()
    assert journal.rename.to_dict()["container"] == "modules"


def test_source_draft_recovery_reads_v4_collection_rename_as_rename() -> None:
    journal = source_recovery_sdk._journal_from_mapping(
        {
            "schema": "paradev.source-draft-recovery.v4",
            "transaction_id": "0" * 32,
            "project_root_identity": [1, 2],
            "phase": "applying",
            "files": [],
            "rename": {
                "container": "collections",
                "source_root": "src",
                "family": "focus",
                "previous_name": "OLD",
                "target_name": "NEW",
                "previous_object_id": "OLD",
                "target_object_id": "NEW",
                "directory_identity": [3, 4],
                "state": "applied",
            },
        }
    )

    assert journal.rename is not None
    assert journal.rename.container == "collections"
    assert journal.rename.operation == "rename"
    assert journal.to_dict()["schema"] == "paradev.source-draft-recovery.v5"


def test_project_apply_source_draft_preserves_external_edit_after_abrupt_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    definition = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    localization = project.root / "src/modules/idea/IDEA_ALPHA/main.loc"
    write_source_draft_content = project_sdk._write_source_draft_content

    def crash_after_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )
        raise SystemExit("simulated abrupt process exit")

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        crash_after_write,
    )
    with pytest.raises(SystemExit):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "interrupted draft",
                }
            ]
        )
    definition.write_text("newer external edit", encoding="utf-8")

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        write_source_draft_content,
    )
    with pytest.raises(
        ValueError,
        match="changed outside ParaDev",
    ):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(localization),
                    "text": 'l_english:\n IDEA_ALPHA:0 "Blocked"\n',
                }
            ]
        )

    assert definition.read_text(encoding="utf-8") == "newer external edit"
    assert (project.root / ".paradev/source-draft-transaction/recovery.json").is_file()


def test_project_apply_source_draft_keeps_committed_write_after_cleanup_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    localization = module_root / "main.loc"
    clear_recovery = project_sdk._clear_source_draft_recovery

    def crash_before_cleanup(
        _authority: object,
        _journal: object,
    ) -> None:
        raise SystemExit("simulated committed cleanup exit")

    monkeypatch.setattr(
        project_sdk,
        "_clear_source_draft_recovery",
        crash_before_cleanup,
    )
    with pytest.raises(SystemExit, match="committed cleanup exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "committed draft",
                }
            ]
        )
    assert definition.read_text(encoding="utf-8") == "committed draft"

    monkeypatch.setattr(
        project_sdk,
        "_clear_source_draft_recovery",
        clear_recovery,
    )
    project.apply_source_draft(
        source_edits=[
            {
                "path": str(localization),
                "text": 'l_english:\n IDEA_ALPHA:0 "Next"\n',
            }
        ]
    )

    assert definition.read_text(encoding="utf-8") == "committed draft"
    assert list((project.root / ".paradev/source-draft-transaction").iterdir()) == []


def test_project_apply_source_draft_rejects_malformed_hidden_recovery_journal(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    definition = project.root / "src/modules/idea/IDEA_ALPHA/def.txt"
    original_definition = definition.read_bytes()
    recovery_root = project.root / ".paradev/source-draft-transaction"
    recovery_root.mkdir(parents=True)
    journal = recovery_root / "recovery.json"
    journal.write_text('{"schema":"untrusted"}\n', encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="fields are invalid|Unsupported source draft recovery schema",
    ):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "must not be written",
                }
            ]
        )

    assert definition.read_bytes() == original_definition
    assert journal.read_text(encoding="utf-8") == '{"schema":"untrusted"}\n'


def test_project_recovery_rejects_a_tampered_displacement_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "IDEA_ALPHA",
        values={"title": "Alpha"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    definition = module_root / "def.txt"
    protected = module_root / "protected.txt"
    protected.write_bytes(b"must remain untouched")
    write_source_draft_content = project_sdk._write_source_draft_content

    def crash_after_write(
        project_root: Path,
        path: Path,
        content: bytes,
        **kwargs: object,
    ) -> project_sdk._SourceDraftMutation:
        write_source_draft_content(
            project_root,
            path,
            content,
            **kwargs,
        )
        raise SystemExit("simulated abrupt process exit")

    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        crash_after_write,
    )
    with pytest.raises(SystemExit, match="abrupt process exit"):
        project.apply_source_draft(
            source_edits=[
                {
                    "path": str(definition),
                    "text": "interrupted draft",
                }
            ]
        )

    recovery = project.root / ".paradev/source-draft-transaction/recovery.json"
    payload = json.loads(recovery.read_text(encoding="utf-8"))
    payload["files"][0]["displaced"] = protected.relative_to(project.root).as_posix()
    recovery.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        project_sdk,
        "_write_source_draft_content",
        write_source_draft_content,
    )

    with pytest.raises(
        ValueError,
        match="displaced path does not match its transaction-owned location",
    ):
        project.build()

    assert definition.read_text(encoding="utf-8") == "interrupted draft"
    assert protected.read_bytes() == b"must remain untouched"
    assert recovery.is_file()


def test_project_apply_source_draft_synchronizes_every_affected_module_catalog_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    for object_id in ("IDEA_ALPHA", "IDEA_BETA"):
        project.scaffold_module("idea", object_id, values={"title": object_id}, write=True)
    synchronized: list[str] = []

    @project_sdk.contextmanager
    def catalog_scope(_project: Project, *, enabled: bool = True):
        assert enabled is True
        yield True

    def sync_catalog(
        _project: Project,
        *,
        previous: Mapping[str, object] | None = None,
        current: Mapping[str, object] | None = None,
        current_error: BaseException | None = None,
        _lock_held: bool = False,
    ) -> dict[str, object]:
        assert previous is not None
        assert current is not None
        assert current_error is None
        assert _lock_held is True
        synchronized.append(str(previous["module_id"]))
        return {
            "schema": "paradev.hb.catalog-mutation.v1",
            "status": "not_configured",
            "code": "catalog.mutation.not_configured",
            "database": str(project.root / ".paradev/.cache/hb/catalog.sqlite"),
        }

    monkeypatch.setattr(paradev_hb, "_module_catalog_mutation_scope", catalog_scope)
    monkeypatch.setattr(paradev_hb, "_sync_module_catalog_projection", sync_catalog)
    payload = project.apply_source_draft(
        source_edits=[
            {
                "path": str(project.root / f"src/modules/idea/{object_id}/def.txt"),
                "text": f"ideas = {{ {object_id} = {{}} }}\n",
            }
            for object_id in ("IDEA_ALPHA", "IDEA_BETA")
        ]
    )

    assert synchronized == ["idea/IDEA_ALPHA", "idea/IDEA_BETA"]
    assert payload["catalog_mutation"]["status"] == "not_configured"


@pytest.mark.parametrize(
    "revision",
    (
        {"expected_size": 1},
        {"expected_mtime_ns": "1"},
        {"expected_size": -1, "expected_mtime_ns": "1"},
        {"expected_size": 1, "expected_mtime_ns": "not-a-number"},
    ),
)
def test_project_apply_source_draft_rejects_invalid_expected_revisions(
    tmp_path: Path,
    revision: dict[str, object],
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    source = project.root / "notes.txt"
    source.write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="expected_"):
        project.apply_source_draft(source_edits=[{"path": str(source), "text": "new", **revision}])

    assert source.read_text(encoding="utf-8") == "old"


def test_project_apply_source_draft_rejects_edit_directories_before_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    notes = project.root / "notes.txt"
    nested = project.root / "nested"
    notes.write_text("old", encoding="utf-8")
    nested.mkdir()

    for directory in (project.root, nested):
        with pytest.raises(ValueError, match="Source edit path is not a file"):
            project.apply_source_draft(
                source_edits=[
                    {"path": str(notes), "text": "new"},
                    {"path": str(directory), "text": "invalid"},
                ]
            )
        assert notes.read_text(encoding="utf-8") == "old"


def test_project_apply_source_draft_preflights_structured_text_before_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/GER_industry_spirit"
    definition = module_root / "def.txt"
    record = module_root / "record.json"
    record.write_text('{"enabled": true}\n', encoding="utf-8")
    original_definition = definition.read_bytes()
    original_record = record.read_bytes()

    with pytest.raises(ValueError, match="Invalid JSON source draft"):
        project.apply_source_draft(
            source_edits=[
                {"path": str(definition), "text": "replacement"},
                {"path": str(record), "text": '{"enabled": NaN}'},
            ]
        )

    assert definition.read_bytes() == original_definition
    assert record.read_bytes() == original_record

    metadata = module_root / "meta.yaml"
    valid_metadata = metadata.read_text(encoding="utf-8").replace("German Industry Spirit", "Verified Industry Spirit")
    valid_record = '{"enabled": false}\n'
    payload = project.apply_source_draft(
        source_edits=[
            {"path": str(metadata), "text": valid_metadata},
            {"path": str(record), "text": valid_record},
        ]
    )

    assert payload["written"] is True
    assert metadata.read_text(encoding="utf-8") == valid_metadata
    assert record.read_text(encoding="utf-8") == valid_record


def test_project_apply_source_draft_rejects_invalid_yaml_without_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    metadata = project.root / "src/modules/idea/GER_industry_spirit/meta.yaml"
    original = metadata.read_bytes()

    with pytest.raises(ValueError, match="Invalid YAML source draft"):
        project.apply_source_draft(source_edits=[{"path": str(metadata), "text": "title: [unterminated"}])

    assert metadata.read_bytes() == original


@pytest.mark.parametrize(
    ("extension", "error_label"),
    (("json", "JSON"), ("yaml", "YAML")),
)
def test_project_apply_source_draft_normalizes_nested_structured_text_failures(
    tmp_path: Path,
    extension: str,
    error_label: str,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    source = project.root / f"src/modules/idea/GER_industry_spirit/nested.{extension}"
    source.write_text("[]\n", encoding="utf-8")
    original = source.read_bytes()
    nested = "[" * 10_000 + "0" + "]" * 10_000

    with pytest.raises(ValueError, match=rf"Invalid {error_label} source draft"):
        project.apply_source_draft(source_edits=[{"path": str(source), "text": nested}])

    assert source.read_bytes() == original


def test_project_apply_source_draft_rejects_recursive_yaml_aliases_without_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    source = project.root / "src/modules/idea/GER_industry_spirit/aliases.yaml"
    source.write_text("enabled: true\n", encoding="utf-8")
    original = source.read_bytes()

    with pytest.raises(ValueError, match="Invalid YAML source draft"):
        project.apply_source_draft(source_edits=[{"path": str(source), "text": "loop: &loop [*loop]\n"}])

    assert source.read_bytes() == original


def test_project_apply_source_draft_bounds_utf8_text_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    definition = project.root / "src/modules/idea/GER_industry_spirit/def.txt"
    original = definition.read_bytes()
    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_TEXT_BYTES", 3)

    with pytest.raises(ValueError, match="3-byte editor limit"):
        project.apply_source_draft(source_edits=[{"path": str(definition), "text": "éé"}])

    assert definition.read_bytes() == original


def test_project_apply_source_draft_invokes_family_text_validator_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    definition = project.root / "src/modules/idea/GER_industry_spirit/def.txt"
    original = definition.read_bytes()
    calls: list[tuple[str, str, str]] = []

    class DraftFamily:
        def validate_source_text(self, *, module_id: str, relative_path: str, text: str) -> None:
            calls.append((module_id, relative_path, text))
            raise ValueError("family contract rejected this source")

    class DraftRegistry:
        def family(self, family_id: str) -> object:
            assert family_id == "idea"
            return DraftFamily()

    monkeypatch.setattr(Project, "_build_registry", lambda _self, **_kwargs: DraftRegistry())

    with pytest.raises(ValueError, match="family contract rejected this source"):
        project.apply_source_draft(source_edits=[{"path": str(definition), "text": "replacement"}])

    assert calls == [("idea/GER_industry_spirit", "def.txt", "replacement")]
    assert definition.read_bytes() == original


def test_project_apply_source_draft_uses_logical_id_and_physical_suffixed_module_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    object_id = "TECHNOLOGY_AIR_CLOUDSHIP"
    module_id = f"technology/{object_id}"
    module_root = project.root / "src/modules/technology" / f"{object_id} - Cloudship technology"
    module_root.mkdir(parents=True)
    definition = module_root / "def.txt"
    definition.write_text("technologies = {}\n", encoding="utf-8")
    unsuffixed_root = project.root / "src/modules/technology" / object_id
    validation_calls: list[tuple[str, str, str]] = []
    lookup_calls: list[tuple[str, Path, tuple[Path, ...]]] = []
    projection_roots: list[tuple[str, str]] = []

    class DraftFamily:
        def validate_source_text(self, *, module_id: str, relative_path: str, text: str) -> None:
            validation_calls.append((module_id, relative_path, text))

    class DraftRegistry:
        def family(self, family_id: str) -> object:
            assert family_id == "technology"
            return DraftFamily()

    class FoundModule:
        def to_dict(self) -> dict[str, object]:
            return {
                "module_id": module_id,
                "family": "technology",
                "object_id": object_id,
                "root": str(module_root),
            }

    def find_module(
        lookup_project: Project,
        requested_module_id: str,
        *,
        source_root: Path | None = None,
    ) -> FoundModule:
        assert source_root is not None
        lookup_calls.append((requested_module_id, source_root, lookup_project.source_roots))
        return FoundModule()

    @project_sdk.contextmanager
    def catalog_scope(_project: Project, *, enabled: bool = True):
        assert enabled is True
        yield True

    def sync_catalog(
        _project: Project,
        *,
        previous: Mapping[str, object] | None = None,
        current: Mapping[str, object] | None = None,
        current_error: BaseException | None = None,
        _lock_held: bool = False,
    ) -> dict[str, object]:
        assert previous is not None
        assert current is not None
        assert current_error is None
        assert _lock_held is True
        projection_roots.append((str(previous["root"]), str(current["root"])))
        return {
            "schema": "paradev.hb.catalog-mutation.v1",
            "status": "not_configured",
            "code": "catalog.mutation.not_configured",
            "database": str(project.root / ".paradev/.cache/hb/catalog.sqlite"),
        }

    monkeypatch.setattr(Project, "_build_registry", lambda _self, **_kwargs: DraftRegistry())
    monkeypatch.setattr(project_sdk, "_find_project_module", find_module)
    monkeypatch.setattr(paradev_hb, "_module_catalog_mutation_scope", catalog_scope)
    monkeypatch.setattr(paradev_hb, "_sync_module_catalog_projection", sync_catalog)

    edited = "technologies = { TECHNOLOGY_AIR_CLOUDSHIP = {} }\n"
    payload = project.apply_source_draft(source_edits=[{"path": str(definition), "text": edited}])

    source_root = project.source_roots[0]
    assert validation_calls == [(module_id, "def.txt", edited)]
    assert lookup_calls == [
        (module_id, source_root, (source_root,)),
        (module_id, source_root, (source_root,)),
    ]
    assert projection_roots == [(str(module_root), str(module_root))]
    assert str(unsuffixed_root) not in {root for roots in projection_roots for root in roots}
    assert not unsuffixed_root.exists()
    assert definition.read_text(encoding="utf-8") == edited
    assert payload["catalog_mutation"]["status"] == "not_configured"


def test_project_apply_source_draft_rejects_filesystem_equivalent_targets_before_writing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    icon_path = "src/modules/idea/GER_industry_spirit/icon.png"
    icon = project.root / icon_path
    assert not icon.exists()

    with pytest.raises(ValueError, match="same filesystem path more than once"):
        project.apply_source_draft(
            source_replacements=[
                {"path": icon_path, "content_base64": "Zmlyc3Q="},
                {
                    "path": "src//modules/idea/GER_industry_spirit/icon.png",
                    "content_base64": "c2Vjb25k",
                },
            ]
        )

    assert not icon.exists()

    model_root = "src/modules/idea/GER_industry_spirit"
    with pytest.raises(ValueError, match="same filesystem path more than once"):
        project.apply_source_draft(
            source_replacements=[
                {"path": f"{model_root}/caf\u00e9.mesh", "content_base64": "Zmlyc3Q="},
                {"path": f"{model_root}/cafe\u0301.mesh", "content_base64": "c2Vjb25k"},
            ]
        )

    assert not (project.root / model_root / "caf\u00e9.mesh").exists()


def test_project_apply_source_draft_rejects_case_equivalent_and_cross_kind_targets(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    def_path = "src/modules/idea/GER_industry_spirit/def.txt"
    definition = project.root / def_path
    original = definition.read_bytes()

    with pytest.raises(ValueError, match="same filesystem path more than once"):
        project.apply_source_draft(
            source_edits=[{"path": def_path, "text": "replacement"}],
            source_replacements=[
                {
                    "path": "src/modules/idea/GER_industry_spirit/DEF.TXT",
                    "content_base64": "YmluYXJ5",
                }
            ],
        )

    assert definition.read_bytes() == original


def test_project_apply_source_draft_bounds_binary_replacement_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    module_root = "src/modules/idea/IDEA_ALPHA"
    first = project.root / module_root / "first.mesh"
    second = project.root / module_root / "second.anim"
    replacements = [
        {"path": f"{module_root}/first.mesh", "content_base64": "YWJj"},
        {"path": f"{module_root}/second.anim", "content_base64": "ZGVm"},
    ]

    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_REPLACEMENT_FILES", 1)
    with pytest.raises(ValueError, match="at most 1 files"):
        project.apply_source_draft(source_replacements=replacements)

    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_REPLACEMENT_FILES", 2)
    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_REPLACEMENT_FILE_BYTES", 2)
    with pytest.raises(ValueError, match="2-byte file limit"):
        project.apply_source_draft(source_replacements=replacements[:1])

    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_REPLACEMENT_FILE_BYTES", 3)
    monkeypatch.setattr(project_sdk, "MAX_PROJECT_SOURCE_REPLACEMENT_TOTAL_BYTES", 5)
    with pytest.raises(ValueError, match="5-byte total limit"):
        project.apply_source_draft(source_replacements=replacements)

    assert not first.exists()
    assert not second.exists()


def test_project_apply_source_draft_rejects_in_project_symlink_traversal(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    project.scaffold_module("idea", "IDEA_BETA", values={"title": "Beta"}, write=True)
    alpha_root = project.root / "src/modules/idea/IDEA_ALPHA"
    beta_root = project.root / "src/modules/idea/IDEA_BETA"
    link = alpha_root / "linked-model"
    try:
        link.symlink_to(beta_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": "src/modules/idea/IDEA_ALPHA/linked-model/mesh.mesh",
                    "content_base64": "YmluYXJ5",
                }
            ]
        )

    assert not (beta_root / "mesh.mesh").exists()


def test_project_apply_source_draft_normalizes_symlink_loop_failures(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    loop = project.root / "loop"
    try:
        loop.symlink_to("loop", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(
        ValueError,
        match=r"Source (?:path could not be resolved safely|draft path must not traverse a symlink)",
    ):
        project.apply_source_draft(source_replacements=[{"path": "loop/created.bin", "content_base64": "Y3JlYXRlZA=="}])


def test_project_apply_source_draft_normalizes_a_validation_time_root_symlink_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = Project.create(workspace / "starter", title="Starter Mod")
    drafts = project.root / "drafts"
    drafts.mkdir()
    target = drafts / "created.bin"
    displaced_workspace = tmp_path / "displaced-workspace"
    original_resolve = Path.resolve
    original_rename = os.rename
    swapped = False

    def swapping_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal swapped
        resolved = original_resolve(path, strict=strict)
        if not swapped and path == target:
            swapped = True
            original_rename(workspace, displaced_workspace)
            workspace.symlink_to("workspace", target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", swapping_resolve)

    with pytest.raises(
        ValueError,
        match=r"Source (?:path could not be resolved safely|path parent does not exist)",
    ):
        project.apply_source_draft(source_replacements=[{"path": str(target), "content_base64": "Y3JlYXRlZA=="}])

    assert swapped is True
    assert not (displaced_workspace / "starter/drafts/created.bin").exists()


def test_project_apply_source_draft_accepts_an_absolute_alias_to_the_project_root(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    alias_root = tmp_path / "starter-alias"
    try:
        alias_root.symlink_to(project.root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    aliased_definition = alias_root / "src/modules/idea/IDEA_ALPHA/def.txt"

    payload = project.apply_source_draft(source_edits=[{"path": str(aliased_definition), "text": "replacement"}])

    assert payload["written"] is True
    assert (project.root / "src/modules/idea/IDEA_ALPHA/def.txt").read_text(encoding="utf-8") == "replacement"


def test_project_apply_source_draft_accepts_case_aliases_with_a_missing_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    notes = project.root / "notes.txt"
    notes.write_text("old", encoding="utf-8")
    alias_root: Path | None = None
    root_parts = list(project.root.parts)
    for index, part in enumerate(root_parts[1:], start=1):
        changed = part.swapcase()
        if changed == part:
            continue
        candidate_parts = list(root_parts)
        candidate_parts[index] = changed
        candidate = Path(*candidate_parts)
        try:
            if candidate != project.root and os.path.samefile(candidate, project.root):
                alias_root = candidate
                break
        except OSError:
            continue
    if alias_root is None:
        pytest.skip("filesystem does not expose a case-insensitive alias for the project root")

    payload = project.apply_source_draft(
        source_edits=[{"path": str(alias_root / "notes.txt"), "text": "new"}],
        source_replacements=[{"path": str(alias_root / "created.bin"), "content_base64": "Y3JlYXRlZA=="}],
    )

    assert payload["written"] is True
    assert notes.read_text(encoding="utf-8") == "new"
    assert (project.root / "created.bin").read_bytes() == b"created"


def test_project_apply_source_draft_accepts_unicode_normalization_aliases(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    decomposed_root = project.root / "cafe\u0301"
    composed_root = project.root / "caf\u00e9"
    decomposed_root.mkdir()
    notes = decomposed_root / "notes.txt"
    notes.write_text("old", encoding="utf-8")
    try:
        aliases_existing_directory = os.path.samefile(composed_root, decomposed_root)
    except OSError:
        aliases_existing_directory = False
    if not aliases_existing_directory:
        pytest.skip("filesystem does not expose Unicode-normalization aliases")

    payload = project.apply_source_draft(
        source_edits=[{"path": str(composed_root / "notes.txt"), "text": "new"}],
        source_replacements=[
            {
                "path": str(composed_root / "created.bin"),
                "content_base64": "Y3JlYXRlZA==",
            }
        ],
    )

    assert payload["written"] is True
    assert notes.read_text(encoding="utf-8") == "new"
    assert (decomposed_root / "created.bin").read_bytes() == b"created"


def test_project_apply_source_draft_rejects_an_absolute_alias_to_a_project_subdirectory(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    alias_root = tmp_path / "module-alias"
    try:
        alias_root.symlink_to(module_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        project.apply_source_draft(source_edits=[{"path": str(alias_root / "def.txt"), "text": "replacement"}])


def test_project_apply_source_draft_is_anchored_when_parent_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module("idea", "IDEA_ALPHA", values={"title": "Alpha"}, write=True)
    module_root = project.root / "src/modules/idea/IDEA_ALPHA"
    model_root = module_root / "model"
    displaced_model = module_root / "displaced-model"
    external_model = tmp_path / "external-model"
    model_root.mkdir()
    external_model.mkdir()
    protected_file = external_model / "protected.txt"
    protected_file.write_text("keep", encoding="utf-8")
    original_rename = os.rename
    swapped = False

    def swapping_rename(source, target, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("src_dir_fd") is not None and os.fspath(target) == "mesh.mesh":
            swapped = True
            original_rename(model_root, displaced_model)
            model_root.symlink_to(external_model, target_is_directory=True)
        return original_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "rename", swapping_rename)

    payload = project.apply_source_draft(
        source_replacements=[
            {
                "path": "src/modules/idea/IDEA_ALPHA/model/mesh.mesh",
                "content_base64": "YmluYXJ5",
            }
        ]
    )

    assert payload["written"] is True
    assert model_root.is_symlink()
    assert (displaced_model / "mesh.mesh").read_bytes() == b"binary"
    assert not (external_model / "mesh.mesh").exists()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_project_apply_source_draft_rejects_a_replaced_project_root_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = Project.create(workspace / "starter", title="Starter Mod")
    drafts = project.root / "drafts"
    drafts.mkdir()
    displaced_workspace = tmp_path / "displaced-workspace"
    replacement_root = workspace / "starter"
    protected_file = replacement_root / "drafts/protected.txt"
    original_open = os.open
    original_rename = os.rename
    root_anchor = project.root.resolve().anchor
    swapped = False
    anchor_open_count = 0

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal anchor_open_count, swapped
        if dir_fd is None and os.fspath(path) == root_anchor and flags & os.O_NOFOLLOW:
            anchor_open_count += 1
            if anchor_open_count == 3:
                swapped = True
                original_rename(workspace, displaced_workspace)
                protected_file.parent.mkdir(parents=True)
                protected_file.write_text("keep", encoding="utf-8")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(ValueError, match="project root changed before mutation"):
        project.apply_source_draft(source_replacements=[{"path": "drafts/created.bin", "content_base64": "Y3JlYXRlZA=="}])

    assert swapped is True
    assert not (displaced_workspace / "starter/drafts/created.bin").exists()
    assert not (replacement_root / "drafts/created.bin").exists()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_project_apply_source_draft_verifies_identity_captured_before_root_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = Project.create(workspace / "starter", title="Starter Mod")
    drafts = project.root / "drafts"
    drafts.mkdir()
    displaced_workspace = tmp_path / "displaced-workspace"
    replacement_root = workspace / "starter"
    protected_file = replacement_root / "drafts/protected.txt"
    original_resolve = Path.resolve
    original_rename = os.rename
    swapped = False

    def swapping_resolve(path: Path, strict: bool = False) -> Path:
        nonlocal swapped
        resolved = original_resolve(path, strict=strict)
        if not swapped and path == project.root:
            swapped = True
            original_rename(workspace, displaced_workspace)
            protected_file.parent.mkdir(parents=True)
            protected_file.write_text("keep", encoding="utf-8")
        return resolved

    monkeypatch.setattr(Path, "resolve", swapping_resolve)

    with pytest.raises(ValueError, match="project root changed before validation"):
        project.apply_source_draft(source_replacements=[{"path": "drafts/created.bin", "content_base64": "Y3JlYXRlZA=="}])

    assert swapped is True
    assert not (displaced_workspace / "starter/drafts/created.bin").exists()
    assert not (replacement_root / "drafts/created.bin").exists()
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_project_apply_source_draft_normalizes_a_mutation_time_root_symlink_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = Project.create(workspace / "starter", title="Starter Mod")
    drafts = project.root / "drafts"
    drafts.mkdir()
    displaced_workspace = tmp_path / "displaced-workspace"
    original_open = os.open
    original_rename = os.rename
    root_anchor = project.root.anchor
    anchor_open_count = 0
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal anchor_open_count, swapped
        if dir_fd is None and os.fspath(path) == root_anchor and flags & os.O_NOFOLLOW:
            anchor_open_count += 1
            if anchor_open_count == 3:
                swapped = True
                original_rename(workspace, displaced_workspace)
                workspace.symlink_to("workspace", target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(ValueError, match="project root changed or traverses a symlink"):
        project.apply_source_draft(source_replacements=[{"path": "drafts/created.bin", "content_base64": "Y3JlYXRlZA=="}])

    assert swapped is True
    assert not (displaced_workspace / "starter/drafts/created.bin").exists()


def test_project_apply_source_draft_converts_png_to_existing_dds_and_build_uses_dds(
    tmp_path: Path,
) -> None:
    if not _imagemagick_converter_available():
        pytest.skip("ImageMagick is required for format-aware replacement.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/GER_industry_spirit"
    target = module_root / "icon.dds"
    target.write_bytes(b"old-dds")

    payload = project.apply_source_draft(
        source_replacements=[
            {
                "path": str(target),
                "content_base64": ONE_PIXEL_PNG_BASE64,
                "content_format": "png",
                "target_format": "dds",
            }
        ]
    )

    converted = target.read_bytes()
    assert payload["written"] is True
    assert converted.startswith(b"DDS ")
    assert converted[84:88] == b"DXT5"
    assert converted != b"old-dds"
    assert not (module_root / "icon.png").exists()
    module = next(module for module in project.discover_modules().modules if module.module_id == "idea/GER_industry_spirit")
    assert module.source_slots["icon"] == ("icon.dds",)

    result = project.build(module_id="idea/GER_industry_spirit", emit_artifacts=True)

    icon_artifact = next(artifact for artifact in result.artifacts if str(artifact.path) == "gfx/interface/ideas/idea_GER_industry_spirit.dds")
    assert icon_artifact.inputs == (target,)
    assert (project.output_root / icon_artifact.path).read_bytes() == converted


def test_project_apply_source_draft_converts_png_to_uncompressed_tga(
    tmp_path: Path,
) -> None:
    if not _imagemagick_converter_available():
        pytest.skip("ImageMagick is required for format-aware replacement.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.tga"
    target.write_bytes(b"old-tga")

    project.apply_source_draft(
        source_replacements=[
            {
                "path": str(target),
                "content_base64": ONE_PIXEL_PNG_BASE64,
                "content_format": "png",
                "target_format": "tga",
            }
        ]
    )

    converted = target.read_bytes()
    assert len(converted) >= 18
    assert converted[2] == 2
    assert int.from_bytes(converted[12:14], "little") == 1
    assert int.from_bytes(converted[14:16], "little") == 1


@pytest.mark.parametrize(
    ("target_format", "expected_prefix"),
    [
        ("jpg", b"\xff\xd8\xff"),
        ("jpeg", b"\xff\xd8\xff"),
        ("webp", b"RIFF"),
        ("bmp", b"BM"),
    ],
)
def test_project_apply_source_draft_converts_png_to_other_advertised_image_formats(
    tmp_path: Path,
    target_format: str,
    expected_prefix: bytes,
) -> None:
    if not _imagemagick_converter_available():
        pytest.skip("ImageMagick is required for format-aware replacement.")
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit" / f"icon.{target_format}"
    target.write_bytes(b"old-image")

    project.apply_source_draft(
        source_replacements=[
            {
                "path": str(target),
                "content_base64": ONE_PIXEL_PNG_BASE64,
                "content_format": "png",
                "target_format": target_format,
            }
        ]
    )

    converted = target.read_bytes()
    assert converted.startswith(expected_prefix)
    if target_format == "webp":
        assert converted[8:12] == b"WEBP"
    assert project_sdk._converted_source_image_error(converted, target_format=target_format) is None


def test_project_apply_source_draft_conversion_dependency_failure_preserves_every_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    module_root = project.root / "src/modules/idea/GER_industry_spirit"
    target = module_root / "icon.dds"
    definition = module_root / "def.txt"
    target.write_bytes(b"old-dds")
    previous_definition = definition.read_text(encoding="utf-8")
    scratch_directories: list[Path] = []

    def missing_converter(args: list[str], **_kwargs):
        if args[0] == "magick":
            scratch_directories.append(Path(args[1]).parent)
        raise FileNotFoundError("missing ImageMagick")

    monkeypatch.setattr(project_sdk, "cmd", missing_converter)

    with pytest.raises(ValueError, match="requires ImageMagick.*magick.*convert"):
        project.apply_source_draft(
            source_edits=[{"path": str(definition), "text": "changed = yes\n"}],
            source_replacements=[
                {
                    "path": str(target),
                    "content_base64": ONE_PIXEL_PNG_BASE64,
                    "content_format": "png",
                    "target_format": "dds",
                }
            ],
        )

    assert target.read_bytes() == b"old-dds"
    assert definition.read_text(encoding="utf-8") == previous_definition
    assert not any(path.name.startswith(".icon.dds.") for path in module_root.iterdir())
    assert scratch_directories
    assert all(not directory.is_relative_to(project.root) for directory in scratch_directories)
    assert all(not directory.exists() for directory in scratch_directories)


def test_project_apply_source_draft_falls_back_to_convert_with_argv_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.dds"
    target.write_bytes(b"old-dds")
    converted = bytearray(128)
    converted[:4] = b"DDS "
    converted[84:88] = b"DXT5"
    calls: list[tuple[list[str], bool | None]] = []
    scratch_directories: list[Path] = []

    def fallback_converter(args: list[str], *, shell: bool | None = None, **_kwargs):
        calls.append((args, shell))
        if args[0] == "magick":
            raise FileNotFoundError("magick is not installed")
        if args == ["convert", "-version"]:
            return project_sdk.CmdResult(args, 0, out="Version: ImageMagick 6.9.13")
        scratch_directories.append(Path(args[1]).parent)
        project_sdk.save_bin(bytes(converted), args[-1])
        return project_sdk.CmdResult(args, 0)

    monkeypatch.setattr(project_sdk, "cmd", fallback_converter)
    monkeypatch.setattr(project_sdk.sys, "platform", "linux")

    project.apply_source_draft(
        source_replacements=[
            {
                "path": str(target),
                "content_base64": ONE_PIXEL_PNG_BASE64,
                "content_format": "png",
                "target_format": "dds",
            }
        ]
    )

    assert target.read_bytes() == bytes(converted)
    assert [args[0] for args, _shell in calls] == ["magick", "convert", "convert"]
    assert calls[1][0] == ["convert", "-version"]
    assert all(shell is False for _args, shell in calls)
    conversion_args = calls[2][0]
    assert conversion_args[2:4] == ["-define", "dds:compression=dxt5"]
    assert conversion_args[1].endswith("source.png")
    assert conversion_args[-1].endswith("converted.dds")
    assert scratch_directories
    assert all(not directory.is_relative_to(project.root) for directory in scratch_directories)
    assert all(not directory.exists() for directory in scratch_directories)


def test_project_apply_source_draft_never_invokes_windows_convert_utility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.dds"
    target.write_bytes(b"old-dds")
    calls: list[list[str]] = []
    scratch_directories: list[Path] = []

    def missing_magick(args: list[str], **_kwargs):
        calls.append(args)
        scratch_directories.append(Path(args[1]).parent)
        raise FileNotFoundError("magick is not installed")

    monkeypatch.setattr(project_sdk, "cmd", missing_magick)
    monkeypatch.setattr(project_sdk.sys, "platform", "win32")

    with pytest.raises(ValueError, match="requires ImageMagick.*magick.*convert"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(target),
                    "content_base64": ONE_PIXEL_PNG_BASE64,
                    "content_format": "png",
                    "target_format": "dds",
                }
            ]
        )

    assert [args[0] for args in calls] == ["magick"]
    assert target.read_bytes() == b"old-dds"
    assert all(not directory.is_relative_to(project.root) for directory in scratch_directories)
    assert all(not directory.exists() for directory in scratch_directories)


def test_project_apply_source_draft_rejects_non_imagemagick_convert_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.dds"
    target.write_bytes(b"old-dds")
    calls: list[list[str]] = []

    def unrelated_convert(args: list[str], **_kwargs):
        calls.append(args)
        if args[0] == "magick":
            raise FileNotFoundError("magick is not installed")
        if args == ["convert", "-version"]:
            return project_sdk.CmdResult(args, 0, out="Unrelated convert utility 1.0")
        pytest.fail("unrelated convert utility was used for image conversion")

    monkeypatch.setattr(project_sdk, "cmd", unrelated_convert)
    monkeypatch.setattr(project_sdk.sys, "platform", "linux")

    with pytest.raises(ValueError, match="requires ImageMagick.*magick.*convert"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(target),
                    "content_base64": ONE_PIXEL_PNG_BASE64,
                    "content_format": "png",
                    "target_format": "dds",
                }
            ]
        )

    assert [args[0] for args in calls] == ["magick", "convert"]
    assert calls[1] == ["convert", "-version"]
    assert target.read_bytes() == b"old-dds"


@pytest.mark.parametrize(
    ("target_name", "content_base64", "format_fields", "message"),
    [
        (
            "icon.dds",
            "bm90LXBuZw==",
            {"content_format": "png", "target_format": "dds"},
            "must contain PNG bytes",
        ),
        (
            "icon.tga",
            ONE_PIXEL_PNG_BASE64,
            {"content_format": "png", "target_format": "dds"},
            "must match target suffix",
        ),
        (
            "icon.dds",
            ONE_PIXEL_PNG_BASE64,
            {"content_format": "png"},
            "provide content_format and target_format together",
        ),
        (
            "icon.dds",
            ONE_PIXEL_PNG_BASE64,
            {"content_format": "png", "target_format": "png"},
            "must be one of",
        ),
    ],
)
def test_project_apply_source_draft_validates_format_aware_replacements_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    content_base64: str,
    format_fields: dict[str, str],
    message: str,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit" / target_name
    target.write_bytes(b"old-image")
    monkeypatch.setattr(
        project_sdk,
        "cmd",
        lambda *_args, **_kwargs: pytest.fail("invalid replacement invoked ImageMagick"),
    )

    with pytest.raises(ValueError, match=message):
        project.apply_source_draft(source_replacements=[{"path": str(target), "content_base64": content_base64, **format_fields}])

    assert target.read_bytes() == b"old-image"


def test_project_apply_source_draft_format_conversion_requires_existing_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.dds"

    with pytest.raises(ValueError, match="must name an existing file for format conversion"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(target),
                    "content_base64": ONE_PIXEL_PNG_BASE64,
                    "content_format": "png",
                    "target_format": "dds",
                }
            ]
        )

    assert not target.exists()


@pytest.mark.parametrize("converted", [b"", b"not-a-dds"])
def test_project_apply_source_draft_rejects_invalid_converter_output_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    converted: bytes,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")
    project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )
    target = project.root / "src/modules/idea/GER_industry_spirit/icon.dds"
    target.write_bytes(b"old-dds")

    def invalid_conversion(args, **_kwargs):
        if args == ["convert", "-version"]:
            return project_sdk.CmdResult(args, 0, out="Version: ImageMagick 6.9.13")
        project_sdk.save_bin(converted, args[-1])
        return project_sdk.CmdResult(args, 0)

    monkeypatch.setattr(project_sdk, "cmd", invalid_conversion)

    with pytest.raises(ValueError, match="conversion failed without changing the source"):
        project.apply_source_draft(
            source_replacements=[
                {
                    "path": str(target),
                    "content_base64": ONE_PIXEL_PNG_BASE64,
                    "content_format": "png",
                    "target_format": "dds",
                }
            ]
        )

    assert target.read_bytes() == b"old-dds"


def test_module_batch_edit_request_builds_pihc3_style_request(tmp_path: Path) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)
    cloudship_meta = "type: technology\ntitle: Updated Cloudship Design\n"
    solar_notes = "generated during PIHC3 migration batch\n"

    payload = project.module_batch_edit_request(
        [
            {
                "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                "relative_path": "meta.yaml",
                "text": cloudship_meta,
            },
            {
                "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                "relative_path": "migration/notes.txt",
                "text": solar_notes,
                "create": "true",
            },
        ],
        encoding="utf-8",
    )

    assert payload["schema"] == "paradev.module.batch_edit_request.v1"
    assert payload["project_id"] == "pihc3_batch_authoring"
    assert payload["create"] is False
    assert payload["encoding"] == "utf-8"
    assert payload["edit_count"] == 2
    assert payload["summary"] == {
        "edit_count": 2,
        "module_count": 2,
        "source_root_count": 0,
        "existing_target_count": 1,
        "missing_target_count": 1,
        "changed_target_count": 2,
        "unchanged_target_count": 0,
        "create_enabled_count": 1,
        "encoding_count": 1,
    }
    assert payload["edits"] == [
        {
            "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
            "relative_path": "meta.yaml",
            "text": cloudship_meta,
        },
        {
            "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
            "relative_path": "migration/notes.txt",
            "text": solar_notes,
            "create": True,
        },
    ]
    assert payload["targets"] == [
        {
            "edit_index": 0,
            "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
            "family": "technology",
            "object_id": "TECHNOLOGY_AIR_CLOUDSHIP",
            "relative_path": "meta.yaml",
            "target_relative_path": "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/meta.yaml",
            "exists": True,
            "created": False,
            "changed": True,
            "create": False,
            "encoding": "utf-8",
            "size_bytes": len(cloudship_meta.encode("utf-8")),
        },
        {
            "edit_index": 1,
            "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
            "family": "technology",
            "object_id": "TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
            "relative_path": "migration/notes.txt",
            "target_relative_path": "src/modules/technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR/migration/notes.txt",
            "exists": False,
            "created": True,
            "changed": True,
            "create": True,
            "encoding": "utf-8",
            "size_bytes": len(solar_notes.encode("utf-8")),
        },
    ]
    assert payload["index"]["module_id"] == {
        "technology/TECHNOLOGY_AIR_CLOUDSHIP": [0],
        "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR": [1],
    }
    assert payload["target_index"]["created"] == {"false": [0], "true": [1]}
    assert payload["target_index"]["target_relative_path"] == {
        "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/meta.yaml": [0],
        "src/modules/technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR/migration/notes.txt": [1],
    }
    preview = project.write_module_files(
        payload["edits"],
        create=payload["create"],
        encoding=payload["encoding"],
        write=False,
    )
    assert preview["schema"] == "paradev.module.batch_edit.v1"
    assert preview["written"] is False
    assert preview["file_count"] == 2


def test_module_batch_edit_request_rejects_unknown_pihc3_module_targets(
    tmp_path: Path,
) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="Unknown module: technology/TECHNOLOGY_DOES_NOT_EXIST\\."):
        project.module_batch_edit_request(
            [
                {
                    "module_id": "technology/TECHNOLOGY_DOES_NOT_EXIST",
                    "relative_path": "migration/notes.txt",
                    "text": "generated during PIHC3 migration batch\n",
                    "create": True,
                }
            ]
        )


def test_module_batch_edit_request_rejects_missing_pihc3_file_targets_without_create(
    tmp_path: Path,
) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)

    with pytest.raises(
        ValueError,
        match=("Module file does not exist: src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/migration/notes\\.txt\\. " "Pass create=True to create it\\."),
    ):
        project.module_batch_edit_request(
            [
                {
                    "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                    "relative_path": "migration/notes.txt",
                    "text": "generated during PIHC3 migration batch\n",
                }
            ]
        )


def test_module_files_batch_edit_writes_pihc3_style_updates(tmp_path: Path) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)
    meta_path = tmp_path / "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/meta.yaml"
    original_meta = meta_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="Module file does not exist"):
        project.write_module_files(
            [
                {
                    "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                    "relative_path": "meta.yaml",
                    "text": "type: technology\ntitle: Updated Cloudship Design\n",
                },
                {
                    "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                    "relative_path": "missing.txt",
                    "text": "missing\n",
                },
            ]
        )
    assert meta_path.read_text(encoding="utf-8") == original_meta

    payload = project.write_module_files(
        [
            {
                "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                "relative_path": "meta.yaml",
                "text": "type: technology\ntitle: Updated Cloudship Design\n",
            },
            {
                "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                "relative_path": "main.loc",
                "text": 'l_english:\n  TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR: "Solar Airframe"\n',
            },
            {
                "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                "relative_path": "migration/notes.txt",
                "text": "generated during PIHC3 migration batch\n",
                "create": True,
            },
        ]
    )

    assert payload["schema"] == "paradev.module.batch_edit.v1"
    assert payload["project_id"] == "pihc3_batch_authoring"
    assert payload["written"] is True
    assert payload["file_count"] == 3
    assert payload["created_count"] == 1
    assert payload["updated_count"] == 2
    assert [row["created"] for row in payload["files"]] == [False, False, True]
    assert [row["module_relative_path"] for row in payload["files"]] == [
        "meta.yaml",
        "main.loc",
        "migration/notes.txt",
    ]
    assert payload["index"]["module_id"] == {
        "technology/TECHNOLOGY_AIR_CLOUDSHIP": [0],
        "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR": [1, 2],
    }
    assert meta_path.read_text(encoding="utf-8") == "type: technology\ntitle: Updated Cloudship Design\n"
    assert (tmp_path / "src/modules/technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR/migration/notes.txt").read_text(encoding="utf-8") == (
        "generated during PIHC3 migration batch\n"
    )


def test_module_files_batch_edit_can_preview_pihc3_style_updates(
    tmp_path: Path,
) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)
    meta_path = tmp_path / "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/meta.yaml"
    notes_path = tmp_path / "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/migration/notes.txt"
    original_meta = meta_path.read_text(encoding="utf-8")

    payload = project.write_module_files(
        [
            {
                "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                "relative_path": "meta.yaml",
                "text": "type: technology\ntitle: Previewed Cloudship Design\n",
            },
            {
                "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                "relative_path": "migration/notes.txt",
                "text": "generated during PIHC3 migration preview\n",
                "create": True,
            },
        ],
        write=False,
    )

    assert payload["schema"] == "paradev.module.batch_edit.v1"
    assert payload["written"] is False
    assert payload["file_count"] == 2
    assert payload["created_count"] == 1
    assert payload["updated_count"] == 1
    assert [row["written"] for row in payload["files"]] == [False, False]
    assert [row["created"] for row in payload["files"]] == [False, True]
    assert [row["module_relative_path"] for row in payload["files"]] == [
        "meta.yaml",
        "migration/notes.txt",
    ]
    assert meta_path.read_text(encoding="utf-8") == original_meta
    assert not notes_path.exists()


def test_module_files_batch_edit_reports_changed_and_unchanged_pihc3_updates(
    tmp_path: Path,
) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)
    cloudship_meta = "type: technology\ntitle: Cloudship Design\n"

    payload = project.write_module_files(
        [
            {
                "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                "relative_path": "meta.yaml",
                "text": cloudship_meta,
            },
            {
                "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                "relative_path": "main.loc",
                "text": 'l_english:\n  TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR: "Solar Airframe"\n',
            },
            {
                "module_id": "technology/TECHNOLOGY_BBA_AIR_FIXEDWING_SOLAR",
                "relative_path": "migration/rerun-notes.txt",
                "text": "generated during PIHC3 rerun\n",
                "create": True,
            },
        ]
    )

    assert payload["schema"] == "paradev.module.batch_edit.v1"
    assert payload["file_count"] == 3
    assert payload["created_count"] == 1
    assert payload["updated_count"] == 2
    assert payload["changed_count"] == 2
    assert payload["unchanged_count"] == 1
    assert [row["created"] for row in payload["files"]] == [False, False, True]
    assert [row["changed"] for row in payload["files"]] == [False, True, True]


def test_module_files_batch_edit_does_not_rewrite_unchanged_pihc3_targets(
    tmp_path: Path,
) -> None:
    _write_pihc3_batch_authoring_project(tmp_path)
    project = Project.load(tmp_path)
    meta_path = tmp_path / "src/modules/technology/TECHNOLOGY_AIR_CLOUDSHIP/meta.yaml"
    original_meta = meta_path.read_text(encoding="utf-8")

    meta_path.chmod(0o444)
    try:
        payload = project.write_module_files(
            [
                {
                    "module_id": "technology/TECHNOLOGY_AIR_CLOUDSHIP",
                    "relative_path": "meta.yaml",
                    "text": original_meta,
                }
            ]
        )
    finally:
        meta_path.chmod(0o644)

    assert payload["schema"] == "paradev.module.batch_edit.v1"
    assert payload["written"] is True
    assert payload["file_count"] == 1
    assert payload["changed_count"] == 0
    assert payload["unchanged_count"] == 1
    assert payload["files"][0]["changed"] is False
    assert payload["files"][0]["written"] is False
    assert meta_path.read_text(encoding="utf-8") == original_meta


def test_module_file_rejects_path_escape_and_missing_write_target(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter", title="Starter Mod")

    with pytest.raises(ValueError, match="Module file path must stay inside module root"):
        project.read_module_file("modifier/starter_starter_modifier", "../paradev.yaml")

    with pytest.raises(ValueError, match="Module file does not exist"):
        project.write_module_file("modifier/starter_starter_modifier", "notes/design.txt", "notes\n")


def test_collection_file_read_and_write_text_source(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    source = project.read_collection_file("germany", "category.txt")

    assert source["schema"] == "paradev.collection.file.v1"
    assert source["collection_id"] == "germany"
    assert source["family"] == "bulletin"
    assert source["collection_relative_path"] == "category.txt"
    assert source["relative_path"] == "src/collections/bulletin/germany/category.txt"
    assert source["encoding"] == "utf-8"
    assert source["exists"] is True
    assert source["text"] == "add_namespace = germany"
    assert source["size"] == len(source["text"].encode("utf-8"))
    assert source["mtime_ns"] == str((tmp_path / "src/collections/bulletin/germany/category.txt").stat().st_mtime_ns)

    text = "add_namespace = germany_news\n"
    written = project.write_collection_file("germany", "category.txt", text)

    assert written["schema"] == "paradev.collection.file.v1"
    assert written["written"] is True
    assert written["text"] == text
    assert (tmp_path / "src/collections/bulletin/germany/category.txt").read_text(encoding="utf-8") == text


def test_collection_file_write_can_create_nested_text_source(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.write_collection_file("germany", "notes/design.txt", "notes\n", create=True)

    assert payload["collection_relative_path"] == "notes/design.txt"
    assert payload["written"] is True
    assert (tmp_path / "src/collections/bulletin/germany/notes/design.txt").read_text(encoding="utf-8") == "notes\n"


def test_collection_file_rejects_path_escape_and_missing_write_target(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="Collection file path must stay inside collection root"):
        project.read_collection_file("germany", "../paradev.yaml")

    with pytest.raises(ValueError, match="Collection file does not exist"):
        project.write_collection_file("germany", "notes/design.txt", "notes\n")


def test_collection_file_can_select_duplicate_collection_id_family(
    tmp_path: Path,
) -> None:
    for family, text in (
        ("bulletin", "add_namespace = bulletin"),
        ("dossier", "add_namespace = dossier"),
    ):
        collection_root = tmp_path / f"src/collections/{family}/germany"
        collection_root.mkdir(parents=True)
        (collection_root / "category.txt").write_text(text, encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: collection_duplicates",
                "title: Collection Duplicates",
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
                "    templates:",
                "      pdx: events/{collection_id}.txt",
                "  dossier:",
                "    kind: collection_source",
                "    collection_source_slots:",
                "      - name: category",
                "        match: category.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/dossiers/{collection_id}.txt",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="ambiguous"):
        project.read_collection_file("germany", "category.txt")

    payload = project.read_collection_file("germany", "category.txt", family="dossier")

    assert payload["family"] == "dossier"
    assert payload["relative_path"] == "src/collections/dossier/germany/category.txt"
    assert payload["text"] == "add_namespace = dossier"


def test_create_collection_plans_and_writes_descriptor_metadata(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    dry = project.create_collection("bulletin", "france", metadata={"title": "France Bulletin"})

    assert dry["schema"] == "paradev.collection.create.v1"
    assert dry["family"] == "bulletin"
    assert dry["collection_id"] == "france"
    assert dry["blocked"] is False
    assert dry["written"] is False
    assert dry["metadata"] == {"title": "France Bulletin"}
    assert dry["files"] == [
        {
            "role": "metadata",
            "path": str(tmp_path / "src/collections/bulletin/france/meta.yaml"),
            "relative_path": "src/collections/bulletin/france/meta.yaml",
            "collection_relative_path": "meta.yaml",
            "exists": False,
            "will_write": True,
            "force": False,
        }
    ]
    assert dry["authoring_plan"]["authoring_path"]["collection_id"] == "france"
    assert not (tmp_path / "src/collections/bulletin/france/meta.yaml").exists()

    written = project.create_collection("bulletin", "france", metadata={"title": "France Bulletin"}, write=True)

    assert written["written"] is True
    assert written["blocked"] is False
    assert (tmp_path / "src/collections/bulletin/france/meta.yaml").read_text(encoding="utf-8") == "title: France Bulletin\n"
    collections = Project.load(tmp_path).collections(collection_id="france")
    assert collections["collections"][0]["family"] == "bulletin"
    assert collections["collections"][0]["metadata"]["title"] == "France Bulletin"


def test_create_collection_rejects_existing_metadata_without_force(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    project.create_collection("bulletin", "france", metadata={"title": "France Bulletin"}, write=True)

    blocked = project.create_collection("bulletin", "france", metadata={"title": "Updated Bulletin"}, write=True)

    assert blocked["blocked"] is True
    assert blocked["written"] is False
    assert blocked["diagnostics"] == [
        {
            "severity": "error",
            "code": "collection_create.file_exists",
            "message": "Collection metadata file already exists.",
            "path": str(tmp_path / "src/collections/bulletin/france/meta.yaml"),
            "relative_path": "src/collections/bulletin/france/meta.yaml",
        }
    ]
    assert "Updated Bulletin" not in (tmp_path / "src/collections/bulletin/france/meta.yaml").read_text(encoding="utf-8")

    forced = project.create_collection(
        "bulletin",
        "france",
        metadata={"title": "Updated Bulletin"},
        write=True,
        force=True,
    )

    assert forced["blocked"] is False
    assert forced["written"] is True
    assert "Updated Bulletin" in (tmp_path / "src/collections/bulletin/france/meta.yaml").read_text(encoding="utf-8")


def test_create_collection_blocks_non_directory_descriptor_path(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    descriptor_path = tmp_path / "src/collections/bulletin/france"
    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor_path.write_text("not a directory", encoding="utf-8")
    project = Project.load(tmp_path)

    blocked = project.create_collection("bulletin", "france", write=True)

    assert blocked["blocked"] is True
    assert blocked["written"] is False
    assert blocked["diagnostics"] == [
        {
            "severity": "error",
            "code": "collection_create.path_not_directory",
            "message": "Collection descriptor path exists and is not a directory.",
            "path": str(descriptor_path),
            "relative_path": "src/collections/bulletin/france",
        }
    ]


def test_collection_rename_moves_descriptor_and_rewrites_member_metadata(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    previous_root = tmp_path / "src/collections/bulletin/germany"
    previous_content = (previous_root / "category.txt").read_text(encoding="utf-8")

    payload = project.rename_collection("germany", "france", family="bulletin")

    new_root = tmp_path / "src/collections/bulletin/france"
    assert payload["schema"] == "paradev.collection.rename.v1"
    assert payload["previous_collection_id"] == "germany"
    assert payload["collection_id"] == "france"
    assert payload["family"] == "bulletin"
    assert payload["source_root"] == str(tmp_path / "src")
    assert payload["previous_root"] == str(previous_root)
    assert payload["root"] == str(new_root)
    assert payload["previous_relative_path"] == "src/collections/bulletin/germany"
    assert payload["relative_path"] == "src/collections/bulletin/france"
    assert payload["content_rewritten"] is True
    assert payload["member_count"] == 1
    assert payload["members"] == ["bulletin/GER_news"]
    assert [(row["layer"], row["action"]) for row in payload["files"]] == [("visible", "update")]
    assert payload["collection"]["collection_id"] == "france"
    assert not previous_root.exists()
    assert (new_root / "category.txt").read_text(encoding="utf-8") == previous_content
    assert Project.load(tmp_path).read_collection_file("france", "category.txt", family="bulletin")["text"] == previous_content
    with pytest.raises(ValueError, match="Unknown collection: germany"):
        Project.load(tmp_path).read_collection_file("germany", "category.txt", family="bulletin")
    assert (tmp_path / "src/modules/bulletin/GER_news/meta.yaml").read_text(encoding="utf-8") == "type: bulletin\ncollection: france\n"


def test_collection_rename_rewrites_hidden_membership_and_preserves_settings(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    module_root = tmp_path / "src/modules/bulletin/GER_news"
    (module_root / "meta.yaml").write_text("type: bulletin\n", encoding="utf-8")
    hidden = module_root / ".paradev/meta.yaml"
    hidden.parent.mkdir()
    hidden.write_text(
        "collection: germany\nsettings:\n  display: compact\n",
        encoding="utf-8",
    )

    payload = Project.load(tmp_path).rename_collection(
        "germany",
        "france",
        family="bulletin",
    )

    assert payload["content_rewritten"] is True
    assert [(row["layer"], row["action"]) for row in payload["files"]] == [("hidden", "update")]
    assert hidden.read_text(encoding="utf-8") == ("collection: france\nsettings:\n  display: compact\n")


def test_collection_rename_rolls_back_member_metadata_when_folder_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_collection_slot_project(tmp_path)
    previous_root = tmp_path / "src/collections/bulletin/germany"
    target_root = tmp_path / "src/collections/bulletin/france"
    metadata = tmp_path / "src/modules/bulletin/GER_news/meta.yaml"
    original = metadata.read_bytes()

    def fail_collection_rename(
        _project: Project,
        _prepared: project_sdk._PreparedCollectionRename,
    ) -> None:
        raise ValueError("simulated collection rename failure")

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_rename",
        fail_collection_rename,
    )

    with pytest.raises(ValueError, match="simulated collection rename failure"):
        Project.load(tmp_path).rename_collection(
            "germany",
            "france",
            family="bulletin",
        )

    assert metadata.read_bytes() == original
    assert previous_root.is_dir()
    assert not target_root.exists()


def test_collection_rename_recovers_abrupt_folder_move_and_member_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_collection_slot_project(tmp_path)
    previous_root = tmp_path / "src/collections/bulletin/germany"
    target_root = tmp_path / "src/collections/bulletin/france"
    metadata = tmp_path / "src/modules/bulletin/GER_news/meta.yaml"
    original = metadata.read_bytes()
    commit = project_sdk._commit_prepared_collection_rename

    def crash_after_collection_rename(
        project: Project,
        prepared: project_sdk._PreparedCollectionRename,
    ) -> None:
        commit(project, prepared)
        raise SystemExit("simulated abrupt collection rename exit")

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_rename",
        crash_after_collection_rename,
    )
    with pytest.raises(SystemExit, match="abrupt collection rename exit"):
        Project.load(tmp_path).rename_collection(
            "germany",
            "france",
            family="bulletin",
        )

    assert not previous_root.exists()
    assert target_root.is_dir()
    assert b"collection: france" in metadata.read_bytes()

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_rename",
        commit,
    )
    result = Project.load(tmp_path).build()

    assert result.blocked is False
    assert previous_root.is_dir()
    assert not target_root.exists()
    assert metadata.read_bytes() == original


def test_collection_rename_rejects_existing_destination(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    existing = tmp_path / "src/collections/bulletin/france"
    existing.mkdir(parents=True)
    (existing / "category.txt").write_text("add_namespace = france", encoding="utf-8")
    project = Project.load(tmp_path)

    with pytest.raises(ValueError, match="Collection target already exists"):
        project.rename_collection("germany", "france", family="bulletin")


def test_collection_rename_preserves_readable_folder_title(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    previous = tmp_path / "src/collections/bulletin/germany"
    titled = previous.with_name("germany - 德国")
    previous.rename(titled)

    renamed = Project.load(tmp_path).rename_collection(
        "germany",
        "france",
        family="bulletin",
    )

    target = titled.with_name("france - 德国")
    assert renamed["root"] == str(target)
    assert target.is_dir()
    assert not titled.exists()


def test_collection_remove_plans_and_removes_descriptor(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    collection_root = tmp_path / "src/collections/bulletin/germany"
    (collection_root / "notes").mkdir()
    (collection_root / "notes/design.txt").write_text("notes\n", encoding="utf-8")

    dry = project.remove_collection("germany", family="bulletin")

    assert dry["schema"] == "paradev.collection.remove.v1"
    assert dry["collection_id"] == "germany"
    assert dry["family"] == "bulletin"
    assert dry["source_root"] == str(tmp_path / "src")
    assert dry["root"] == str(collection_root)
    assert dry["relative_path"] == "src/collections/bulletin/germany"
    assert dry["blocked"] is False
    assert dry["removed"] is False
    assert dry["status"] == "planned"
    assert len(dry["plan_hash"]) == 64
    assert dry["collection"]["collection_id"] == "germany"
    assert dry["members"] == ["bulletin/GER_news"]
    assert [item["relative_path"] for item in dry["member_files"]] == ["src/modules/bulletin/GER_news/meta.yaml"]
    assert [item["collection_relative_path"] for item in dry["files"]] == [
        "category.txt",
        "notes/design.txt",
        "strings.yml",
    ]
    assert all(item["will_remove"] is True for item in dry["files"])
    assert collection_root.is_dir()

    unguarded = project.remove_collection(
        "germany",
        family="bulletin",
        write=True,
    )
    assert unguarded["blocked"] is True
    assert unguarded["diagnostics"][0]["code"] == ("collection_remove.plan_hash_required")
    assert collection_root.is_dir()

    removed = project.remove_collection(
        "germany",
        family="bulletin",
        write=True,
        plan_hash=dry["plan_hash"],
    )

    assert removed["blocked"] is False
    assert removed["removed"] is True
    assert removed["status"] == "removed"
    assert not collection_root.exists()
    current = Project.load(tmp_path)
    assert current.discover_collections().collections == ()
    assert current.discover_modules(module_id="bulletin/GER_news").modules[0].collection_id is None
    assert (tmp_path / "src/modules/bulletin/GER_news/meta.yaml").read_text(encoding="utf-8") == "type: bulletin\n"
    with pytest.raises(ValueError, match="Unknown collection: germany"):
        Project.load(tmp_path).read_collection_file("germany", "category.txt", family="bulletin")


def test_collection_remove_clears_hidden_membership_and_preserves_settings(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    module_root = tmp_path / "src/modules/bulletin/GER_news"
    (module_root / "meta.yaml").write_text("type: bulletin\n", encoding="utf-8")
    hidden = module_root / ".paradev/meta.yaml"
    hidden.parent.mkdir()
    hidden.write_text(
        "collection: germany\nsettings:\n  color: blue\n",
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    plan = project.remove_collection("germany", family="bulletin")
    removed = project.remove_collection(
        "germany",
        family="bulletin",
        write=True,
        plan_hash=plan["plan_hash"],
    )

    assert removed["removed"] is True
    assert removed["members"] == ["bulletin/GER_news"]
    assert [row["layer"] for row in removed["member_files"]] == ["hidden"]
    assert hidden.read_text(encoding="utf-8") == "settings:\n  color: blue\n"
    current = Project.load(tmp_path).discover_modules(module_id="bulletin/GER_news").modules[0]
    assert current.collection_id is None


def test_collection_remove_rejects_stale_plan(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    plan = project.remove_collection("germany", family="bulletin")
    descriptor = tmp_path / "src/collections/bulletin/germany/category.txt"
    descriptor.write_text("add_namespace = changed\n", encoding="utf-8")

    stale = project.remove_collection(
        "germany",
        family="bulletin",
        write=True,
        plan_hash=plan["plan_hash"],
    )

    assert stale["blocked"] is True
    assert stale["removed"] is False
    assert stale["diagnostics"][0]["code"] == ("collection_remove.plan_hash_mismatch")
    assert descriptor.is_file()


def test_collection_remove_rejects_conflicting_visible_and_hidden_membership(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    hidden = tmp_path / "src/modules/bulletin/GER_news/.paradev/meta.yaml"
    hidden.parent.mkdir()
    hidden.write_text("collection: france\n", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting hidden collection metadata"):
        Project.load(tmp_path).remove_collection("germany", family="bulletin")

    assert (tmp_path / "src/collections/bulletin/germany").is_dir()
    assert "collection: germany" in (tmp_path / "src/modules/bulletin/GER_news/meta.yaml").read_text(encoding="utf-8")
    assert hidden.read_text(encoding="utf-8") == "collection: france\n"


def test_collection_remove_rolls_back_member_metadata_when_folder_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    metadata = tmp_path / "src/modules/bulletin/GER_news/meta.yaml"
    original = metadata.read_bytes()
    plan = project.remove_collection("germany", family="bulletin")

    def fail_collection_removal(
        _project: Project,
        _prepared: project_sdk._PreparedCollectionRemoval,
    ) -> None:
        raise ValueError("simulated collection move failure")

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_removal",
        fail_collection_removal,
    )
    with pytest.raises(ValueError, match="simulated collection move failure"):
        project.remove_collection(
            "germany",
            family="bulletin",
            write=True,
            plan_hash=plan["plan_hash"],
        )

    assert metadata.read_bytes() == original
    assert (tmp_path / "src/collections/bulletin/germany").is_dir()


def test_collection_remove_recovers_abrupt_precommit_folder_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    metadata = tmp_path / "src/modules/bulletin/GER_news/meta.yaml"
    original = metadata.read_bytes()
    collection_root = tmp_path / "src/collections/bulletin/germany"
    plan = project.remove_collection("germany", family="bulletin")
    commit = project_sdk._commit_prepared_collection_removal

    def crash_after_collection_move(
        target_project: Project,
        prepared: project_sdk._PreparedCollectionRemoval,
    ) -> None:
        commit(target_project, prepared)
        raise SystemExit("simulated abrupt collection removal exit")

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_removal",
        crash_after_collection_move,
    )
    with pytest.raises(SystemExit, match="abrupt collection removal exit"):
        project.remove_collection(
            "germany",
            family="bulletin",
            write=True,
            plan_hash=plan["plan_hash"],
        )

    assert not collection_root.exists()
    assert tuple(collection_root.parent.glob(".paradev-remove-*"))
    assert b"collection" not in metadata.read_bytes()

    monkeypatch.setattr(
        project_sdk,
        "_commit_prepared_collection_removal",
        commit,
    )
    result = Project.load(tmp_path).build()

    assert result.blocked is False
    assert collection_root.is_dir()
    assert not tuple(collection_root.parent.glob(".paradev-remove-*"))
    assert metadata.read_bytes() == original


def test_collection_remove_finishes_committed_cleanup_after_abrupt_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)
    metadata = tmp_path / "src/modules/bulletin/GER_news/meta.yaml"
    collection_root = tmp_path / "src/collections/bulletin/germany"
    plan = project.remove_collection("germany", family="bulletin")
    finalize = project_sdk._finalize_committed_source_draft_rename

    def crash_before_committed_cleanup(
        _project_root: Path,
        _rename: project_sdk.SourceDraftRecoveryRename | None,
    ) -> None:
        raise SystemExit("simulated committed cleanup exit")

    monkeypatch.setattr(
        project_sdk,
        "_finalize_committed_source_draft_rename",
        crash_before_committed_cleanup,
    )
    with pytest.raises(SystemExit, match="committed cleanup exit"):
        project.remove_collection(
            "germany",
            family="bulletin",
            write=True,
            plan_hash=plan["plan_hash"],
        )

    assert not collection_root.exists()
    assert tuple(collection_root.parent.glob(".paradev-remove-*"))
    assert b"collection" not in metadata.read_bytes()

    monkeypatch.setattr(
        project_sdk,
        "_finalize_committed_source_draft_rename",
        finalize,
    )
    result = Project.load(tmp_path).build()

    assert result.blocked is False
    assert not collection_root.exists()
    assert not tuple(collection_root.parent.glob(".paradev-remove-*"))
    assert b"collection" not in metadata.read_bytes()


def test_missing_project_manifest_error_is_contextual(tmp_path: Path) -> None:
    with pytest.raises(ProjectManifestError, match="paradev.yaml"):
        Project.load(tmp_path)


def test_project_templates_list_builtin_authoring_templates(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    payload = project.templates()
    templates = {row["id"]: row for row in payload["templates"]}

    assert payload["schema"] == "paradev.sdk.templates.v1"
    assert payload["project_id"] == "starter"
    assert payload["preferred_language"] == "en"
    assert payload["source_roots"] == [
        {
            "path": str(project.source_roots[0]),
            "relative_path": "src",
            "default": True,
        }
    ]
    assert payload["index"] == {
        "id": {"hoi4:idea/basic": [0]},
        "family": {"idea": [0]},
        "family_id": {"ideas": [0]},
        "kind": {"module": [0]},
        "source": {"builtin": [0]},
        "authoring_ready": {"true": [0]},
    }
    assert templates["hoi4:idea/basic"] == {
        "id": "hoi4:idea/basic",
        "title": "Basic HoI4 Idea",
        "family": "idea",
        "family_id": "ideas",
        "kind": "module",
        "source": "builtin",
        "directory": "{object_id}",
        "authoring_ready": True,
        "args": {
            "title": {
                "required": False,
                "default": "",
                "advanced": False,
                "type": "string",
                "description": ("Preferred-language display name used by `meta.yaml` and " "`main.loc`."),
                "description_source": "generated",
            },
            "description": {
                "required": False,
                "default": "",
                "advanced": False,
                "type": "text",
                "description": ("Preferred-language explanatory text written to `main.loc`."),
                "description_source": "generated",
            },
            "category": {
                "required": False,
                "default": "country",
                "advanced": True,
                "type": "string",
                "description": "Category used by `def.txt`.",
                "description_source": "generated",
            },
            "language": {
                "required": False,
                "default": "en",
                "advanced": True,
                "type": "string",
                "description": ("Localization language code, such as `zh` or `en`, used by " "`main.loc`."),
                "description_source": "generated",
            },
        },
        "form": {
            "fields": [
                {
                    "name": "title",
                    "target": "values",
                    "label": "Title",
                    "required": False,
                    "default": "",
                    "advanced": False,
                    "type": "string",
                    "description": ("Preferred-language display name used by `meta.yaml` and " "`main.loc`."),
                    "description_source": "generated",
                },
                {
                    "name": "description",
                    "target": "values",
                    "label": "Description",
                    "required": False,
                    "default": "",
                    "advanced": False,
                    "type": "text",
                    "description": ("Preferred-language explanatory text written to `main.loc`."),
                    "description_source": "generated",
                },
                {
                    "name": "category",
                    "target": "values",
                    "label": "Category",
                    "required": False,
                    "default": "country",
                    "advanced": True,
                    "type": "string",
                    "description": "Category used by `def.txt`.",
                    "description_source": "generated",
                },
                {
                    "name": "language",
                    "target": "values",
                    "label": "Language",
                    "required": False,
                    "default": "en",
                    "advanced": True,
                    "type": "string",
                    "description": ("Localization language code, such as `zh` or `en`, used by " "`main.loc`."),
                    "description_source": "generated",
                },
            ]
        },
        "files": ["meta.yaml", "def.txt", "main.loc"],
        "renderer": "files",
        "default_assets": {"icon": _idea_default_asset()},
    }


def test_project_templates_mark_unknown_family_not_authoring_ready(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: unknown_template_family",
                "title: Unknown Template Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  notice/custom:",
                "    title: Custom Notice",
                "    family: notice",
                "    files:",
                "      body.txt: notice = { id = {object_id} }",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    payload = project.templates()
    templates = {row["id"]: row for row in payload["templates"]}

    assert templates["notice/custom"]["authoring_ready"] is False
    assert templates["notice/custom"]["diagnostic_codes"] == ["template.unknown_family"]
    assert payload["index"] == {
        "id": {"hoi4:idea/basic": [0], "notice/custom": [1]},
        "family": {"idea": [0], "notice": [1]},
        "family_id": {"ideas": [0], "notice": [1]},
        "kind": {"module": [0, 1]},
        "source": {"builtin": [0], "project": [1]},
        "authoring_ready": {"false": [1], "true": [0]},
        "diagnostic_code": {"template.unknown_family": [1]},
    }


def test_project_templates_filters_rebuild_rows_and_index(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: filtered_template_project",
                "title: Filtered Template Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  notice/custom:",
                "    title: Custom Notice",
                "    family: notice",
                "    files:",
                "      body.txt: notice = { id = {object_id} }",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    payload = project.templates(
        family="notice",
        authoring_ready=False,
        diagnostic_code="template.unknown_family",
    )

    assert [row["id"] for row in payload["templates"]] == ["notice/custom"]
    assert payload["index"] == {
        "id": {"notice/custom": [0]},
        "family": {"notice": [0]},
        "family_id": {"notice": [0]},
        "kind": {"module": [0]},
        "source": {"project": [0]},
        "authoring_ready": {"false": [0]},
        "diagnostic_code": {"template.unknown_family": [0]},
    }


def test_project_templates_accept_browser_family_identity(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    payload = project.templates(family="ideas")

    assert [row["id"] for row in payload["templates"]] == ["hoi4:idea/basic"]
    assert payload["templates"][0]["family"] == "idea"
    assert payload["templates"][0]["family_id"] == "ideas"


def test_project_templates_filter_by_template_id(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    payload = project.templates(template_id="hoi4:idea/basic")

    assert [row["id"] for row in payload["templates"]] == ["hoi4:idea/basic"]
    assert payload["index"]["id"] == {"hoi4:idea/basic": [0]}


def test_project_templates_hide_shadowed_builtin_fallbacks_by_default(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: project_template_precedence",
                "title: Project Template Precedence",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  pihc3:idea/basic:",
                "    title: Project-owned Idea",
                "    family: idea",
                "    files:",
                "      def.txt: ideas = { country = { {object_id} = {} } }",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    default_ids = [row["id"] for row in project.templates()["templates"]]
    exact_builtin_ids = [
        row["id"]
        for row in project.templates(
            template_id="hoi4:idea/basic",
        )["templates"]
    ]
    builtin_ids = [row["id"] for row in project.templates(source="builtin")["templates"]]

    assert default_ids == ["pihc3:idea/basic"]
    assert exact_builtin_ids == ["hoi4:idea/basic"]
    assert builtin_ids == ["hoi4:idea/basic"]


def test_project_templates_mark_project_local_family_authoring_ready(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: ready_template_family",
                "title: Ready Template Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  notice:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: body.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/notices/{object_id}.txt",
                "templates:",
                "  notice/custom:",
                "    title: Custom Notice",
                "    family: notice",
                "    files:",
                "      body.txt: notice = { id = {object_id} }",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    payload = project.templates()
    templates = {row["id"]: row for row in payload["templates"]}

    assert templates["notice/custom"]["authoring_ready"] is True
    assert "diagnostic_codes" not in templates["notice/custom"]


def test_project_authoring_path_resolves_module_and_collection_roots(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")

    module = project.authoring_path("module", "idea", "GER_industry_spirit")
    collection = project.authoring_path("collection", "focus", "GER_main")

    assert module == {
        "schema": "paradev.sdk.authoring_path.v1",
        "project_id": "starter",
        "kind": "module",
        "family": "idea",
        "object_id": "GER_industry_spirit",
        "module_id": "idea/GER_industry_spirit",
        "source_root": str(project.source_roots[0]),
        "source_root_relative_path": "src",
        "root": str(project.source_roots[0] / "modules/idea/GER_industry_spirit"),
        "relative_path": "src/modules/idea/GER_industry_spirit",
        "template": "modules/{family}/{object_id}",
        "exists": False,
    }
    assert collection == {
        "schema": "paradev.sdk.authoring_path.v1",
        "project_id": "starter",
        "kind": "collection",
        "family": "focus",
        "collection_id": "GER_main",
        "source_root": str(project.source_roots[0]),
        "source_root_relative_path": "src",
        "root": str(project.source_roots[0] / "collections/focus/GER_main"),
        "relative_path": "src/collections/focus/GER_main",
        "template": "collections/{family}/{collection_id}",
        "exists": False,
    }


def test_project_authoring_plan_returns_project_local_family_slots(
    tmp_path: Path,
) -> None:
    _write_required_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.authoring_plan("module", "badge", "GER_new_badge")

    assert payload["schema"] == "paradev.sdk.authoring_plan.v1"
    assert payload["project_id"] == "required_slot_project"
    assert payload["profile"] == "hoi4"
    assert payload["authoring_path"]["module_id"] == "badge/GER_new_badge"
    assert payload["authoring_path"]["relative_path"] == "src/modules/badge/GER_new_badge"
    assert payload["source_slots"] == [
        {
            "owner_kind": "module",
            "module_id": "badge/GER_new_badge",
            "family": "badge",
            "root": str(tmp_path / "src/modules/badge/GER_new_badge"),
            "slot": "body",
            "match": "body.txt",
            "required": True,
            "many": False,
            "regex": False,
            "loader": "pdx",
            "status": "missing",
            "source_count": 0,
            "relative_paths": [],
            "paths": [],
            "diagnostic_codes": ["slot.missing_required"],
            "suggested_relative_paths": ["src/modules/badge/GER_new_badge/body.txt"],
            "suggested_paths": [str(tmp_path / "src/modules/badge/GER_new_badge/body.txt")],
        },
        {
            "owner_kind": "module",
            "module_id": "badge/GER_new_badge",
            "family": "badge",
            "root": str(tmp_path / "src/modules/badge/GER_new_badge"),
            "slot": "icon",
            "match": "icon.png",
            "required": True,
            "many": False,
            "regex": False,
            "loader": "copy",
            "status": "missing",
            "source_count": 0,
            "relative_paths": [],
            "paths": [],
            "diagnostic_codes": ["slot.missing_required"],
            "suggested_relative_paths": ["src/modules/badge/GER_new_badge/icon.png"],
            "suggested_paths": [str(tmp_path / "src/modules/badge/GER_new_badge/icon.png")],
        },
    ]


def test_project_scaffold_module_writes_builtin_idea_template(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    plan = project.scaffold_module(
        "hoi4:idea/basic",
        "GER_industry_spirit",
        values={
            "title": "German Industry Spirit",
            "description": "Industrial production spirit.",
        },
        write=True,
    )

    module_root = project.source_roots[0] / "modules/idea/GER_industry_spirit"
    assert plan["schema"] == "paradev.sdk.module_scaffold.v1"
    assert plan["blocked"] is False
    assert plan["written"] is True
    assert plan["module_id"] == "idea/GER_industry_spirit"
    assert plan["authoring_plan"]["schema"] == "paradev.sdk.authoring_plan.v1"
    assert plan["authoring_plan"]["authoring_path"]["module_id"] == "idea/GER_industry_spirit"
    assert [(row["slot"], row["status"], row["relative_paths"]) for row in plan["authoring_plan"]["source_slots"]] == [
        ("def", "satisfied", ["def.txt"]),
        ("icon", "empty", []),
        ("loc", "satisfied", ["main.loc"]),
    ]
    assert plan["authoring_plan"]["index"]["status"] == {
        "empty": [1],
        "satisfied": [0, 2],
    }
    assert [row["relative_path"] for row in plan["files"]] == [
        "src/modules/idea/GER_industry_spirit/meta.yaml",
        "src/modules/idea/GER_industry_spirit/def.txt",
        "src/modules/idea/GER_industry_spirit/main.loc",
    ]
    assert (module_root / "meta.yaml").read_text(encoding="utf-8") == "title: German Industry Spirit\n"
    assert (module_root / "def.txt").read_text(encoding="utf-8") == (
        "ideas = {\n" "\tcountry = {\n" "\t\tGER_industry_spirit = {\n" "\t\t\tpicture = GER_industry_spirit\n" "\t\t}\n" "\t}\n" "}\n"
    )
    assert (module_root / "main.loc").read_text(encoding="utf-8") == (
        "[en.GER_industry_spirit]\n" "German Industry Spirit\n\n" "[en.GER_industry_spirit_desc]\n" "Industrial production spirit.\n"
    )
    result = project.build()
    assert result.blocked is False


def test_project_create_modules_requires_plan_hash_and_applies_idempotently(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {
            "family": "idea",
            "object_id": object_id,
            "values": {
                "title": f"Idea {object_id}",
                "description": f"Generated description {index}.",
            },
        }
        for index, object_id in enumerate(("A", "B", "C", "D", "E"), start=1)
    ]

    plan = project.create_modules(requests)

    assert plan["schema"] == "paradev.sdk.module_batch.v1"
    assert plan["blocked"] is False
    assert plan["applied"] is False
    assert plan["written"] is False
    assert plan["counts"] == {"create": 5, "created": 0, "unchanged": 0, "blocked": 0}
    assert [row["status"] for row in plan["modules"]] == ["create"] * 5
    assert len(plan["plan_hash"]) == 64
    assert not (project.source_roots[0] / "modules/idea/A").exists()

    missing_hash = project.create_modules(requests, write=True)

    assert missing_hash["blocked"] is True
    assert missing_hash["applied"] is False
    assert {row["code"] for row in missing_hash["diagnostics"]} == {"module_batch.plan_hash_required"}
    assert not (project.source_roots[0] / "modules/idea/A").exists()

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is False
    assert applied["applied"] is True
    assert applied["written"] is True
    assert applied["plan_hash"] == plan["plan_hash"]
    assert applied["counts"] == {
        "create": 0,
        "created": 5,
        "unchanged": 0,
        "blocked": 0,
    }
    assert [row["status"] for row in applied["modules"]] == ["created"] * 5
    assert all(row["catalog_mutation"]["status"] == "not_configured" for row in applied["modules"])
    assert (project.source_roots[0] / "modules/idea/E/meta.yaml").read_text(encoding="utf-8") == "title: Idea E\n"

    stale_retry = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert stale_retry["blocked"] is True
    assert stale_retry["applied"] is False
    assert stale_retry["written"] is False
    assert stale_retry["plan_hash"] != plan["plan_hash"]
    assert {row["code"] for row in stale_retry["diagnostics"]} == {"module_batch.plan_hash_mismatch"}

    unchanged_plan = project.create_modules(requests)
    retried = project.create_modules(requests, write=True, plan_hash=unchanged_plan["plan_hash"])

    assert retried["blocked"] is False
    assert retried["applied"] is True
    assert retried["written"] is False
    assert retried["plan_hash"] == unchanged_plan["plan_hash"]
    assert retried["counts"] == {
        "create": 0,
        "created": 0,
        "unchanged": 5,
        "blocked": 0,
    }
    assert [row["status"] for row in retried["modules"]] == ["unchanged"] * 5


def test_project_create_modules_plan_hash_is_stable_and_order_sensitive(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "A", "values": {"title": "Alpha"}},
        {"family": "idea", "object_id": "B", "values": {"title": "Beta"}},
    ]

    first = project.create_modules(requests)
    repeated = project.create_modules(requests)
    reordered = project.create_modules(list(reversed(requests)))
    changed = project.create_modules(
        [
            requests[0],
            {"family": "idea", "object_id": "B", "values": {"title": "Changed Beta"}},
        ]
    )

    assert first["plan_hash"] == repeated["plan_hash"]
    assert reordered["plan_hash"] != first["plan_hash"]
    assert changed["plan_hash"] != first["plan_hash"]
    assert not (project.source_roots[0] / "modules/idea/A").exists()
    assert not (project.source_roots[0] / "modules/idea/B").exists()


def test_project_create_modules_holds_source_mutation_lock_through_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [{"family": "idea", "object_id": "LOCKED", "values": {"title": "Locked"}}]
    plan = project.create_modules(requests)
    original_writer = templates_sdk._write_scaffold_batch_anchored
    lock_active = False
    committed_under_lock = False

    @project_sdk.contextmanager
    def tracking_lock(locked_project: Project):
        nonlocal lock_active
        assert locked_project is project
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    def tracking_writer(**kwargs: object) -> None:
        nonlocal committed_under_lock
        committed_under_lock = lock_active
        original_writer(**kwargs)

    monkeypatch.setattr(project_sdk, "_project_source_mutation_lock", tracking_lock)
    monkeypatch.setattr(templates_sdk, "_write_scaffold_batch_anchored", tracking_writer)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is False
    assert committed_under_lock is True
    assert lock_active is False


def test_project_create_modules_blocks_entire_batch_on_divergent_existing_module(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "SAFE", "values": {"title": "Safe"}},
        {"family": "idea", "object_id": "CONFLICT", "values": {"title": "Conflict"}},
    ]
    plan = project.create_modules(requests)
    conflict_root = project.source_roots[0] / "modules/idea/CONFLICT"
    conflict_root.mkdir(parents=True)
    (conflict_root / "meta.yaml").write_text("type: idea\ntitle: Different\n", encoding="utf-8")

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is True
    assert applied["applied"] is False
    assert applied["written"] is False
    assert applied["plan_hash"] != plan["plan_hash"]
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.plan_hash_mismatch"}
    assert applied["counts"] == {
        "create": 1,
        "created": 0,
        "unchanged": 0,
        "blocked": 1,
    }
    assert {row["code"] for row in applied["modules"][1]["diagnostics"]} == {"module_batch.module_conflict"}
    assert not (project.source_roots[0] / "modules/idea/SAFE").exists()
    assert (conflict_root / "meta.yaml").read_text(encoding="utf-8") == "type: idea\ntitle: Different\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="Injects into _write_scaffold_file_anchored, which _uses_win32_scaffold_authority() bypasses on Windows.",
)
def test_project_create_modules_rolls_back_all_modules_when_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "FIRST", "values": {"title": "First"}},
        {"family": "idea", "object_id": "SECOND", "values": {"title": "Second"}},
    ]
    plan = project.create_modules(requests)
    original_write = templates_sdk._write_scaffold_file_anchored
    write_count = 0

    def failing_fourth_stage(module_fd: int, relative_path: str, content: str, *, force: bool) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 4:
            raise OSError("injected batch staging failure")
        original_write(module_fd, relative_path, content, force=force)

    monkeypatch.setattr(templates_sdk, "_write_scaffold_file_anchored", failing_fourth_stage)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is True
    assert applied["applied"] is False
    assert applied["written"] is False
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.concurrent_change"}
    assert not (project.source_roots[0] / "modules/idea/FIRST").exists()
    assert not (project.source_roots[0] / "modules/idea/SECOND").exists()
    transaction_root = project.source_roots[0] / ".paradev/module-transactions"
    assert not transaction_root.exists() or list(transaction_root.iterdir()) == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="Injects into os.link, which the retained Win32 publication path does not use.",
)
def test_project_create_modules_rolls_back_every_installed_module_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "FIRST", "values": {"title": "First"}},
        {"family": "idea", "object_id": "SECOND", "values": {"title": "Second"}},
    ]
    plan = project.create_modules(requests)
    original_link = os.link
    install_count = 0

    def failing_fourth_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal install_count
        if isinstance(src, str) and src.endswith(".stage"):
            install_count += 1
            if install_count == 4:
                raise OSError("injected batch install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", failing_fourth_install)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is True
    assert applied["applied"] is False
    assert applied["written"] is False
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.concurrent_change"}
    assert not (project.source_roots[0] / "modules/idea/FIRST").exists()
    assert not (project.source_roots[0] / "modules/idea/SECOND").exists()
    transaction_root = project.source_roots[0] / ".paradev/module-transactions"
    assert not transaction_root.exists() or list(transaction_root.iterdir()) == []


def test_project_create_modules_preserves_file_swapped_at_rollback_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "FIRST", "values": {"title": "First"}},
        {"family": "idea", "object_id": "SECOND", "values": {"title": "Second"}},
    ]
    plan = project.create_modules(requests)
    original_link = os.link
    original_rename = os.rename
    install_count = 0
    swapped = False

    def failing_fourth_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal install_count
        if isinstance(src, str) and src.endswith(".stage"):
            install_count += 1
            if install_count == 4:
                raise OSError("injected batch install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def swapping_target_at_quarantine(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and dst == templates_sdk._SCAFFOLD_QUARANTINE_ENTRY and isinstance(src, str) and src_dir_fd is not None:
            swapped = True
            original_rename(
                src,
                f"{src}.displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            descriptor = os.open(
                src,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=src_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent owner\n")
            finally:
                os.close(descriptor)
        original_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "link", failing_fourth_install)
    monkeypatch.setattr(os, "rename", swapping_target_at_quarantine)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert swapped is True
    assert applied["blocked"] is True
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    module_root = project.source_roots[0] / "modules/idea/FIRST"
    restored = [path for path in module_root.iterdir() if path.is_file() and path.read_text(encoding="utf-8") == "concurrent owner\n"]
    assert len(restored) == 1
    assert (module_root / f"{restored[0].name}.displaced").is_file()
    recovery_path = Path(applied["diagnostics"][0]["recovery_path"])
    quarantined = list(recovery_path.glob("*.rollback/entry"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "concurrent owner\n"
    assert quarantined[0].stat().st_ino == restored[0].stat().st_ino


def test_project_create_modules_preserves_in_place_content_change_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [
        {"family": "idea", "object_id": "FIRST", "values": {"title": "First"}},
        {"family": "idea", "object_id": "SECOND", "values": {"title": "Second"}},
    ]
    plan = project.create_modules(requests)
    original_link = os.link
    install_count = 0
    changed_target = ""

    def changing_third_then_failing_fourth_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal changed_target, install_count
        if isinstance(src, str) and src.endswith(".stage"):
            install_count += 1
            if install_count == 4:
                raise OSError("injected batch install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if install_count == 3 and isinstance(dst, str) and dst_dir_fd is not None:
            changed_target = dst
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_TRUNC,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent in-place edit\n")
            finally:
                os.close(descriptor)

    monkeypatch.setattr(os, "link", changing_third_then_failing_fourth_install)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert changed_target
    assert applied["blocked"] is True
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    changed_path = project.source_roots[0] / "modules/idea/FIRST" / changed_target
    assert changed_path.read_text(encoding="utf-8") == "concurrent in-place edit\n"
    recovery_path = Path(applied["diagnostics"][0]["recovery_path"])
    changed_stages = [path for path in recovery_path.glob("*.stage") if path.read_text(encoding="utf-8") == "concurrent in-place edit\n"]
    assert len(changed_stages) == 1
    assert changed_stages[0].stat().st_ino == changed_path.stat().st_ino


def test_project_create_modules_preserves_empty_module_swapped_at_rollback_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [{"family": "idea", "object_id": "SWAPPED", "values": {"title": "Swapped"}}]
    plan = project.create_modules(requests)
    original_link = os.link
    original_rename = os.rename
    swapped = False

    def failing_first_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if isinstance(src, str) and src.endswith(".stage"):
            raise OSError("injected batch install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def swapping_module_at_quarantine(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and src == "SWAPPED" and dst == templates_sdk._SCAFFOLD_QUARANTINE_ENTRY and src_dir_fd is not None:
            swapped = True
            original_rename(
                src,
                "SWAPPED-displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            os.mkdir(src, dir_fd=src_dir_fd)
        original_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "link", failing_first_install)
    monkeypatch.setattr(os, "rename", swapping_module_at_quarantine)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert swapped is True
    assert applied["blocked"] is True
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    family_root = project.source_roots[0] / "modules/idea"
    assert not (family_root / "SWAPPED").exists()
    assert (family_root / "SWAPPED-displaced").is_dir()
    recovery_path = Path(applied["diagnostics"][0]["recovery_path"])
    concurrent_module = recovery_path / "module-000000.rollback/entry"
    assert concurrent_module.is_dir()
    assert list(concurrent_module.iterdir()) == []


def test_project_create_modules_preserves_empty_nested_directory_swapped_at_rollback_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: nested_batch",
                "title: Nested Batch",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  nested:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: nested/body.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/nested/{object_id}.txt",
                "templates:",
                "  nested/basic:",
                "    title: Nested",
                "    family: nested",
                "    files:",
                "      meta.yaml: 'type: nested'",
                "      nested/body.txt: '{object_id} = {{}}'",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    requests = [{"template_id": "nested/basic", "object_id": "SWAPPED", "values": {}}]
    plan = project.create_modules(requests)
    original_link = os.link
    original_rename = os.rename
    swapped = False

    def failing_second_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if isinstance(src, str) and src.endswith("000001.stage"):
            raise OSError("injected batch install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def swapping_directory_at_quarantine(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and src == "nested" and dst == templates_sdk._SCAFFOLD_QUARANTINE_ENTRY and src_dir_fd is not None:
            swapped = True
            original_rename(
                src,
                "nested-displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            os.mkdir(src, dir_fd=src_dir_fd)
        original_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "link", failing_second_install)
    monkeypatch.setattr(os, "rename", swapping_directory_at_quarantine)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert swapped is True
    assert applied["blocked"] is True
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    module_root = project.source_roots[0] / "modules/nested/SWAPPED"
    assert not (module_root / "nested").exists()
    assert (module_root / "nested-displaced").is_dir()
    recovery_path = Path(applied["diagnostics"][0]["recovery_path"])
    concurrent_directories = list(recovery_path.glob("directory-*.rollback/entry"))
    assert len(concurrent_directories) == 1
    assert concurrent_directories[0].is_dir()
    assert list(concurrent_directories[0].iterdir()) == []


def test_project_create_modules_never_overwrites_or_removes_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [{"family": "idea", "object_id": "RACED", "values": {"title": "Raced"}}]
    plan = project.create_modules(requests)
    original_link = os.link
    injected = False

    def injecting_target_before_link(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal injected
        if not injected and isinstance(src, str) and src.endswith(".stage"):
            injected = True
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent owner\n")
            finally:
                os.close(descriptor)
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", injecting_target_before_link)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    module_root = project.source_roots[0] / "modules/idea/RACED"
    assert applied["blocked"] is True
    assert applied["applied"] is False
    assert applied["written"] is False
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    assert (module_root / "meta.yaml").read_text(encoding="utf-8") == "concurrent owner\n"
    recovery_path = Path(applied["diagnostics"][0]["recovery_path"])
    assert recovery_path.is_dir()
    assert sorted(path.name for path in recovery_path.glob("*.stage")) == [
        "000000.stage",
        "000001.stage",
        "000002.stage",
    ]


def test_project_create_modules_preserves_concurrently_replaced_module_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "starter")
    requests = [{"family": "idea", "object_id": "REPLACED", "values": {"title": "Replaced"}}]
    plan = project.create_modules(requests)
    original_link = os.link
    module_root = project.source_roots[0] / "modules/idea/REPLACED"
    displaced_root = project.source_roots[0] / "modules/idea/REPLACED-displaced"
    swapped = False

    def replacing_module_before_link(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if not swapped and isinstance(src, str) and src.endswith(".stage"):
            swapped = True
            module_root.rename(displaced_root)
            module_root.mkdir()
            (module_root / "concurrent.txt").write_text("concurrent owner\n", encoding="utf-8")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", replacing_module_before_link)

    applied = project.create_modules(requests, write=True, plan_hash=plan["plan_hash"])

    assert applied["blocked"] is True
    assert {row["code"] for row in applied["diagnostics"]} == {"module_batch.rollback_incomplete"}
    assert (module_root / "concurrent.txt").read_text(encoding="utf-8") == "concurrent owner\n"
    assert displaced_root.is_dir()
    assert list(displaced_root.iterdir()) == []
    assert Path(applied["diagnostics"][0]["recovery_path"]).is_dir()


def test_project_create_modules_rejects_duplicate_module_requests(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")

    plan = project.create_modules(
        [
            {"family": "idea", "object_id": "DUPLICATE", "values": {"title": "First"}},
            {
                "template_id": "hoi4:idea/basic",
                "object_id": "DUPLICATE",
                "values": {"title": "Second"},
            },
        ]
    )

    assert plan["blocked"] is True
    assert plan["counts"] == {"create": 1, "created": 0, "unchanged": 0, "blocked": 1}
    assert {row["code"] for row in plan["modules"][1]["diagnostics"]} == {"module_batch.duplicate_module"}
    assert not (project.source_roots[0] / "modules/idea/DUPLICATE").exists()

    filesystem_equivalent = project.create_modules(
        [
            {"family": "idea", "object_id": "CAFÉ", "values": {"title": "First"}},
            {
                "family": "idea",
                "object_id": "CAFE\u0301",
                "values": {"title": "Second"},
            },
        ]
    )

    assert filesystem_equivalent["blocked"] is True
    assert {row["code"] for row in filesystem_equivalent["modules"][1]["diagnostics"]} == {"module_batch.duplicate_module"}


def test_project_scaffold_module_blocks_template_missing_required_family_slot(
    tmp_path: Path,
) -> None:
    _write_required_slot_project(tmp_path)
    project = Project.load(tmp_path)

    dry = project.scaffold_module("badge/basic", "GER_new_badge")
    written = project.scaffold_module("badge/basic", "GER_new_badge", write=True)

    expected_diagnostic = {
        "code": "slot.missing_required",
        "message": "Required slot 'icon' did not match 'icon.png'.",
        "severity": "error",
        "module_id": "badge/GER_new_badge",
        "slot": "icon",
    }
    assert dry["blocked"] is True
    assert dry["written"] is False
    assert dry["diagnostics"] == [expected_diagnostic]
    assert written["blocked"] is True
    assert written["written"] is False
    assert written["diagnostics"] == [expected_diagnostic]
    assert not (tmp_path / "src/modules/badge/GER_new_badge").exists()


def test_project_scaffold_module_blocks_broken_external_destination_symlink(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project")
    module_link = project.root / "src/modules/idea/EVIL"
    outside = tmp_path / "outside"
    module_link.parent.mkdir(parents=True, exist_ok=True)
    module_link.symlink_to(outside, target_is_directory=True)

    dry = project.scaffold_module("idea", "EVIL", values={"title": "Outside"})
    written = project.scaffold_module("idea", "EVIL", values={"title": "Outside"}, write=True)

    for plan in (dry, written):
        assert plan["blocked"] is True
        assert plan["written"] is False
        assert plan["root"] == str(module_link)
        assert plan["authoring_plan"]["authoring_path"]["root"] == str(module_link)
        assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.path_symlink"}
        assert all(item["action"] == "blocked" for item in plan["files"])
    assert module_link.is_symlink()
    assert not outside.exists()


def test_project_scaffold_module_blocks_broken_sibling_destination_symlink(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project")
    family_root = project.root / "src/modules/idea"
    sibling = family_root / "SAFE"
    sibling.mkdir(parents=True)
    (sibling / "protected.txt").write_text("keep", encoding="utf-8")
    module_link = family_root / "EVIL"
    module_link.symlink_to(sibling / "missing", target_is_directory=True)

    plan = project.create_module("idea", "EVIL", values={"title": "Outside"})

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.path_symlink"}
    assert module_link.is_symlink()
    assert (sibling / "protected.txt").read_text(encoding="utf-8") == "keep"
    assert not (sibling / "missing").exists()


@pytest.mark.parametrize("component", ["modules", "family"])
def test_project_scaffold_module_blocks_symlinked_path_component(tmp_path: Path, component: str) -> None:
    project = Project.create(tmp_path / "project")
    modules_root = project.root / "src/modules"
    external_root = tmp_path / f"external-{component}"
    external_root.mkdir()
    if component == "modules":
        displaced = project.root / "src/modules-real"
        modules_root.rename(displaced)
        modules_root.symlink_to(external_root, target_is_directory=True)
        symlink = modules_root
    else:
        family_root = modules_root / "idea"
        family_root.symlink_to(external_root, target_is_directory=True)
        symlink = family_root

    plan = project.create_module("idea", "EVIL", values={"title": "Outside"})

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.path_symlink"}
    assert symlink.is_symlink()
    assert list(external_root.iterdir()) == []


def test_project_scaffold_module_rejects_generated_file_symlink_even_with_force(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "project")
    module_root = project.root / "src/modules/idea/SAFE"
    module_root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (module_root / "def.txt").symlink_to(outside)

    plan = project.scaffold_module("idea", "SAFE", values={"title": "Safe"}, write=True, force=True)

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.path_symlink"}
    assert outside.read_text(encoding="utf-8") == "keep"
    assert (module_root / "def.txt").is_symlink()


def test_project_scaffold_module_uses_open_directory_when_family_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    family_root = project.root / "src/modules/idea"
    displaced_family = project.root / "src/displaced-idea-family"
    external_family = tmp_path / "external-idea-family"
    external_family.mkdir()
    protected = external_family / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    original_write = templates_sdk._write_scaffold_file_anchored
    swapped = False

    def swapping_write(module_fd: int, relative_path: str, content: str, *, force: bool) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            family_root.rename(displaced_family)
            family_root.symlink_to(external_family, target_is_directory=True)
        original_write(module_fd, relative_path, content, force=force)

    monkeypatch.setattr(templates_sdk, "_write_scaffold_file_anchored", swapping_write)

    plan = project.create_module("idea", "SAFE", values={"title": "Safe"})

    assert plan["written"] is False
    assert plan["blocked"] is True
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.concurrent_change"}
    assert family_root.is_symlink()
    assert not (displaced_family / "SAFE").exists()
    assert protected.read_text(encoding="utf-8") == "keep"
    assert not (external_family / "SAFE").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Injects into _write_scaffold_file_anchored, which _uses_win32_scaffold_authority() bypasses on Windows.",
)
def test_project_scaffold_module_removes_new_partial_module_after_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    module_root = project.root / "src/modules/idea/PARTIAL"
    original_write = templates_sdk._write_scaffold_file_anchored
    write_count = 0

    def failing_second_write(module_fd: int, relative_path: str, content: str, *, force: bool) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected scaffold write failure")
        original_write(module_fd, relative_path, content, force=force)

    monkeypatch.setattr(templates_sdk, "_write_scaffold_file_anchored", failing_second_write)

    plan = project.create_module("idea", "PARTIAL", values={"title": "Partial"})

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.concurrent_change"}
    assert not module_root.exists()


def test_project_scaffold_module_stages_existing_force_update_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    initial = project.create_module("idea", "EXISTING", values={"title": "Initial"})
    targets = [Path(item["path"]) for item in initial["files"]]
    assert initial["written"] is True
    assert len(targets) >= 2
    original_contents = {target: f"preserved-{index}\n" for index, target in enumerate(targets)}
    for target, content in original_contents.items():
        target.write_text(content, encoding="utf-8")

    original_write = templates_sdk._write_scaffold_file_anchored
    write_count = 0

    def failing_second_stage(module_fd: int, relative_path: str, content: str, *, force: bool) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected scaffold staging failure")
        original_write(module_fd, relative_path, content, force=force)

    monkeypatch.setattr(templates_sdk, "_write_scaffold_file_anchored", failing_second_stage)

    plan = project.scaffold_module(
        "idea",
        "EXISTING",
        values={"title": "Replacement"},
        write=True,
        force=True,
    )

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.concurrent_change"}
    assert {target: target.read_text(encoding="utf-8") for target in targets} == original_contents
    transaction_root = project.root / "src/.paradev/module-transactions"
    assert not transaction_root.exists() or list(transaction_root.iterdir()) == []


def test_project_scaffold_module_holds_source_lock_outside_catalog_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    original_writer = templates_sdk._write_scaffold_files_anchored
    events: list[str] = []
    source_lock_active = False

    @project_sdk.contextmanager
    def tracking_source_lock(locked_project: Project):
        nonlocal source_lock_active
        assert locked_project is project
        source_lock_active = True
        events.append("source.enter")
        try:
            yield
        finally:
            events.append("source.exit")
            source_lock_active = False

    @project_sdk.contextmanager
    def tracking_catalog_lock(*args: object, enabled: bool):
        assert enabled is True
        assert source_lock_active is True
        events.append("catalog.enter")
        try:
            yield True
        finally:
            events.append("catalog.exit")

    def tracking_writer(**kwargs: object) -> None:
        assert source_lock_active is True
        events.append("write")
        original_writer(**kwargs)

    monkeypatch.setattr(project_sdk, "_project_source_mutation_lock", tracking_source_lock)
    monkeypatch.setattr(paradev_hb, "_module_catalog_mutation_scope", tracking_catalog_lock)
    monkeypatch.setattr(templates_sdk, "_write_scaffold_files_anchored", tracking_writer)

    plan = project.create_module("idea", "LOCKED", values={"title": "Locked"})

    assert plan["written"] is True
    assert events == [
        "source.enter",
        "catalog.enter",
        "write",
        "catalog.exit",
        "source.exit",
    ]


def test_project_scaffold_module_never_overwrites_target_appearing_before_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    original_link = os.link
    injected = False

    def injecting_target_before_link(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal injected
        if not injected and isinstance(src, str) and src.endswith(".stage"):
            injected = True
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent owner\n")
            finally:
                os.close(descriptor)
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", injecting_target_before_link)

    plan = project.create_module("idea", "RACED", values={"title": "Raced"})

    assert injected is True
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    module_root = project.source_roots[0] / "modules/idea/RACED"
    assert (module_root / "meta.yaml").read_text(encoding="utf-8") == "concurrent owner\n"
    assert Path(str(diagnostic["recovery_path"])).is_dir()


def test_project_scaffold_module_preserves_force_target_swapped_at_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    initial = project.create_module("idea", "EXISTING", values={"title": "Initial"})
    target = Path(initial["files"][0]["path"])
    target.write_text("observed original\n", encoding="utf-8")
    original_rename = os.rename
    swapped = False

    def swapping_force_target_at_quarantine(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and src == target.name and dst == templates_sdk._SCAFFOLD_QUARANTINE_ENTRY and src_dir_fd is not None:
            swapped = True
            original_rename(
                src,
                f"{src}.displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            descriptor = os.open(
                src,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=src_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent replacement\n")
            finally:
                os.close(descriptor)
        original_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", swapping_force_target_at_quarantine)

    plan = project.scaffold_module(
        "idea",
        "EXISTING",
        values={"title": "Replacement"},
        write=True,
        force=True,
    )

    assert swapped is True
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    assert target.read_text(encoding="utf-8") == "concurrent replacement\n"
    assert target.with_name(f"{target.name}.displaced").read_text(encoding="utf-8") == "observed original\n"
    recovery_path = Path(str(diagnostic["recovery_path"]))
    quarantined = list(recovery_path.glob("*.backup/entry"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "concurrent replacement\n"
    assert quarantined[0].stat().st_ino == target.stat().st_ino


def test_project_scaffold_module_preserves_target_appearing_after_force_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    initial = project.create_module("idea", "EXISTING", values={"title": "Initial"})
    target = Path(initial["files"][0]["path"])
    target.write_text("observed original\n", encoding="utf-8")
    original_link = os.link
    injected = False

    def injecting_target_after_backup(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal injected
        if not injected and isinstance(src, str) and src.endswith(".stage") and dst == target.name:
            injected = True
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent target\n")
            finally:
                os.close(descriptor)
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", injecting_target_after_backup)

    plan = project.scaffold_module(
        "idea",
        "EXISTING",
        values={"title": "Replacement"},
        write=True,
        force=True,
    )

    assert injected is True
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    assert target.read_text(encoding="utf-8") == "concurrent target\n"
    recovery_path = Path(str(diagnostic["recovery_path"]))
    assert (recovery_path / "0000.backup/entry").read_text(encoding="utf-8") == "observed original\n"


def test_project_scaffold_module_blocks_output_replaced_before_final_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    module_root = project.source_roots[0] / "modules/idea/REPLACED"
    target = module_root / "meta.yaml"
    original_require = templates_sdk._require_scaffold_target_identity
    require_count = 0

    def replacing_before_final_file_verification(*args: object, **kwargs: object) -> None:
        nonlocal require_count
        original_require(*args, **kwargs)
        require_count += 1
        if require_count == 2:
            target.rename(target.with_name("meta.yaml.displaced"))
            target.write_text("concurrent replacement\n", encoding="utf-8")

    monkeypatch.setattr(
        templates_sdk,
        "_require_scaffold_target_identity",
        replacing_before_final_file_verification,
    )

    plan = project.create_module("idea", "REPLACED", values={"title": "Replaced"})

    assert require_count == 2
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    assert target.read_text(encoding="utf-8") == "concurrent replacement\n"
    assert target.with_name("meta.yaml.displaced").is_file()
    assert Path(str(diagnostic["recovery_path"])).is_dir()


def test_project_scaffold_module_preserves_unknown_transaction_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    original_cleanup = templates_sdk._cleanup_scaffold_transaction
    injected = False

    def injecting_unknown_content(
        transaction_fd: int,
        stage_fingerprints: Mapping[str, tuple[tuple[int, int], int, str]],
    ) -> list[BaseException]:
        nonlocal injected
        injected = True
        descriptor = os.open(
            "concurrent.txt",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=transaction_fd,
        )
        try:
            os.write(descriptor, b"concurrent transaction owner\n")
        finally:
            os.close(descriptor)
        return original_cleanup(transaction_fd, stage_fingerprints)

    monkeypatch.setattr(
        templates_sdk,
        "_cleanup_scaffold_transaction",
        injecting_unknown_content,
    )

    plan = project.create_module("idea", "SAFE", values={"title": "Safe"})

    assert plan["written"] is True
    assert injected is True
    transaction_root = project.source_roots[0] / ".paradev/module-transactions"
    transactions = list(transaction_root.glob("*.txn"))
    assert len(transactions) == 1
    assert (transactions[0] / "concurrent.txt").read_text(encoding="utf-8") == ("concurrent transaction owner\n")


def test_project_scaffold_module_preserves_transaction_swapped_at_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    original_rename = os.rename
    swapped = False

    def swapping_transaction_at_cleanup(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and isinstance(src, str) and src.endswith(".txn") and isinstance(dst, str) and ".cleanup-" in dst and src_dir_fd is not None:
            swapped = True
            original_rename(
                src,
                f"{src}.displaced",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            os.mkdir(src, dir_fd=src_dir_fd)
        original_rename(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", swapping_transaction_at_cleanup)

    plan = project.create_module("idea", "SAFE", values={"title": "Safe"})

    assert plan["written"] is True
    assert swapped is True
    transaction_root = project.source_roots[0] / ".paradev/module-transactions"
    displaced = list(transaction_root.glob("*.txn.displaced"))
    concurrent = list(transaction_root.glob("*.cleanup-*.rollback"))
    assert len(displaced) == 1
    assert len(concurrent) == 1
    assert displaced[0].is_dir()
    assert concurrent[0].is_dir()


def test_project_scaffold_module_rolls_back_existing_force_update_when_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    initial = project.create_module("idea", "EXISTING", values={"title": "Initial"})
    targets = [Path(item["path"]) for item in initial["files"]]
    assert initial["written"] is True
    assert len(targets) >= 2
    original_contents = {target: f"preserved-{index}\n" for index, target in enumerate(targets)}
    for target, content in original_contents.items():
        target.write_text(content, encoding="utf-8")

    original_link = os.link
    stage_install_count = 0

    def failing_second_install(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal stage_install_count
        if isinstance(src, str) and src.endswith(".stage"):
            stage_install_count += 1
            if stage_install_count == 2:
                raise OSError("injected scaffold install failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", failing_second_install)

    plan = project.scaffold_module(
        "idea",
        "EXISTING",
        values={"title": "Replacement"},
        write=True,
        force=True,
    )

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.concurrent_change"}
    assert {target: target.read_text(encoding="utf-8") for target in targets} == original_contents
    transaction_root = project.root / "src/.paradev/module-transactions"
    assert not transaction_root.exists() or list(transaction_root.iterdir()) == []


def test_project_scaffold_module_preserves_backup_when_rollback_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    initial = project.create_module("idea", "EXISTING", values={"title": "Initial"})
    targets = [Path(item["path"]) for item in initial["files"]]
    assert initial["written"] is True
    assert len(targets) >= 2
    original_contents = {target: f"preserved-{index}\n" for index, target in enumerate(targets)}
    for target, content in original_contents.items():
        target.write_text(content, encoding="utf-8")

    original_link = os.link
    stage_install_count = 0
    restore_failed = False

    def failing_install_and_restore(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal restore_failed, stage_install_count
        if isinstance(src, str) and src.endswith(".stage"):
            stage_install_count += 1
            if stage_install_count == 2:
                raise OSError("injected scaffold install failure")
        if src == templates_sdk._SCAFFOLD_QUARANTINE_ENTRY and not restore_failed:
            restore_failed = True
            raise OSError("injected scaffold restore failure")
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", failing_install_and_restore)

    plan = project.scaffold_module(
        "idea",
        "EXISTING",
        values={"title": "Replacement"},
        write=True,
        force=True,
    )

    assert restore_failed is True
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    recovery_path = Path(str(diagnostic["recovery_path"]))
    assert recovery_path.is_dir()
    assert (recovery_path / "0001.backup/entry").read_text(encoding="utf-8") == original_contents[targets[1]]
    assert targets[0].read_text(encoding="utf-8") == original_contents[targets[0]]
    assert not targets[1].exists()


def test_project_scaffold_module_preserves_unanchored_module_when_descriptor_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    module_root = project.root / "src/modules/idea/OPENFAIL"
    original_open = os.open
    injected = False

    def failing_module_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if path == "OPENFAIL" and dir_fd is not None and not injected:
            injected = True
            raise OSError("injected module descriptor failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing_module_open)

    plan = project.create_module("idea", "OPENFAIL", values={"title": "Open failure"})

    assert injected is True
    assert plan["blocked"] is True
    assert plan["written"] is False
    diagnostic = next(item for item in plan["diagnostics"] if item["code"] == "scaffold.rollback_incomplete")
    assert Path(str(diagnostic["recovery_path"])).is_dir()
    assert module_root.is_dir()
    assert list(module_root.iterdir()) == []


def test_project_scaffold_module_fails_closed_without_anchored_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "project")
    module_root = project.root / "src/modules/idea/SAFE"
    monkeypatch.setattr(templates_sdk, "_ANCHORED_SCAFFOLD_SUPPORTED", False)

    plan = project.create_module("idea", "SAFE", values={"title": "Safe"})

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert {item["code"] for item in plan["diagnostics"]} == {"scaffold.unsupported_platform"}
    assert not module_root.exists()


def test_project_templates_expose_generic_form_fields_and_family_default_assets(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")

    payload = project.templates(template_id="hoi4:idea/basic")
    template = payload["templates"][0]
    fields = {field["name"]: field for field in template["form"]["fields"]}

    assert fields["title"]["type"] == "string"
    assert fields["title"]["target"] == "values"
    assert fields["title"]["description"] == ("Preferred-language display name used by `meta.yaml` and `main.loc`.")
    assert fields["title"]["description_source"] == "generated"
    assert fields["description"]["type"] == "text"
    assert fields["description"]["description"] == ("Preferred-language explanatory text written to `main.loc`.")
    assert fields["language"]["description"] == ("Localization language code, such as `zh` or `en`, used by `main.loc`.")
    assert template["args"]["category"]["description"] == ("Category used by `def.txt`.")
    assert template["args"]["category"]["description_source"] == "generated"
    assert fields["category"]["default"] == "country"
    assert fields["language"]["advanced"] is True
    assert template["renderer"] == "files"
    assert template["default_assets"] == {
        "icon": {
            "slot": "icon",
            "asset_id": "hoi4:idea/default_icon",
            "title": "Default HoI4 idea icon",
            "source": "builtin",
            "path": "hoi4:idea/default_icon",
            "editable": True,
            "injected": False,
        }
    }


def test_project_authoring_plan_exposes_default_assets_without_writing_instance_file(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")

    preflight = project.authoring_plan("module", "idea", "GER_industry_spirit")
    icon_row = next(row for row in preflight["source_slots"] if row["slot"] == "icon")

    assert icon_row["status"] == "empty"
    assert icon_row["default_asset"]["asset_id"] == "hoi4:idea/default_icon"

    plan = project.scaffold_module(
        "hoi4:idea/basic",
        "GER_industry_spirit",
        values={"title": "German Industry Spirit"},
        write=True,
    )

    module_root = project.source_roots[0] / "modules/idea/GER_industry_spirit"
    icon_row = next(row for row in plan["authoring_plan"]["source_slots"] if row["slot"] == "icon")
    assert icon_row["default_asset"]["injected"] is False
    assert not (module_root / "icon.png").exists()
    assert not (module_root / "icon.dds").exists()


def test_project_scaffold_module_accepts_builtin_family_shorthand(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "starter")

    plan = project.scaffold_module(
        "idea",
        "GER_industry_spirit",
        values={
            "title": "German Industry Spirit",
            "description": "Industrial production spirit.",
        },
        write=True,
    )

    assert plan["blocked"] is False
    assert plan["template_id"] == "hoi4:idea/basic"
    assert (project.source_roots[0] / "modules/idea/GER_industry_spirit/main.loc").is_file()


def test_project_create_module_is_intuitive_sdk_alias(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    plan = project.create_module(
        "idea",
        "GER_industry_spirit",
        values={
            "title": "German Industry Spirit",
            "description": "Industrial production spirit.",
        },
    )

    assert plan["blocked"] is False
    assert plan["written"] is True
    assert plan["template_id"] == "hoi4:idea/basic"
    assert plan["module_id"] == "idea/GER_industry_spirit"
    assert (project.source_roots[0] / "modules/idea/GER_industry_spirit/main.loc").is_file()


def test_project_create_module_draft_resolves_browser_family_id(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    payload = project.create_module_draft(
        "ideas",
        "GER_industry_spirit",
        values={
            "title": "German Industry Spirit",
            "description": "Industrial production spirit.",
        },
    )

    plan = payload["plan"]

    assert payload["schema"] == "paradev.rest.module_draft.v1"
    assert payload["project_id"] == "starter"
    assert payload["family_id"] == "ideas"
    assert payload["draft_id"] == "ideas:GER_industry_spirit"
    assert plan["template_id"] == "hoi4:idea/basic"
    assert plan["written"] is False
    assert plan["blocked"] is False
    assert plan["module_id"] == "idea/GER_industry_spirit"


def test_project_scaffold_module_can_select_source_root(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: multi_root_project",
                "title: Multi Root Project",
                "game: hoi4",
                "source_roots: [src, imports]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    plan = project.scaffold_module(
        "hoi4:idea/basic",
        "GER_imported_spirit",
        source_root="imports",
        values={"title": "German Imported Spirit"},
        write=True,
    )

    assert plan["blocked"] is False
    assert plan["source_root"] == str(tmp_path / "imports")
    assert plan["root"] == str(tmp_path / "imports/modules/idea/GER_imported_spirit")
    assert (tmp_path / "imports/modules/idea/GER_imported_spirit/def.txt").is_file()
    assert not (tmp_path / "src/modules/idea/GER_imported_spirit/def.txt").exists()


def test_project_scaffold_module_rejects_unknown_source_root(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "starter")

    with pytest.raises(ValueError, match="Unknown source root"):
        project.scaffold_module("hoi4:idea/basic", "GER_missing", source_root="imports")


def test_project_scaffold_module_rejects_template_with_unknown_family(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: unknown_template_family",
                "title: Unknown Template Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  notice/custom:",
                "    title: Custom Notice",
                "    family: notice",
                "    files:",
                "      body.txt: notice = { id = {object_id} }",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    with pytest.raises(
        ValueError,
        match="Template 'notice/custom' targets unknown build family 'notice'",
    ):
        project.scaffold_module("notice/custom", "ALERT")


def test_project_scaffold_module_supports_project_local_template_args(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: local_template_project",
                "title: Local Template Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  modifier/custom:",
                "    title: Custom Modifier",
                "    family: modifier",
                "    args:",
                "      title:",
                "        required: true",
                "      bonus:",
                "        default: '0.05'",
                "    files:",
                "      meta.yaml: |",
                "        type: modifier",
                "        title: {title}",
                "      def.txt: |",
                "        {object_id} = {{",
                "          stability_factor = {bonus}",
                "        }}",
                "      main.loc: |",
                "        [en.{object_id}]",
                "        {title}",
                "",
                "        [en.{object_id}_desc]",
                "        Custom modifier.",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    plan = project.scaffold_module(
        "modifier/custom",
        "GER_recovery_boost",
        values={"title": "German Recovery Boost"},
        write=True,
    )

    module_root = tmp_path / "src/modules/modifier/GER_recovery_boost"
    assert plan["blocked"] is False
    assert plan["written"] is True
    assert (module_root / "def.txt").read_text(encoding="utf-8") == "GER_recovery_boost = {\n  stability_factor = 0.05\n}\n"
    assert (module_root / "main.loc").read_text(encoding="utf-8") == (
        "[en.GER_recovery_boost]\n" "German Recovery Boost\n\n" "[en.GER_recovery_boost_desc]\n" "Custom modifier.\n"
    )


def test_project_scaffold_module_preserves_written_plan_when_projection_is_undiscoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: undiscoverable_template_project",
                "title: Undiscoverable Template Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  modifier/source-only:",
                "    title: Source-only Modifier",
                "    family: modifier",
                "    files:",
                "      def.txt: '{object_id} = {{}}'",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    def fail_projection(*args, **kwargs):
        raise ValueError("Unknown module after scaffold")

    monkeypatch.setattr(project_sdk, "_find_project_module", fail_projection)

    plan = project.scaffold_module("modifier/source-only", "GER_notes", write=True)

    assert plan["written"] is True
    assert plan["blocked"] is False
    assert plan["catalog_mutation"]["status"] == "not_configured"
    assert (tmp_path / "src/modules/modifier/GER_notes/def.txt").read_text(encoding="utf-8") == "GER_notes = {}\n"


def test_project_templates_support_project_local_form_arg_metadata(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: local_template_form_project",
                "title: Local Template Form Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  modifier/custom:",
                "    title: Custom Modifier",
                "    family: modifier",
                "    args:",
                "      title:",
                "        required: true",
                "        label: Display title",
                "        description: Human-facing modifier title.",
                "      description:",
                "        type: text",
                "        advanced: true",
                "      bonus:",
                "        type: number",
                "        default: '0.05'",
                "        advanced: false",
                "      scope:",
                "        type: choice",
                "        default: country",
                "        choices: [country, state]",
                "    files:",
                "      meta.yaml: |",
                "        type: modifier",
                "        title: {title}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    template = project.templates(template_id="modifier/custom")["templates"][0]
    fields = {field["name"]: field for field in template["form"]["fields"]}

    assert fields["title"]["label"] == "Display title"
    assert fields["title"]["description"] == "Human-facing modifier title."
    assert fields["title"]["description_source"] == "declared"
    assert fields["description"]["type"] == "text"
    assert fields["description"]["advanced"] is True
    assert fields["description"]["description_source"] == "generated"
    assert fields["bonus"]["type"] == "number"
    assert fields["bonus"]["advanced"] is False
    assert fields["scope"]["type"] == "choice"
    assert fields["scope"]["choices"] == ["country", "state"]


def test_module_template_values_enforce_shared_form_types(tmp_path: Path) -> None:
    template = ModuleTemplate(
        template_id="notice/typed",
        family="notice",
        title="Typed Notice",
        args=(
            TemplateArg("title", required=True),
            TemplateArg("amount", type="number"),
            TemplateArg(
                "scope",
                type="choice",
                choices=("country", "state"),
            ),
            TemplateArg("enabled", type="boolean"),
        ),
        files=(
            TemplateFile(
                "def.txt",
                "{object_id} = {{ amount = {amount} scope = {scope} enabled = {enabled} }}",
            ),
        ),
    )

    invalid = module_scaffold_plan(
        project_id="typed",
        project_root=tmp_path,
        source_root=tmp_path / "src",
        template=template,
        object_id="ALERT",
        values={
            "title": "",
            "amount": "NaN",
            "scope": "province",
            "enabled": "maybe",
        },
    )

    assert invalid["blocked"] is True
    assert {(diagnostic["code"], diagnostic.get("field")) for diagnostic in invalid["diagnostics"]} == {
        ("scaffold.invalid_boolean", "enabled"),
        ("scaffold.invalid_choice", "scope"),
        ("scaffold.invalid_number", "amount"),
        ("scaffold.missing_value", "title"),
    }

    valid = module_scaffold_plan(
        project_id="typed",
        project_root=tmp_path,
        source_root=tmp_path / "src",
        template=template,
        object_id="ALERT",
        values={
            "title": "Alert",
            "amount": "0.25",
            "scope": "country",
            "enabled": "yes",
        },
    )

    assert valid["blocked"] is False
    assert valid["diagnostics"] == []

    overflow = module_scaffold_plan(
        project_id="typed",
        project_root=tmp_path,
        source_root=tmp_path / "src",
        template=template,
        object_id="OVERFLOW",
        values={
            "title": "Overflow",
            "amount": "1e10000",
            "scope": "country",
            "enabled": "yes",
        },
    )

    assert overflow["blocked"] is True
    assert [diagnostic["code"] for diagnostic in overflow["diagnostics"]] == ["scaffold.invalid_number"]


def test_module_template_accepts_python_renderer_functions(tmp_path: Path) -> None:
    def render(values: Mapping[str, str]) -> tuple[TemplateFile, ...]:
        return (
            TemplateFile("meta.yaml", f"type: {values['family']}\ntitle: {values['title']}\n"),
            TemplateFile("def.txt", f"{values['object_id']} = {{ value = {values['value']} }}\n"),
        )

    template = ModuleTemplate(
        template_id="notice/python",
        family="notice",
        title="Python Notice",
        args=(TemplateArg("title", required=True), TemplateArg("value", default="1")),
        files=(),
        renderer=render,
    )

    plan = module_scaffold_plan(
        project_id="python_renderer",
        project_root=tmp_path,
        source_root=tmp_path / "src",
        template=template,
        object_id="ALERT",
        values={"title": "Alert"},
        write=True,
    )

    root = tmp_path / "src/modules/notice/ALERT"
    assert plan["blocked"] is False
    assert plan["renderer"] == "python"
    assert (root / "meta.yaml").read_text(encoding="utf-8") == "type: notice\ntitle: Alert\n"
    assert (root / "def.txt").read_text(encoding="utf-8") == "ALERT = { value = 1 }\n"


def test_module_template_reports_python_renderer_failures_as_diagnostics(
    tmp_path: Path,
) -> None:
    def render(_values: Mapping[str, str]) -> tuple[TemplateFile, ...]:
        raise RuntimeError("renderer context unavailable")

    template = ModuleTemplate(
        template_id="notice/python",
        family="notice",
        title="Python Notice",
        args=(),
        files=(),
        renderer=render,
    )

    plan = module_scaffold_plan(
        project_id="python_renderer",
        project_root=tmp_path,
        source_root=tmp_path / "src",
        template=template,
        object_id="ALERT",
        write=True,
    )

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert plan["files"] == []
    assert plan["diagnostics"] == [
        {
            "code": "scaffold.renderer_failed",
            "severity": "error",
            "message": "Template notice/python renderer failed: renderer context unavailable",
            "template_id": "notice/python",
        }
    ]


def test_project_scaffold_module_blocks_missing_required_args(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: missing_template_args",
                "title: Missing Template Args",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  modifier/custom:",
                "    family: modifier",
                "    args:",
                "      title:",
                "        required: true",
                "    files:",
                "      meta.yaml: |",
                "        type: modifier",
                "        title: {title}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    plan = project.scaffold_module("modifier/custom", "GER_missing_title", write=True)

    assert plan["blocked"] is True
    assert plan["written"] is False
    assert plan["diagnostics"] == [
        {
            "code": "scaffold.missing_value",
            "severity": "error",
            "message": "Template modifier/custom requires value 'title'.",
            "template_id": "modifier/custom",
            "field": "title",
        }
    ]
    assert not (tmp_path / "src/modules/modifier/GER_missing_title").exists()


def test_project_manifest_supports_family_default_asset_overrides(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: default_asset_project",
                "title: Default Asset Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: icon",
                "        match: icon.png",
                "        kind: copy",
                "    default_assets:",
                "      icon:",
                "        title: Badge fallback icon",
                "        path: assets/defaults/badge.png",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "templates:",
                "  badge/basic:",
                "    family: badge",
                "    files:",
                "      meta.yaml: |",
                "        type: badge",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    family = project.families(family="badge")["families"][0]
    template = project.templates(template_id="badge/basic")["templates"][0]
    authoring = project.authoring_plan("module", "badge", "GER_badge")

    assert family["assets"]["defaults"]["icon"] == {
        "slot": "icon",
        "asset_id": "badge:icon/default",
        "title": "Badge fallback icon",
        "source": "project",
        "path": "assets/defaults/badge.png",
        "editable": True,
        "injected": False,
    }
    assert template["default_assets"]["icon"]["path"] == "assets/defaults/badge.png"
    assert authoring["source_slots"][0]["default_asset"]["path"] == "assets/defaults/badge.png"


def test_comment_metadata_is_allowed_for_modules_and_collections_in_strict_mode(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/focus/GER_comment_focus"
    collection_root = tmp_path / "src/collections/focus/GER_comment_tree"
    module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text(
        "type: focus\ncollection: GER_comment_tree\ncomment: Requirements and rewards are still draft.\n",
        encoding="utf-8",
    )
    (collection_root / "meta.yaml").write_text("comment: Focus tree design notes.\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: comment_metadata_project",
                "title: Comment Metadata Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    modules = project.discover_modules(profile="hoi4", strict_metadata=True)
    collections = project.discover_collections(profile="hoi4", strict_metadata=True)

    assert [diagnostic.code for diagnostic in modules.diagnostics] == []
    assert [diagnostic.code for diagnostic in collections.diagnostics] == []
    assert modules.modules[0].metadata["comment"] == "Requirements and rewards are still draft."
    assert collections.collections[0].metadata["comment"] == "Focus tree design notes."


def test_project_manifest_rejects_unsafe_scaffold_template_file_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: unsafe_template_path",
                "title: Unsafe Template Path",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "templates:",
                "  modifier/unsafe:",
                "    family: modifier",
                "    files:",
                "      ../outside.txt: escape",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="templates\\.modifier/unsafe\\.files\\.\\.\\./outside\\.txt",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_unsupported_family_kind(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_family",
                "title: Bad Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: python_source",
                "    templates:",
                "      pdx: events/superevents/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.superevent\\.kind"):
        Project.load(tmp_path)


@pytest.mark.parametrize("value", ["hidden", 0, 1, [], {}])
def test_project_manifest_rejects_non_boolean_family_visibility(tmp_path: Path, value: object) -> None:
    (tmp_path / "paradev.yaml").write_text(
        json.dumps(
            {
                "project_id": "bad_family_visibility",
                "title": "Bad Family Visibility",
                "game": "hoi4",
                "source_roots": ["src"],
                "output_root": "build/mod",
                "build_root": ".paradev/.cache/build",
                "families": {"support": {"visible": value}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.support\\.visible must be a boolean"):
        Project.load(tmp_path)


def test_project_manifest_rejects_compiler_owned_publication_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        json.dumps(
            {
                "project_id": "bad_retired_families",
                "title": "Bad Retired Families",
                "game": "hoi4",
                "source_roots": ["src"],
                "output_root": "build/mod",
                "build_root": ".paradev/.cache/build",
                "families": {
                    "achievement": {
                        "retired_families": "achievement_component",
                        "templates": {"pdx": "common/achievements/{object_id}.txt"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="retired_families is no longer a compiler setting",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_list_compiler_owned_publication_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        json.dumps(
            {
                "project_id": "self_retired_family",
                "title": "Self Retired Family",
                "game": "hoi4",
                "source_roots": ["src"],
                "output_root": "build/mod",
                "build_root": ".paradev/.cache/build",
                "families": {
                    "achievement": {
                        "retired_families": ["achievement"],
                        "templates": {"pdx": "common/achievements/{object_id}.txt"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ProjectManifestError,
        match="retired_families is no longer a compiler setting",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_module_pdx_template_for_simple_family(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_family_template",
                "title: Bad Family Template",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    templates:",
                "      module_pdx: events/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.superevent\\.templates\\.module_pdx"):
        Project.load(tmp_path)


def test_project_manifest_rejects_collection_slots_for_simple_family(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_family_slots",
                "title: Bad Family Slots",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    collection_source_slots:",
                "      - name: header",
                "        match: header.txt",
                "    templates:",
                "      pdx: events/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.superevent\\.collection_source_slots"):
        Project.load(tmp_path)


def test_project_manifest_rejects_invalid_source_slot_regex(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_source_slot_regex",
                "title: Bad Source Slot Regex",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: '['",
                "        regex: true",
                "    templates:",
                "      pdx: events/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="families\\.superevent\\.source_slots\\[0\\]\\.match must be a valid regex",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_escaping_source_slot_match(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_source_slot_path",
                "title: Bad Source Slot Path",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: ../outside.pdx",
                "    templates:",
                "      pdx: events/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="families\\.superevent\\.source_slots\\[0\\]\\.match must be relative and stay under the source root",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_routed_family_without_routes(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_routed_family",
                "title: Bad Routed Family",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  trait:",
                "    kind: routed_source",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.trait\\.routes"):
        Project.load(tmp_path)


def test_project_manifest_exposes_explicit_non_emitting_route_in_family_schema(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: non_emitting_route",
                "title: Non-emitting Route",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  model_asset:",
                "    kind: routed_source",
                "    route_setting: settings.asset_kind",
                "    routes:",
                "      aggregate:",
                "        emits_artifacts: false",
                "      record:",
                "        pdx: gfx/models/{object_id}.asset",
            ]
        ),
        encoding="utf-8",
    )

    payload = Project.load(tmp_path).families()
    family = next(row for row in payload["families"] if row["family"] == "model_asset")

    assert payload["schema"] == "paradev.build.families.v1"
    assert family["routes"] == {
        "aggregate": {"emits_artifacts": False},
        "record": {"pdx": "gfx/models/{object_id}.asset"},
    }
    assert family["metadata"]["settings"]["asset_kind"] == {
        "required": True,
        "values": ["aggregate", "record"],
    }
    assert [output["route"] for output in family["outputs"]] == ["record"]


@pytest.mark.parametrize(
    ("route_lines", "message"),
    [
        (
            [
                "        emits_artifacts: false",
                "        pdx: gfx/models/{object_id}.asset",
            ],
            "cannot define artifact templates when emits_artifacts is false",
        ),
        (["        emits_artifacts: disabled"], "emits_artifacts must be a boolean"),
    ],
)
def test_project_manifest_rejects_invalid_non_emitting_route_contracts(
    tmp_path: Path,
    route_lines: list[str],
    message: str,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: invalid_non_emitting_route",
                "title: Invalid Non-emitting Route",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  model_asset:",
                "    kind: routed_source",
                "    routes:",
                "      aggregate:",
                *route_lines,
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match=message):
        Project.load(tmp_path)


def test_project_manifest_rejects_top_level_route_templates_for_routed_family(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_routed_templates",
                "title: Bad Routed Templates",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  trait:",
                "    kind: routed_source",
                "    routes:",
                "      country_leader:",
                "        pdx: common/country_leader/{object_id}.txt",
                "    templates:",
                "      pdx: common/traits/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.trait\\.templates\\.pdx"):
        Project.load(tmp_path)


def test_project_manifest_rejects_invalid_asset_constraints(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_asset_constraints",
                "title: Bad Asset Constraints",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "    asset_constraints:",
                "      icon:",
                "        width: 0",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="families\\.badge\\.asset_constraints\\.icon\\.width",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_unknown_family_template_fields(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_template_field",
                "title: Bad Template Field",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    templates:",
                "      copy: gfx/badges/{object_id}/{unknown_field}.png",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="families\\.badge\\.templates\\.copy.*unknown template field 'unknown_field'",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_incomplete_sprite_templates(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_sprite_templates",
                "title: Bad Sprite Templates",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    sprite_slots: [icon]",
                "    templates:",
                "      copy: gfx/badges/{object_id}{source_suffix}",
                "      sprite_gfx: interface/paradev_{family}.gfx",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="families\\.badge\\.templates\\.sprite_name"):
        Project.load(tmp_path)


def test_project_manifest_rejects_unknown_setting_normalizer(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_setting_normalizer",
                "title: Bad Setting Normalizer",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  character:",
                "    kind: routed_source",
                "    settings_normalizers:",
                "      subtype: python",
                "    routes:",
                "      advisor:",
                "        pdx: common/advisors/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectManifestError,
        match="families\\.character\\.settings_normalizers\\.subtype",
    ):
        Project.load(tmp_path)


def test_project_manifest_rejects_missing_python_module(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: missing_python_module",
                "title: Missing Python Module",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "python_modules:",
                "  - tools/missing.py",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectManifestError, match="python_modules\\[0\\]"):
        Project.load(tmp_path)


def test_project_families_rejects_python_module_without_register(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: missing_python_register",
                "title: Missing Python Register",
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

    with pytest.raises(ProjectManifestError, match="register\\(registry\\)"):
        project.families()


def test_project_families_rejects_python_module_with_empty_routed_family(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import RoutedSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(RoutedSourceFamily(family='character', routes={}))",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_routed_family",
                "title: Bad Python Routed Family",
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

    with pytest.raises(
        ProjectManifestError,
        match="Routed build family 'character' must define at least one route",
    ):
        project.families()


def test_project_families_rejects_python_module_with_empty_routed_family_route(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import RoutedSourceFamily, SourceRoute",
                "",
                "",
                "def register(registry):",
                "    registry.add(RoutedSourceFamily(family='character', routes={'advisor': SourceRoute()}))",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_routed_route",
                "title: Bad Python Routed Route",
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

    with pytest.raises(
        ProjectManifestError,
        match="Routed build family 'character' route 'advisor' must define at least one artifact template",
    ):
        project.families()


def test_project_families_rejects_python_module_with_empty_routed_family_route_id(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import RoutedSourceFamily, SourceRoute",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        RoutedSourceFamily(",
                "            family='character',",
                "            routes={'': SourceRoute(pdx_path_template='common/advisors/{object_id}.txt')},",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_routed_route_id",
                "title: Bad Python Routed Route Id",
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

    with pytest.raises(
        ProjectManifestError,
        match="Routed build family 'character' route ids must be non-empty strings",
    ):
        project.families()


def test_project_families_rejects_python_module_with_empty_routed_settings_key(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import RoutedSourceFamily, SourceRoute",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        RoutedSourceFamily(",
                "            family='character',",
                "            settings_key='',",
                "            routes={'advisor': SourceRoute(pdx_path_template='common/advisors/{object_id}.txt')},",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_routed_settings_key",
                "title: Bad Python Routed Settings Key",
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

    with pytest.raises(
        ProjectManifestError,
        match="Routed build family 'character' settings_key must name a key under settings",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_setting_values(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            settings_values={'picture': ('news', 7)},",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_setting_values",
                "title: Bad Python Setting Values",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' settings_values.picture must be a string or list of strings",
    ):
        project.families()


def test_project_families_rejects_python_module_with_unknown_template_field(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{unknown_field}.txt',",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_template_field",
                "title: Bad Python Template Field",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' templates.pdx uses unknown template field 'unknown_field'",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_template_value(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template=7,",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_template_value",
                "title: Bad Python Template Value",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' templates.pdx must be a non-empty string",
    ):
        project.families()


def test_project_families_rejects_python_module_with_incomplete_sprite_templates(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            sprite_gfx_path_template='interface/paradev_{family}.gfx',",
                "            sprite_name_template='GFX_notice_{object_id}',",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_sprite_templates",
                "title: Bad Python Sprite Templates",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' templates.copy must be defined when sprite templates are declared",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_metadata_keys(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            metadata_keys=('scope', 7),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_metadata_keys",
                "title: Bad Python Metadata Keys",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' metadata_keys must be a string or list of strings",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_asset_constraints(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            copy_path_template='gfx/notices/{object_id}{source_suffix}',",
                "            asset_constraints={'icon': {'width': 0}},",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_asset_constraints",
                "title: Bad Python Asset Constraints",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' asset_constraints.icon.width must be a positive integer",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_source_slot(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
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
                "            source_slots=(Slot('', 'def.pdx'),),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_source_slots",
                "title: Bad Python Source Slots",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' source_slots\\[0\\]\\.name must be a non-empty string",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_source_slot_regex(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
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
                "            source_slots=(Slot('body', '[', regex=True),),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_source_slot_regex",
                "title: Bad Python Source Slot Regex",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' source_slots\\[0\\]\\.match must be a valid regex",
    ):
        project.families()


def test_project_families_rejects_python_module_with_escaping_source_slot_match(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
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
                "            source_slots=(Slot('body', '../outside.pdx'),),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_source_slot_path",
                "title: Bad Python Source Slot Path",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' source_slots\\[0\\]\\.match must be relative and stay under the source root",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_required_settings(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            required_settings=('picture', 7),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_required_settings",
                "title: Bad Python Required Settings",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' required_settings must be a string or list of strings",
    ):
        project.families()


def test_project_families_rejects_python_module_with_invalid_setting_normalizer(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            settings_normalizers={'picture': 'lower_snake'},",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_setting_normalizer",
                "title: Bad Python Setting Normalizer",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' settings_normalizers.picture must be callable",
    ):
        project.families()


def test_project_families_rejects_python_module_with_unknown_required_loc_key_field(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "tools"
    plugin_root.mkdir()
    (plugin_root / "families.py").write_text(
        "\n".join(
            [
                "from paradev.build import SimpleSourceFamily",
                "",
                "",
                "def register(registry):",
                "    registry.add(",
                "        SimpleSourceFamily(",
                "            family='notice',",
                "            pdx_path_template='common/notices/{object_id}.txt',",
                "            required_loc_keys=('{unknown_field}',),",
                "        )",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: bad_python_required_loc_keys",
                "title: Bad Python Required Loc Keys",
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

    with pytest.raises(
        ProjectManifestError,
        match="Build family 'notice' required_loc_keys uses unknown template field 'unknown_field'",
    ):
        project.families()


def test_project_cli_outputs_manifest_view_json() -> None:
    result = CliRunner().invoke(build_app(), ["project", str(PROJECT_ROOT), "--json"])

    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert view["project_id"] == "minimal_hoi4"
    assert view["root"] == str(PROJECT_ROOT)


def test_project_families_returns_profile_family_contracts() -> None:
    payload = Project.load(PROJECT_ROOT).families()
    families = {row["family"]: row for row in payload["families"]}
    presentations = {family: row.pop("presentation") for family, row in families.items()}
    authoring = {family: row.pop("authoring") for family, row in families.items() if "authoring" in row}

    assert presentations == {
        "decision": {
            "id": "decisions",
            "title": "Decisions",
            "group": "country",
            "title_key": "modules.decisions.title",
        },
        "event": {
            "id": "events",
            "title": "Events",
            "group": "events",
            "title_key": "modules.events.title",
        },
        "focus": {
            "id": "focuses",
            "title": "Focuses",
            "group": "country",
            "title_key": "modules.focuses.title",
        },
        "idea": {
            "id": "ideas",
            "title": "Ideas",
            "group": "country",
            "title_key": "modules.ideas.title",
        },
        "mod_descriptor": {
            "id": "mod-descriptor",
            "title": "Mod Descriptor",
            "group": "other",
        },
        "modifier": {
            "id": "modifiers",
            "title": "Modifiers",
            "group": "shared",
            "title_key": "modules.modifiers.title",
        },
        "opinion_modifier": {
            "id": "opinion-modifiers",
            "title": "Opinion Modifiers",
            "group": "country",
            "title_key": "modules.opinionModifiers.title",
        },
        "trait": {
            "id": "traits",
            "title": "Traits",
            "group": "country",
            "title_key": "modules.traits.title",
        },
    }
    assert authoring == {
        family: _identity_copy_contract()
        for family in (
            "decision",
            "event",
            "focus",
            "idea",
            "modifier",
            "opinion_modifier",
            "trait",
        )
    }

    assert payload["schema"] == "paradev.build.families.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["authoring"] == {
        "source_roots": [
            {
                "path": str(PROJECT_ROOT / "src"),
                "relative_path": "src",
                "default": True,
            }
        ],
        "module_path_template": "modules/{family}/{object_id}",
        "collection_path_template": "collections/{family}/{collection_id}",
    }
    assert sorted(families) == [
        "decision",
        "event",
        "focus",
        "idea",
        "mod_descriptor",
        "modifier",
        "opinion_modifier",
        "trait",
    ]
    assert payload["index"]["family"] == {
        "decision": [0],
        "event": [1],
        "focus": [2],
        "idea": [3],
        "mod_descriptor": [4],
        "modifier": [5],
        "opinion_modifier": [6],
        "trait": [7],
    }
    assert payload["index"]["kind"] == {
        "collection_pdx": [2],
        "collection_source": [0, 1, 5],
        "project_metadata": [4],
        "routed_source": [7],
        "simple_source": [3, 6],
    }
    assert payload["index"]["source_slot"]["def"] == [0, 1, 2, 3, 5, 6, 7]
    assert payload["index"]["source_slot"]["assets"] == [0, 1, 2, 5, 6, 7]
    assert payload["index"]["sprite_slot"] == {"icon": [3]}
    assert payload["index"]["route"] == {
        "country_leader": [7],
        "scientist": [7],
        "unit_leader": [7],
    }
    assert payload["index"]["output_artifact_type"] == {
        "copy": [0, 1, 2, 3, 5, 6, 7],
        "loc": [0, 1, 2, 3, 5, 6, 7],
        "pdx": [0, 1, 2, 3, 5, 6, 7],
        "sprite_gfx": [3],
        "view": [2],
    }
    source_slots = _default_source_slots_view()
    assert families["decision"] == {
        "family": "decision",
        "kind": "collection_source",
        "metadata": _metadata_contract(),
        "stages": _collection_family_stages(),
        "source_slots": source_slots,
        "outputs": [
            _output_contract("pdx", "pdx", "common/decisions/{collection_id}.txt", ["collection"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["collection", "module"],
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/paradev/{object_id}/{source_path}",
                ["collection", "module"],
            ),
        ],
        "templates": {
            "pdx": "common/decisions/{collection_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            "copy": "gfx/paradev/{object_id}/{source_path}",
        },
    }
    assert families["event"] == {
        "family": "event",
        "kind": "collection_source",
        "metadata": _metadata_contract(),
        "stages": _collection_family_stages(),
        "source_slots": source_slots,
        "outputs": [
            _output_contract("pdx", "pdx", "events/{collection_id}.txt", ["collection"]),
            _output_contract("pdx", "module_pdx", "events/{object_id}.txt", ["module"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["collection", "module"],
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/paradev/{object_id}/{source_path}",
                ["collection", "module"],
            ),
        ],
        "templates": {
            "pdx": "events/{collection_id}.txt",
            "module_pdx": "events/{object_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            "copy": "gfx/paradev/{object_id}/{source_path}",
        },
    }
    assert families["focus"]["kind"] == "collection_pdx"
    assert families["focus"]["stages"] == _collection_family_stages()
    assert families["idea"] == {
        "family": "idea",
        "kind": "simple_source",
        "localization": _localization_contract(),
        "assets": {"defaults": {"icon": _idea_default_asset()}},
        "metadata": _metadata_contract(),
        "stages": _source_family_stages(),
        "source_slots": _idea_source_slots_view(),
        "sprite_slots": ["icon"],
        "outputs": [
            _output_contract("pdx", "pdx", "common/ideas/{object_id}.txt", ["module"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/interface/ideas/idea_{object_id}{source_suffix}",
                ["module"],
            ),
            _output_contract(
                "sprite_gfx",
                "sprite_gfx",
                "interface/paradev_idea.gfx",
                ["project"],
                source_slots=["icon"],
            ),
        ],
        "templates": {
            "pdx": "common/ideas/{object_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            "copy": "gfx/interface/ideas/idea_{object_id}{source_suffix}",
            "sprite_gfx": "interface/paradev_idea.gfx",
            "sprite_name": "GFX_idea_{object_id}",
        },
    }
    assert families["modifier"] == {
        "family": "modifier",
        "kind": "collection_source",
        "metadata": _metadata_contract(),
        "stages": _collection_family_stages(),
        "source_slots": _modifier_source_slots_view(),
        "outputs": [
            _output_contract("pdx", "pdx", "common/modifiers/{collection_id}.txt", ["collection"]),
            _output_contract("pdx", "module_pdx", "common/modifiers/{object_id}.txt", ["module"]),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["collection", "module"],
            ),
            _output_contract("copy", "copy", "{source_path}", ["collection", "module"]),
        ],
        "templates": {
            "pdx": "common/modifiers/{collection_id}.txt",
            "module_pdx": "common/modifiers/{object_id}.txt",
            "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
            "copy": "{source_path}",
        },
    }
    assert families["mod_descriptor"] == {
        "family": "mod_descriptor",
        "kind": "project_metadata",
        "visible": False,
        "metadata": _metadata_contract(),
        "stages": ["emit"],
    }
    assert families["trait"] == {
        "family": "trait",
        "kind": "routed_source",
        "metadata": _metadata_contract(
            settings={
                "subtype": {
                    "required": True,
                    "values": ["country_leader", "scientist", "unit_leader"],
                }
            }
        ),
        "route_setting": "settings.subtype",
        "stages": _source_family_stages(),
        "source_slots": source_slots,
        "outputs": [
            _output_contract(
                "pdx",
                "pdx",
                "common/country_leader/{object_id}.txt",
                ["module"],
                route="country_leader",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
                route="country_leader",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/paradev/{object_id}/{source_path}",
                ["module"],
                route="country_leader",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "pdx",
                "pdx",
                "common/scientist_traits/{object_id}.txt",
                ["module"],
                route="scientist",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
                route="scientist",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/paradev/{object_id}/{source_path}",
                ["module"],
                route="scientist",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "pdx",
                "pdx",
                "common/unit_leader/{object_id}.txt",
                ["module"],
                route="unit_leader",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "loc",
                "loc",
                "localisation/{language_folder}/{object_id}_{language}.yml",
                ["module"],
                route="unit_leader",
                route_setting="settings.subtype",
            ),
            _output_contract(
                "copy",
                "copy",
                "gfx/paradev/{object_id}/{source_path}",
                ["module"],
                route="unit_leader",
                route_setting="settings.subtype",
            ),
        ],
        "routes": {
            "country_leader": {
                "pdx": "common/country_leader/{object_id}.txt",
                "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
                "copy": "gfx/paradev/{object_id}/{source_path}",
            },
            "scientist": {
                "pdx": "common/scientist_traits/{object_id}.txt",
                "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
                "copy": "gfx/paradev/{object_id}/{source_path}",
            },
            "unit_leader": {
                "pdx": "common/unit_leader/{object_id}.txt",
                "loc": "localisation/{language_folder}/{object_id}_{language}.yml",
                "copy": "gfx/paradev/{object_id}/{source_path}",
            },
        },
    }
    assert payload["writers"] == [
        {"artifact_type": "copy", "kind": "StaticCopyWriter"},
        {"artifact_type": "loc", "kind": "LocalizationYMLWriter"},
        {"artifact_type": "mod_descriptor", "kind": "ModDescriptorWriter"},
        {"artifact_type": "pdx", "kind": "PDXTextWriter"},
        {"artifact_type": "sprite_gfx", "kind": "SpriteGFXWriter"},
        {"artifact_type": "view", "kind": "JsonViewWriter"},
    ]
    assert payload["index"]["artifact_type"] == {
        "copy": [0],
        "loc": [1],
        "mod_descriptor": [2],
        "pdx": [3],
        "sprite_gfx": [4],
        "view": [5],
    }


def test_project_families_filters_profile_family_contracts() -> None:
    project = Project.load(PROJECT_ROOT)

    idea = project.families(family="idea", source_slot="icon")
    assert [row["family"] for row in idea["families"]] == ["idea"]
    assert idea["index"]["family"] == {"idea": [0]}
    assert idea["index"]["source_slot"] == {"def": [0], "icon": [0], "loc": [0]}
    assert idea["index"]["sprite_slot"] == {"icon": [0]}

    collections = project.families(kind="collection_source", source_slot="assets")
    assert [row["family"] for row in collections["families"]] == [
        "decision",
        "event",
        "modifier",
    ]
    assert collections["index"]["kind"] == {"collection_source": [0, 1, 2]}
    assert collections["index"]["source_slot"]["assets"] == [0, 1, 2]

    routed = project.families(route="scientist")
    assert [row["family"] for row in routed["families"]] == ["trait"]
    assert routed["index"]["route"]["scientist"] == [0]

    writer = project.families(artifact_type="sprite_gfx")
    assert [row["family"] for row in writer["families"]] == ["idea"]
    assert (
        _output_contract(
            "sprite_gfx",
            "sprite_gfx",
            "interface/paradev_idea.gfx",
            ["project"],
            source_slots=["icon"],
        )
        in writer["families"][0]["outputs"]
    )
    assert writer["writers"] == [{"artifact_type": "sprite_gfx", "kind": "SpriteGFXWriter"}]
    assert writer["index"]["output_artifact_type"]["sprite_gfx"] == [0]
    assert writer["index"]["artifact_type"] == {"sprite_gfx": [0]}


def test_project_families_prefers_family_source_slot_contracts() -> None:
    registry = BuildRegistry(source_slots=(Slot("def", "def.txt"),)).add(
        SimpleSourceFamily(
            family="event",
            pdx_path_template="events/{object_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            metadata_keys=("scope",),
            settings_keys=("picture",),
            settings_values={"picture": ("feature", "news")},
            required_settings=("picture",),
            required_loc_keys=("{object_id}", "{object_id}_desc"),
        )
    )

    payload = Project.load(PROJECT_ROOT).families(registry=registry)
    assert payload["families"][0].pop("presentation") == {
        "id": "event",
        "title": "Event",
        "group": "other",
    }
    assert payload["families"][0].pop("authoring") == _identity_copy_contract()

    assert payload["families"] == [
        {
            "family": "event",
            "kind": "simple_source",
            "localization": _localization_contract(),
            "metadata": _metadata_contract(
                "scope",
                settings={"picture": {"required": True, "values": ["feature", "news"]}},
            ),
            "stages": _source_family_stages(),
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
            "outputs": [_output_contract("pdx", "pdx", "events/{object_id}.txt", ["module"])],
            "templates": {
                "pdx": "events/{object_id}.txt",
            },
        }
    ]


def test_project_families_exposes_shared_source_slot_contracts(tmp_path: Path) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: shared_source_slots",
                "title: Shared Source Slots",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  superevent:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: body",
                "        match: shared.pdx",
                "        kind: pdx",
                "        shared: true",
                "    templates:",
                "      pdx: events/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    payload = Project.load(tmp_path).families()
    families = {row["family"]: row for row in payload["families"]}

    assert families["superevent"]["source_slots"] == [
        {
            "name": "body",
            "match": "shared.pdx",
            "required": False,
            "many": False,
            "regex": False,
            "kind": "pdx",
            "shared": True,
        }
    ]


def test_project_families_exposes_sprite_slot_contracts() -> None:
    registry = BuildRegistry(source_slots=(Slot("def", "def.txt"),)).add(
        SimpleSourceFamily(
            family="idea",
            copy_path_template="gfx/interface/ideas/idea_{object_id}{source_suffix}",
            sprite_gfx_path_template="interface/paradev_{family}.gfx",
            sprite_name_template="GFX_idea_{object_id}",
            sprite_slots=("icon",),
            source_slots=(Slot("icon", r"^icon\.(png|dds)$", regex=True, kind="copy"),),
        )
    )

    payload = Project.load(PROJECT_ROOT).families(registry=registry)
    assert payload["families"][0].pop("presentation") == {
        "id": "idea",
        "title": "Idea",
        "group": "other",
    }
    assert payload["families"][0].pop("authoring") == _identity_copy_contract()

    assert payload["families"] == [
        {
            "family": "idea",
            "kind": "simple_source",
            "metadata": _metadata_contract(),
            "stages": _source_family_stages(),
            "source_slots": [
                {
                    "name": "icon",
                    "match": r"^icon\.(png|dds)$",
                    "required": False,
                    "many": False,
                    "regex": True,
                    "kind": "copy",
                }
            ],
            "sprite_slots": ["icon"],
            "outputs": [
                _output_contract(
                    "copy",
                    "copy",
                    "gfx/interface/ideas/idea_{object_id}{source_suffix}",
                    ["module"],
                ),
                _output_contract(
                    "sprite_gfx",
                    "sprite_gfx",
                    "interface/paradev_{family}.gfx",
                    ["project"],
                    source_slots=["icon"],
                ),
            ],
            "templates": {
                "copy": "gfx/interface/ideas/idea_{object_id}{source_suffix}",
                "sprite_gfx": "interface/paradev_{family}.gfx",
                "sprite_name": "GFX_idea_{object_id}",
            },
        }
    ]


def test_project_families_exposes_collection_source_slot_contracts() -> None:
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(
                Slot("category", "category.txt", kind="pdx"),
                Slot("strings", "strings.yml", kind="loc"),
            ),
        )
    )

    payload = Project.load(PROJECT_ROOT).families(registry=registry)
    assert payload["families"][0].pop("presentation") == {
        "id": "event",
        "title": "Event",
        "group": "other",
    }
    assert payload["families"][0].pop("authoring") == _identity_copy_contract()

    assert payload["families"] == [
        {
            "family": "event",
            "kind": "collection_source",
            "metadata": _metadata_contract(),
            "stages": _collection_family_stages(),
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
                    "name": "category",
                    "match": "category.txt",
                    "required": False,
                    "many": False,
                    "regex": False,
                    "kind": "pdx",
                },
                {
                    "name": "strings",
                    "match": "strings.yml",
                    "required": False,
                    "many": False,
                    "regex": False,
                    "kind": "loc",
                },
            ],
            "outputs": [_output_contract("pdx", "pdx", "events/{collection_id}.txt", ["collection"])],
            "templates": {"pdx": "events/{collection_id}.txt"},
        }
    ]


def test_project_families_returns_asset_contracts() -> None:
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="idea",
            copy_path_template="gfx/interface/ideas/idea_{object_id}{source_suffix}",
            source_slots=(Slot("icon", r"^icon\.(png|dds)$", regex=True, kind="copy"),),
            asset_constraints={"icon": {"formats": ("png", "dds"), "width": 64, "height": 64}},
        )
    )

    payload = Project.load(PROJECT_ROOT).families(registry=registry)
    assert payload["families"][0].pop("presentation") == {
        "id": "idea",
        "title": "Idea",
        "group": "other",
    }
    assert payload["families"][0].pop("authoring") == _identity_copy_contract()

    assert payload["families"] == [
        {
            "family": "idea",
            "kind": "simple_source",
            "assets": _asset_contract(),
            "metadata": _metadata_contract(),
            "stages": _source_family_stages(),
            "source_slots": [
                {
                    "name": "icon",
                    "match": r"^icon\.(png|dds)$",
                    "required": False,
                    "many": False,
                    "regex": True,
                    "kind": "copy",
                }
            ],
            "outputs": [
                _output_contract(
                    "copy",
                    "copy",
                    "gfx/interface/ideas/idea_{object_id}{source_suffix}",
                    ["module"],
                )
            ],
            "templates": {"copy": "gfx/interface/ideas/idea_{object_id}{source_suffix}"},
        }
    ]


def test_project_cli_outputs_profile_family_contracts() -> None:
    result = CliRunner().invoke(build_app(), ["families", str(PROJECT_ROOT), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    trait = next(row for row in payload["families"] if row["family"] == "trait")
    assert payload["schema"] == "paradev.build.families.v1"
    assert payload["profile"] == "hoi4"
    assert payload["authoring"]["module_path_template"] == "modules/{family}/{object_id}"
    assert payload["authoring"]["collection_path_template"] == "collections/{family}/{collection_id}"
    assert payload["authoring"]["source_roots"] == [
        {
            "path": str(PROJECT_ROOT / "src"),
            "relative_path": "src",
            "default": True,
        }
    ]
    assert trait["route_setting"] == "settings.subtype"
    assert trait["source_slots"] == _default_source_slots_view()
    assert sorted(trait["routes"]) == ["country_leader", "scientist", "unit_leader"]
    assert payload["index"]["family"]["trait"] == [7]
    assert payload["index"]["route"]["scientist"] == [7]
    assert payload["index"]["artifact_type"]["sprite_gfx"] == [4]


def test_project_cli_filters_profile_family_contracts() -> None:
    result = CliRunner().invoke(
        build_app(),
        [
            "families",
            str(PROJECT_ROOT),
            "--family",
            "idea",
            "--source-slot",
            "icon",
            "--artifact-type",
            "sprite_gfx",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["family"] for row in payload["families"]] == ["idea"]
    assert payload["writers"] == [{"artifact_type": "sprite_gfx", "kind": "SpriteGFXWriter"}]
    assert payload["index"]["family"] == {"idea": [0]}
    assert payload["index"]["artifact_type"] == {"sprite_gfx": [0]}


def test_demo_project_build_cli_outputs_profile_artifacts() -> None:
    result = CliRunner().invoke(build_app(), ["build", str(PROJECT_ROOT), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["profile"] == "hoi4"
    assert payload["modules"][0]["source_slots"] == {
        "def": ["def.txt"],
        "loc": ["main.loc"],
    }
    assert payload["collections"][0]["collection_id"] == "GER_main"
    assert payload["collections"][0]["module_ids"] == ["focus/GER_sample"]
    assert [artifact["path"] for artifact in payload["artifacts"]] == [
        "common/national_focus/GER_main.txt",
        "views/focus-tree/GER_main.json",
        "localisation/english/GER_sample_l_english.yml",
        "descriptor.mod",
        "launcher/minimal_hoi4.mod",
    ]
    assert payload["dependencies"] == [
        {
            "source": "module:focus/GER_sample",
            "target": "idea:GER_industrial_spirit",
            "kind": "requires",
        },
        {
            "source": "module:focus/GER_sample",
            "target": "focus:GER_rhineland",
            "kind": "after",
        },
    ]
    assert payload["summary"]["artifact_count"] == 5
    assert payload["summary"]["dependency_count"] == 2


def test_project_build_cli_can_emit_only_the_compact_summary(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")

    result = CliRunner().invoke(
        build_app(),
        [
            "build",
            str(project_root),
            "--emit-artifacts",
            "--emit-manifests",
            "--summary",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "schema": "paradev.build.summary.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "summary": {
            "module_count": 1,
            "collection_count": 1,
            "dependency_count": 2,
            "artifact_count": 5,
            "diagnostic_count": 0,
            "error_count": 0,
            "blocked": False,
        },
    }
    assert (project_root / "build/mod/common/national_focus/GER_main.txt").is_file()
    assert (project_root / ".paradev/.cache/build/summary.json").is_file()


def test_project_build_strict_metadata_blocks_unknown_module_keys(tmp_path: Path, cm_paradev_lock) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    metadata_path = project_root / "src/modules/focus/GER_sample/meta.yaml"
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8") + "\nlegacy_hint: yes\n",
        encoding="utf-8",
    )
    project = Project.load(project_root)
    previous = CM_PARADEV.get("paradev.build.strict_metadata", default=None)

    try:
        CM_PARADEV.unset("paradev.build.strict_metadata")

        loose = project.build()
        strict = project.build(strict_metadata=True)
        result = CliRunner().invoke(build_app(), ["build", str(project_root), "--strict-metadata", "--json"])
    finally:
        if previous is None or isinstance(previous, Mapping):
            CM_PARADEV.unset("paradev.build.strict_metadata")
        else:
            CM_PARADEV.set("paradev.build.strict_metadata", previous)

    assert loose.blocked is False
    assert loose.diagnostics[0].code == "metadata.unknown_key"
    assert loose.diagnostics[0].severity == "warning"
    assert strict.blocked is True
    assert strict.diagnostics[0].code == "metadata.unknown_key"
    assert strict.diagnostics[0].severity == "error"
    assert strict.diagnostics[0].module_id == "focus/GER_sample"

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["blocked"] is True
    assert payload["diagnostics"][0]["code"] == "metadata.unknown_key"
    assert payload["diagnostics"][0]["severity"] == "error"


def test_project_build_strict_metadata_uses_configured_default(tmp_path: Path, cm_paradev_lock) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    metadata_path = project_root / "src/modules/focus/GER_sample/meta.yaml"
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8") + "\nlegacy_hint: yes\n",
        encoding="utf-8",
    )
    project = Project.load(project_root)
    previous = CM_PARADEV.get("paradev.build.strict_metadata", default=None)

    try:
        CM_PARADEV.set("paradev.build.strict_metadata", True)

        configured = project.build()
        explicit_loose = project.build(strict_metadata=False)
        diagnostics = project.diagnostics()
        strict_cli = CliRunner().invoke(build_app(), ["build", str(project_root), "--json"])
        loose_cli = CliRunner().invoke(build_app(), ["build", str(project_root), "--no-strict-metadata", "--json"])
    finally:
        if previous is None or isinstance(previous, Mapping):
            CM_PARADEV.unset("paradev.build.strict_metadata")
        else:
            CM_PARADEV.set("paradev.build.strict_metadata", previous)

    assert configured.blocked is True
    assert configured.diagnostics[0].severity == "error"
    assert explicit_loose.blocked is False
    assert explicit_loose.diagnostics[0].severity == "warning"
    assert diagnostics["diagnostics"][0]["severity"] == "error"
    assert strict_cli.exit_code == 0, strict_cli.output
    assert json.loads(strict_cli.output)["summary"]["blocked"] is True
    assert loose_cli.exit_code == 0, loose_cli.output
    assert json.loads(loose_cli.output)["summary"]["blocked"] is False


def test_project_summary_returns_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.summary()

    assert payload == {
        "schema": "paradev.build.summary.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "summary": {
            "module_count": 1,
            "collection_count": 1,
            "dependency_count": 2,
            "artifact_count": 5,
            "diagnostic_count": 0,
            "error_count": 0,
            "blocked": False,
        },
    }
    assert not project.build_root.exists()


def test_project_cli_outputs_summary_manifest_json_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")

    result = CliRunner().invoke(build_app(), ["summary", str(project_root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.summary.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["summary"] == {
        "module_count": 1,
        "collection_count": 1,
        "dependency_count": 2,
        "artifact_count": 5,
        "diagnostic_count": 0,
        "error_count": 0,
        "blocked": False,
    }
    assert not (project_root / ".paradev/.cache/build/summary.json").exists()


def test_project_manifests_returns_all_manifest_payloads_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.manifests()

    assert payload["schema"] == "paradev.build.manifests.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert sorted(payload["manifests"]) == [
        "artifacts.json",
        "assets.json",
        "collections.json",
        "dependencies.json",
        "diagnostics.json",
        "localization.json",
        "modules.json",
        "source-map.json",
        "sources.json",
        "sprites.json",
        "summary.json",
    ]
    assert payload["manifests"]["summary.json"]["summary"]["artifact_count"] == 5
    assert payload["manifests"]["modules.json"]["modules"][0]["module_id"] == "focus/GER_sample"
    assert payload["manifests"]["sources.json"]["sources"][0]["module_id"] == "focus/GER_sample"
    assert all(manifest["project_id"] == "minimal_hoi4" for manifest in payload["manifests"].values())
    assert all(manifest["profile"] == "hoi4" for manifest in payload["manifests"].values())
    assert not project.build_root.exists()


def test_project_cli_outputs_all_manifest_payloads_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")

    result = CliRunner().invoke(build_app(), ["manifests", str(project_root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.manifests.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["manifests"]["summary.json"]["summary"]["artifact_count"] == 5
    assert payload["manifests"]["artifacts.json"]["artifacts"][0]["path"] == "common/national_focus/GER_main.txt"
    assert not (project_root / ".paradev/.cache/build/modules.json").exists()


def test_project_dependencies_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.dependencies(
        module_id="focus/GER_sample",
        kind="requires",
        target="idea:GER_industrial_spirit",
    )
    prefixed_module_payload = project.dependencies(
        module_id="module:focus/GER_sample",
        kind="requires",
        target="idea:GER_industrial_spirit",
    )
    bare_source_payload = project.dependencies(source="focus/GER_sample", kind="requires", target="idea:GER_industrial_spirit")

    assert payload == {
        "schema": "paradev.build.dependencies.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "dependencies": [
            {
                "source": "module:focus/GER_sample",
                "target": "idea:GER_industrial_spirit",
                "kind": "requires",
            }
        ],
        "index": {"module:focus/GER_sample": {"requires": [0]}},
    }
    assert prefixed_module_payload == payload
    assert bare_source_payload == payload
    assert not project.build_root.exists()


def test_project_cli_filters_dependency_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "dependencies",
            str(project_root),
            "--module",
            "focus/GER_sample",
            "--kind",
            "after",
            "--target",
            "focus:GER_rhineland",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "schema": "paradev.build.dependencies.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "dependencies": [
            {
                "source": "module:focus/GER_sample",
                "target": "focus:GER_rhineland",
                "kind": "after",
            }
        ],
        "index": {"module:focus/GER_sample": {"after": [0]}},
    }

    prefixed_result = CliRunner().invoke(
        build_app(),
        [
            "dependencies",
            str(project_root),
            "--module",
            "module:focus/GER_sample",
            "--kind",
            "after",
            "--target",
            "focus:GER_rhineland",
            "--json",
        ],
    )
    bare_source_result = CliRunner().invoke(
        build_app(),
        [
            "dependencies",
            str(project_root),
            "--source",
            "focus/GER_sample",
            "--kind",
            "after",
            "--target",
            "focus:GER_rhineland",
            "--json",
        ],
    )

    assert prefixed_result.exit_code == 0, prefixed_result.output
    assert bare_source_result.exit_code == 0, bare_source_result.output
    assert json.loads(prefixed_result.output) == payload
    assert json.loads(bare_source_result.output) == payload


def test_demo_project_build_cli_can_emit_profile_files(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(build_app(), ["build", str(project_root), "--emit-artifacts", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["summary"]["artifact_count"] == 5
    assert (project_root / "build/mod/descriptor.mod").read_text(encoding="utf-8") == (
        'version="0.1.0"\nname="Minimal HOI4 Project"\nsupported_version="1.18.*"\n'
    )
    assert (project_root / ".paradev/.cache/build/launcher/minimal_hoi4.mod").read_text(encoding="utf-8") == (
        'version="0.1.0"\n' 'name="Minimal HOI4 Project"\n' 'supported_version="1.18.*"\n' f'path="{project_root / "build/mod"}"\n'
    )
    assert (project_root / "build/mod/common/national_focus/GER_main.txt").read_text(encoding="utf-8") == ("focus = {\n\tid = GER_sample\n\tcost = 10\n}\n")
    assert not (project_root / "build/mod/views/focus-tree/GER_main.json").exists()
    view_payload = json.loads((project_root / ".paradev/.cache/build/views/focus-tree/GER_main.json").read_text(encoding="utf-8"))
    assert view_payload["schema"] == "focus-tree.view.v1"
    assert (project_root / "build/mod/localisation/english/GER_sample_l_english.yml").read_text(encoding="utf-8-sig") == (
        'l_english:\n GER_sample:0 "Sample Focus"\n GER_sample_desc:0 "Sample focus description."\n'
    )


def test_demo_project_build_cli_reports_blocked_emit_payload_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / "build")
    delete_dir(project_root / ".paradev")
    (project_root / "src/modules/focus/GER_sample/main.loc").write_text("en:\n  GER_sample: Sample Focus\n", encoding="utf-8")
    build_calls = 0
    original_build = Project.build

    def counted_build(self: Project, **kwargs: object) -> BuildResult:
        nonlocal build_calls
        build_calls += 1
        return original_build(self, **kwargs)

    monkeypatch.setattr(Project, "build", counted_build)

    result = CliRunner().invoke(build_app(), ["build", str(project_root), "--emit-artifacts", "--json"])

    assert build_calls == 1
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["summary"]["blocked"] is True
    assert payload["diagnostics"][0]["code"] == "focus.missing_localization"
    assert payload["diagnostics"][0]["module_id"] == "focus/GER_sample"
    assert not (project_root / "build/mod/common/national_focus/GER_main.txt").exists()
    assert not project_root.joinpath(".paradev/.cache/build/views/focus-tree/GER_main.json").exists()


def test_demo_project_build_cli_can_emit_source_map_manifests(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(build_app(), ["build", str(project_root), "--emit-manifests", "--json"])

    assert result.exit_code == 0, result.output
    source_map = json.loads((project_root / ".paradev/.cache/build/source-map.json").read_text(encoding="utf-8"))
    dependencies = json.loads((project_root / ".paradev/.cache/build/dependencies.json").read_text(encoding="utf-8"))
    localization = json.loads((project_root / ".paradev/.cache/build/localization.json").read_text(encoding="utf-8"))
    assert source_map["source_map"] == [
        {
            "artifact_path": "common/national_focus/GER_main.txt",
            "inputs": [str(project_root / "src/modules/focus/GER_sample/def.txt")],
            "owner": "collection:GER_main",
            "sources": [
                {
                    "family": "focus",
                    "module_id": "focus/GER_sample",
                    "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
                    "slot": "def",
                }
            ],
            "target_root": "output",
            "type": "pdx",
        },
        {
            "artifact_path": "views/focus-tree/GER_main.json",
            "inputs": [str(project_root / "src/modules/focus/GER_sample/def.txt")],
            "owner": "collection:GER_main",
            "sources": [
                {
                    "family": "focus",
                    "module_id": "focus/GER_sample",
                    "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
                    "slot": "def",
                }
            ],
            "target_root": "build",
            "type": "view",
        },
        {
            "artifact_path": "localisation/english/GER_sample_l_english.yml",
            "inputs": [str(project_root / "src/modules/focus/GER_sample/main.loc")],
            "owner": "module:focus/GER_sample",
            "sources": [
                {
                    "family": "focus",
                    "module_id": "focus/GER_sample",
                    "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
                    "slot": "loc",
                }
            ],
            "target_root": "output",
            "type": "loc",
        },
        {
            "artifact_path": "descriptor.mod",
            "inputs": [],
            "owner": "project:minimal_hoi4",
            "sources": [],
            "target_root": "output",
            "type": "mod_descriptor",
        },
        {
            "artifact_path": "launcher/minimal_hoi4.mod",
            "inputs": [],
            "owner": "project:minimal_hoi4",
            "sources": [],
            "target_root": "build",
            "type": "mod_descriptor",
        },
    ]
    assert dependencies["dependencies"] == [
        {
            "source": "module:focus/GER_sample",
            "target": "idea:GER_industrial_spirit",
            "kind": "requires",
        },
        {
            "source": "module:focus/GER_sample",
            "target": "focus:GER_rhineland",
            "kind": "after",
        },
    ]
    assert localization["localization"] == [
        {
            "module_id": "focus/GER_sample",
            "family": "focus",
            "language": "l_english",
            "key": "GER_sample",
            "text": "Sample Focus",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "family": "focus",
                "module_id": "focus/GER_sample",
                "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
                "slot": "loc",
            },
        },
        {
            "module_id": "focus/GER_sample",
            "family": "focus",
            "language": "l_english",
            "key": "GER_sample_desc",
            "text": "Sample focus description.",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "family": "focus",
                "module_id": "focus/GER_sample",
                "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
                "slot": "loc",
            },
        },
    ]
    assert localization["index"] == {
        "l_english": {
            "GER_sample": {"duplicate": False, "rows": [0]},
            "GER_sample_desc": {"duplicate": False, "rows": [1]},
        }
    }


def test_project_localization_returns_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    payload = project.localization()

    assert payload["schema"] == "paradev.build.localization.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["localization"] == [
        {
            "module_id": "focus/GER_sample",
            "family": "focus",
            "language": "l_english",
            "key": "GER_sample",
            "text": "Sample Focus",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "family": "focus",
                "module_id": "focus/GER_sample",
                "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
                "slot": "loc",
            },
        },
        {
            "module_id": "focus/GER_sample",
            "family": "focus",
            "language": "l_english",
            "key": "GER_sample_desc",
            "text": "Sample focus description.",
            "source_path": "main.loc",
            "duplicate": False,
            "source": {
                "family": "focus",
                "module_id": "focus/GER_sample",
                "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
                "slot": "loc",
            },
        },
    ]
    assert payload["index"] == {
        "l_english": {
            "GER_sample": {"duplicate": False, "rows": [0]},
            "GER_sample_desc": {"duplicate": False, "rows": [1]},
        }
    }


def test_project_cli_outputs_localization_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(build_app(), ["localization", str(project_root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.localization.v1"
    assert [row["key"] for row in payload["localization"]] == [
        "GER_sample",
        "GER_sample_desc",
    ]
    assert payload["index"] == {
        "l_english": {
            "GER_sample": {"duplicate": False, "rows": [0]},
            "GER_sample_desc": {"duplicate": False, "rows": [1]},
        }
    }
    assert payload["localization"][0]["source"] == {
        "family": "focus",
        "module_id": "focus/GER_sample",
        "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
        "slot": "loc",
    }


def test_project_modules_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.modules(
        family="focus",
        module_id="focus/GER_sample",
        collection_id="GER_main",
        source_slot="def",
    )

    assert payload == {
        "schema": "paradev.build.modules.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "modules": [
            {
                "module_id": "focus/GER_sample",
                "family": "focus",
                "root": str(project_root / "src/modules/focus/GER_sample"),
                "source_slots": {"def": ["def.txt"], "loc": ["main.loc"]},
                "metadata": {
                    "object_id": "GER_sample",
                    "type": "focus",
                    "title": "Sample Focus",
                    "owner": "GER",
                    "tags": ["sample"],
                    "collection": "GER_main",
                    "requires": ["idea:GER_industrial_spirit"],
                    "after": ["focus:GER_rhineland"],
                },
                "collection_id": "GER_main",
            }
        ],
        "index": {
            "collection": {"GER_main": [0]},
            "family": {"focus": [0]},
            "id": {"focus/GER_sample": [0]},
            "slot": {"def": [0], "loc": [0]},
        },
    }
    empty_payload = project.modules(source_slot="icon")
    assert empty_payload["modules"] == []
    assert empty_payload["index"] == {}
    assert not project.build_root.exists()


def test_project_browser_returns_frontend_ready_items_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.browser(collection_id="GER_main")

    assert payload["schema"] == "paradev.sdk.project-browser.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["filters"] == {"collection_id": "GER_main"}
    assert payload["authoring"]["source_roots"] == [
        {
            "path": str(project_root / "src"),
            "relative_path": "src",
            "default": True,
        }
    ]
    assert payload["groups"] == [
        {
            "id": "focuses",
            "family": "focus",
            "item_count": 2,
            "module_count": 1,
            "collection_count": 1,
        }
    ]
    families = {row["id"]: row for row in payload["families"]}
    assert {row["name"] for row in families["focuses"]["resource_slots"]} >= {
        "def",
        "loc",
    }
    focus_family_view = {key: value for key, value in families["focuses"].items() if key != "resource_slots"}
    focus_diagram_view = dict(focus_family_view["diagram"])
    node_authoring = focus_diagram_view.pop("node_authoring")
    focus_family_view["diagram"] = focus_diagram_view
    assert node_authoring["title"] == "Add focus"
    assert node_authoring["requires_selection"] is False
    assert [field["name"] for field in node_authoring["fields"]] == [
        "tree_id",
        "focus_id",
        "title",
        "description",
        "x",
        "y",
        "relative_position_id",
        "prerequisite_id",
        "icon_key",
    ]
    assert focus_family_view == {
        "id": "focuses",
        "family": "focus",
        "title": "Focuses",
        "group": "country",
        "title_key": "modules.focuses.title",
        "visible": True,
        "item_count": 2,
        "source_count": 3,
        "layouts": ["canonical"],
        "diagram": {
            "id": "focus_tree",
            "aliases": ["focus_trees", "focus", "focuses"],
            "renderer": "focus-tree",
            "title": "Focus tree",
            "editable": True,
            "authoring_kind": "diagram-node",
            "scope_authoring_kind": "collection",
            "relationships": [
                {
                    "kind": "prerequisite",
                    "label": "Prerequisite",
                    "visual_kind": "dependency",
                    "selected_endpoint": "target",
                    "owner_endpoint": "target",
                    "symmetric": False,
                    "cardinality": "many",
                },
                {
                    "kind": "mutually_exclusive",
                    "label": "Mutually exclusive",
                    "visual_kind": "reference",
                    "selected_endpoint": "source",
                    "owner_endpoint": "source",
                    "symmetric": True,
                    "cardinality": "many",
                },
            ],
        },
    }
    assert families["ideas"]["item_count"] == 0
    assert families["mod-descriptor"]["visible"] is False
    assert payload["index"]["id"] == {
        "collection:focus/GER_main": [0],
        "module:focus/GER_sample": [1],
    }
    assert payload["index"]["kind"] == {"collection": [0], "module": [1]}
    assert payload["index"]["family"] == {"focus": [0, 1]}
    assert payload["index"]["family_id"] == {"focuses": [0, 1]}
    assert payload["index"]["module_id"] == {"focus/GER_sample": [1]}
    assert payload["index"]["collection_id"] == {"GER_main": [0, 1]}
    assert payload["index"]["object_id"] == {"GER_main": [0], "GER_sample": [1]}
    collection, module = payload["items"]
    assert collection["id"] == "collection:focus/GER_main"
    assert collection["kind"] == "collection"
    assert collection["layout"] == "canonical"
    assert collection["family_id"] == "focuses"
    assert collection["object_id"] == "GER_main"
    assert collection["collection_id"] == "GER_main"
    assert collection["module_ids"] == ["focus/GER_sample"]
    assert collection["source_count"] == 0
    assert collection["resource_slots"] == []
    assert module["id"] == "module:focus/GER_sample"
    assert module["kind"] == "module"
    assert module["layout"] == "canonical"
    assert module["family_id"] == "focuses"
    assert module["object_id"] == "GER_sample"
    assert module["label"] == "Sample Focus"
    module_root = project_root / "src/modules/focus/GER_sample"
    assert module["root"] == str(module_root)
    assert module["relative_path"] == "src/modules/focus/GER_sample"
    assert module["relative_root"] == "src/modules/focus/GER_sample"
    assert module["source_root_relative_path"] == "src"
    assert module["source_slots"] == {
        "def": ["def.txt"],
        "loc": ["main.loc"],
        "meta": ["meta.yaml"],
    }
    for relative_paths in module["source_slots"].values():
        for source_relative_path in relative_paths:
            assert not Path(source_relative_path).is_absolute()
            assert (module_root / source_relative_path).is_file()
    assert module["source_count"] == 3
    sources = {row["slot"]: row for row in module["sources"]}
    assert sources["def"]["path"] == str(module_root / "def.txt")
    assert sources["def"]["relative_path"] == "src/modules/focus/GER_sample/def.txt"
    assert sources["def"]["slot_kinds"] == ["pdx"]
    assert sources["loc"]["path"] == str(module_root / "main.loc")
    assert sources["loc"]["relative_path"] == "src/modules/focus/GER_sample/main.loc"
    assert sources["loc"]["slot_kinds"] == ["loc"]
    assert sources["meta"]["path"] == str(module_root / "meta.yaml")
    assert sources["meta"]["relative_path"] == "src/modules/focus/GER_sample/meta.yaml"
    assert sources["meta"]["slot_kinds"] == []
    assert {row["name"] for row in module["resource_slots"]} >= {"def", "loc"}
    assert module["metadata"]["title"] == "Sample Focus"
    assert not project.build_root.exists()

    modules_only = project.browser(kind="module", module_id="focus/GER_sample")
    assert modules_only["filters"] == {
        "kind": "module",
        "module_id": "focus/GER_sample",
    }
    assert [item["id"] for item in modules_only["items"]] == ["module:focus/GER_sample"]
    with pytest.raises(ValueError, match="module or collection"):
        project.browser(kind="artifact")


def test_project_browser_exposes_only_exact_absent_family_image_targets(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "image-target-project")
    project.scaffold_module(
        "hoi4:idea/basic",
        "IDEA_WITH_ICON",
        values={"title": "Idea With Icon"},
        write=True,
    )
    module_root = project.source_roots[0] / "modules/idea/IDEA_WITH_ICON"
    icon_path = module_root / "icon.png"

    payload = project.browser(kind="module", module_id="idea/IDEA_WITH_ICON")
    [item] = payload["items"]

    assert item["image_targets"] == [
        {
            "slot": "icon",
            "slot_kinds": ["copy"],
            "name": "icon.png",
            "path": str(icon_path),
            "relative_path": "src/modules/idea/IDEA_WITH_ICON/icon.png",
            "extension": "png",
            "exists": False,
        }
    ]
    assert not icon_path.exists()

    project.apply_source_draft(
        source_replacements=[
            {
                "path": item["image_targets"][0]["relative_path"],
                "content_base64": ONE_PIXEL_PNG_BASE64,
                "expected_absent": True,
            }
        ]
    )

    [refreshed] = project.browser(
        kind="module",
        module_id="idea/IDEA_WITH_ICON",
    )["items"]
    assert "image_targets" not in refreshed
    assert next(source for source in refreshed["sources"] if source["slot"] == "icon") == {
        "slot": "icon",
        "slot_kinds": ["copy"],
        "name": "icon.png",
        "path": str(icon_path),
        "relative_path": "src/modules/idea/IDEA_WITH_ICON/icon.png",
        "extension": "png",
        "size": icon_path.stat().st_size,
        "mtime_ns": str(icon_path.stat().st_mtime_ns),
    }


def test_project_browser_authoring_metadata_slots_follow_loader_filenames(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "module"
    collection_root = tmp_path / "collection"
    module_root.mkdir()
    collection_root.mkdir()
    (module_root / "meta.yml").write_text("title: Ignored metadata\n", encoding="utf-8")
    (collection_root / "collection.yaml").write_text("title: Collection metadata\n", encoding="utf-8")

    module_slots = project_sdk._project_browser_authoring_source_slots({}, root=module_root, kind="module")
    collection_slots = project_sdk._project_browser_authoring_source_slots({}, root=collection_root, kind="collection")

    assert module_slots == {}
    assert collection_slots == {"meta": ["collection.yaml"]}


def test_project_browser_summary_counts_authoring_folders_without_source_parse(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "summary-browser-project"
    module_root = project_root / "src/modules/custom_family/ITEM_A"
    nested_module_root = project_root / "src/modules/custom_family/ITEM_B/nested"
    collection_root = project_root / "src/collections/custom_family/COL_A"
    source_folder_root = project_root / "src/custom_family/LEGACY_A"
    nested_inactive_root = project_root / "src/inactive_modules/bookmark/nested"
    module_root.mkdir(parents=True)
    nested_module_root.mkdir(parents=True)
    collection_root.mkdir(parents=True)
    source_folder_root.mkdir(parents=True)
    nested_inactive_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("not: [valid\n", encoding="utf-8")
    (module_root / "def.txt").write_text("not valid pdx = {\n", encoding="utf-8")
    (nested_module_root / "def.txt").write_text("nested = yes\n", encoding="utf-8")
    (collection_root / "def.txt").write_text("collection = yes\n", encoding="utf-8")
    (source_folder_root / "def.txt").write_text("legacy = yes\n", encoding="utf-8")
    (nested_inactive_root / "info.json").write_text("{}\n", encoding="utf-8")
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: summary_browser_project",
                "title: Summary Browser Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )

    project = Project.load(project_root)
    payload = project.browser_summary()
    families = {row["id"]: row for row in payload["families"]}
    groups = {row["id"]: row for row in payload["groups"]}

    assert payload["schema"] == "paradev.sdk.project-browser.v1"
    assert payload["items"] == []
    assert payload["index"] == {}
    assert families["custom-family"]["family"] == "custom_family"
    assert families["custom-family"]["item_count"] == 4
    assert families["custom-family"]["source_count"] >= 5
    assert "inactive-modules" not in families
    assert groups["custom-family"]["item_count"] == 4
    assert groups["custom-family"]["module_count"] == 2
    assert groups["custom-family"]["collection_count"] == 1
    assert not project.build_root.exists()


def test_project_browser_exposes_project_family_visibility_without_disabling_builds(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "browser-family-visibility-project"
    hidden_root = project_root / "src/modules/idea_support/ITEM_A"
    visible_component_root = project_root / "src/modules/visible_component/ITEM_B"
    unknown_root = project_root / "src/modules/unknown_family/ITEM_C"
    hidden_root.mkdir(parents=True)
    visible_component_root.mkdir(parents=True)
    unknown_root.mkdir(parents=True)
    (hidden_root / "hidden.dds").write_bytes(b"hidden asset")
    (visible_component_root / "visible.dds").write_bytes(b"visible asset")
    (unknown_root / "def.pdx").write_text("unknown = {}\n", encoding="utf-8")
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: browser_family_visibility_project",
                "title: Browser Family Visibility Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  idea_support:",
                "    visible: false",
                "    source_slots:",
                "      - name: copy",
                "        match: '**/*'",
                "        kind: copy",
                "        many: true",
                "    templates:",
                "      copy: '{source_path}'",
                "  visible_component:",
                "    source_slots:",
                "      - name: copy",
                "        match: '**/*'",
                "        kind: copy",
                "        many: true",
                "    templates:",
                "      copy: '{source_path}'",
            ]
        ),
        encoding="utf-8",
    )

    project = Project.load(project_root)
    summary_families = {row["family"]: row for row in project.browser_summary()["families"]}
    browser_families = {row["family"]: row for row in project.browser()["families"]}

    assert summary_families["idea_support"]["visible"] is False
    assert summary_families["visible_component"]["visible"] is True
    assert summary_families["unknown_family"]["visible"] is True
    assert browser_families["idea_support"]["visible"] is False
    assert browser_families["visible_component"]["visible"] is True

    hidden_contract = project.families(family="idea_support")["families"][0]
    assert hidden_contract["visible"] is False

    result = project.build()
    assert {module.module_id for module in result.modules} >= {
        "idea_support/ITEM_A",
        "visible_component/ITEM_B",
    }
    assert {str(artifact.path) for artifact in result.artifacts} >= {
        "hidden.dds",
        "visible.dds",
    }


def test_project_browser_scopes_family_module_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "scoped-browser-project"
    focus_root = project_root / "src/modules/focus_tree/C01_MAIN"
    unrelated_root = project_root / "src/modules/unrelated/SHOULD_NOT_PARSE"
    focus_root.mkdir(parents=True)
    unrelated_root.mkdir(parents=True)
    (focus_root / "meta.yaml").write_text("type: focus_tree\ntitle: C01 Main\n", encoding="utf-8")
    (focus_root / "def.txt").write_text("focus_tree = { id = C01 }\n", encoding="utf-8")
    (unrelated_root / "meta.yaml").write_text("type: unrelated\ntitle: Should Not Parse\n", encoding="utf-8")
    (unrelated_root / "def.txt").write_text("unrelated = {}\n", encoding="utf-8")
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: scoped_browser_project",
                "title: Scoped Browser Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  focus_tree:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/national_focus/{object_id}.txt",
                "  unrelated:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/unrelated/{object_id}.txt",
            ]
        ),
        encoding="utf-8",
    )

    import paradev.build.discovery as discovery

    loaded_roots: list[str] = []
    load_module_sources = discovery.load_module_sources

    def recording_load_module_sources(root: Path, *args: object, **kwargs: object) -> object:
        loaded_roots.append(root.relative_to(project_root).as_posix())
        return load_module_sources(root, *args, **kwargs)

    monkeypatch.setattr(discovery, "load_module_sources", recording_load_module_sources)

    payload = Project.load(project_root).browser(family="focus_tree", module_id="C01_MAIN")

    assert payload["filters"] == {
        "family": "focus_tree",
        "module_id": "focus_tree/C01_MAIN",
    }
    assert [item["id"] for item in payload["items"]] == ["module:focus_tree/C01_MAIN"]
    assert loaded_roots == ["src/modules/focus_tree/C01_MAIN"]


def test_project_browser_lists_family_root_source_folders(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "browser-project", title="Browser Project")
    project.scaffold_module("hoi4:idea/basic", "GER_industry_spirit", write=True)
    decision_root = project.source_roots[0] / "decisions/DECISION_CATEGORY_TEST - Test Decisions"
    decision_root.mkdir(parents=True)
    (decision_root / "def.txt").write_text("DECISION_CATEGORY_TEST = {}", encoding="utf-8")
    (decision_root / "main.loc").write_text("en:\n  DECISION_CATEGORY_TEST: Test Decisions\n", encoding="utf-8")

    payload = Project.load(project.root).browser()
    families = {row["id"]: row for row in payload["families"]}
    decision = next(row for row in payload["items"] if row.get("family_id") == "decisions")
    sources = {row["slot"]: row for row in decision["sources"]}

    assert payload["schema"] == "paradev.sdk.project-browser.v1"
    assert families["decisions"]["item_count"] == 1
    assert decision["kind"] == "source_folder"
    assert decision["layout"] == "family_root"
    assert decision["title"] == "Test Decisions"
    assert sources["def"]["path"] == str(decision_root / "def.txt")
    assert sources["def"]["relative_path"] == "src/decisions/DECISION_CATEGORY_TEST - Test Decisions/def.txt"
    assert sources["loc"]["path"] == str(decision_root / "main.loc")
    assert sources["loc"]["relative_path"] == "src/decisions/DECISION_CATEGORY_TEST - Test Decisions/main.loc"


def test_project_browser_prefers_canonical_module_over_shadowed_family_root_folder(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "character-browser-project"
    module_root = project_root / "src/modules/character/CHARACTER_ABYSSINIA_PARLIAMENT"
    legacy_root = project_root / "src/characters/CHARACTER_ABYSSINIA_PARLIAMENT - Abyssinian Parliament"
    module_root.mkdir(parents=True)
    legacy_root.mkdir(parents=True)
    (module_root / "meta.yaml").write_text("type: character\ntitle: Metadata Character\n", encoding="utf-8")
    (module_root / "def.txt").write_text(
        "characters = {\n" "    CHARACTER_ABYSSINIA_PARLIAMENT = {\n" "        name = CHARACTER_ABYSSINIA_PARLIAMENT_NAME\n" "    }\n" "}\n",
        encoding="utf-8",
    )
    (module_root / "main.loc").write_text(
        "en:\n" "  CHARACTER_ABYSSINIA_PARLIAMENT_NAME: Abyssinian Parliament\n" "zh:\n" "  CHARACTER_ABYSSINIA_PARLIAMENT_NAME: Abyssinian Parliament CN\n",
        encoding="utf-8",
    )
    (module_root / "portrait.png").write_bytes(pack("!Q", 1))
    (legacy_root / "main.loc").write_text(
        "zh:\n  CHARACTER_ABYSSINIA_PARLIAMENT: Abyssinian Parliament CN\n",
        encoding="utf-8",
    )
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: character_browser_project",
                "title: Character Browser Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  character:",
                "    kind: simple_source",
                "    presentation:",
                "      id: characters",
                "      title: Characters",
                "      group: country",
                "      title_key: modules.characters.title",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "      - name: loc",
                "        match: '*.loc'",
                "        many: true",
                "        kind: loc",
                "      - name: portrait",
                "        match: '^portrait\\.(png|dds|tga)$'",
                "        regex: true",
                "        kind: copy",
                "    templates:",
                "      pdx: common/characters/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
                "      copy: gfx/leaders/{object_id}{source_suffix}",
            ]
        ),
        encoding="utf-8",
    )

    payload = Project.load(project_root).browser(family="characters")

    assert [item["id"] for item in payload["items"]] == ["module:character/CHARACTER_ABYSSINIA_PARLIAMENT"]
    character = payload["items"][0]
    sources = {row["slot"]: row for row in character["sources"]}
    assert character["kind"] == "module"
    assert character["layout"] == "canonical"
    assert character["title"] == "Metadata Character"
    assert character["localized_titles"] == {
        "l_english": "Abyssinian Parliament",
        "l_simp_chinese": "Abyssinian Parliament CN",
    }
    assert character["root"] == str(module_root)
    assert character["relative_root"] == "src/modules/character/CHARACTER_ABYSSINIA_PARLIAMENT"
    assert sources["portrait"]["path"] == str(module_root / "portrait.png")
    assert sources["portrait"]["relative_path"] == "src/modules/character/CHARACTER_ABYSSINIA_PARLIAMENT/portrait.png"
    families = {row["id"]: row for row in payload["families"]}
    assert {row["name"] for row in families["characters"]["resource_slots"]} >= {
        "def",
        "loc",
    }
    assert {key: value for key, value in families["characters"].items() if key != "resource_slots"} == {
        "id": "characters",
        "family": "character",
        "title": "Characters",
        "group": "country",
        "title_key": "modules.characters.title",
        "visible": True,
        "item_count": 1,
        "source_count": 4,
        "layouts": ["canonical"],
    }


def test_project_browser_uses_registry_owned_title_localization_keys(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "title-key-browser-project"
    agency_root = project_root / "src/modules/agency/AGENCY_TEST - Human Agency"
    dynamic_root = project_root / "src/modules/dynamic/DYNAMIC_DAM - Dam Mountain"
    agency_root.mkdir(parents=True)
    dynamic_root.mkdir(parents=True)
    (agency_root / "def.txt").write_text(
        "AGENCY_TEST = { name = AGENCY_TEST }\n",
        encoding="utf-8",
    )
    (agency_root / "main.loc").write_text(
        "[en.AGENCY_TEST]\nTEST\n\n[en.AGENCY_TEST_NAME]\nHuman Agency\n",
        encoding="utf-8",
    )
    (dynamic_root / "def.txt").write_text(
        "DYNAMIC_DAM = {}\n",
        encoding="utf-8",
    )
    (dynamic_root / "main.loc").write_text(
        "[en.DYNAMIC_DAM]\n$dam$\n",
        encoding="utf-8",
    )
    (project_root / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: title_key_browser_project",
                "title: Title Key Browser Project",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  agency:",
                "    kind: simple_source",
                "    title_loc_keys:",
                "      - '{object_id}_NAME'",
                "      - '{object_id}'",
                "    source_slots:",
                "      - {name: def, match: def.txt, kind: pdx}",
                "      - {name: loc, match: '*.loc', kind: loc, many: true}",
                "    templates:",
                "      pdx: common/agency/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
                "  dynamic:",
                "    kind: simple_source",
                "    title_loc_keys: []",
                "    source_slots:",
                "      - {name: def, match: def.txt, kind: pdx}",
                "      - {name: loc, match: '*.loc', kind: loc, many: true}",
                "    templates:",
                "      pdx: common/dynamic/{object_id}.txt",
                "      loc: localisation/{language_folder}/{object_id}_{language}.yml",
            ]
        ),
        encoding="utf-8",
    )

    project = Project.load(project_root)
    agency = project.browser(family="agency")["items"][0]
    dynamic = project.browser(family="dynamic")["items"][0]
    family_views = {row["family"]: row for row in project._build_registry(profile="hoi4").to_view()["families"]}

    assert agency["title"] == "Human Agency"
    assert agency["localized_titles"] == {"l_english": "Human Agency"}
    assert agency["title_keys"] == ["AGENCY_TEST_NAME", "AGENCY_TEST"]
    assert dynamic["title"] == "Dam Mountain"
    assert dynamic["localized_titles"] == {}
    assert dynamic["title_keys"] == []
    assert family_views["agency"]["localization"]["title_keys"] == [
        "{object_id}_NAME",
        "{object_id}",
    ]
    assert family_views["dynamic"]["localization"]["title_keys"] == []


def test_project_inspect_dispatches_filtered_modules_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.inspect(
        "modules",
        family="focus",
        module_id="focus/GER_sample",
        collection_id="GER_main",
        source_slot="def",
    )

    assert payload == project.modules(
        family="focus",
        module_id="focus/GER_sample",
        collection_id="GER_main",
        source_slot="def",
    )
    assert not project.build_root.exists()


def test_project_inspect_rejects_unknown_kind(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    with pytest.raises(ValueError, match="Unknown project inspection kind: nope"):
        project.inspect("nope")


def test_project_inspect_rejects_unsupported_filter_without_hiding_method_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    with pytest.raises(
        ValueError,
        match="Unsupported filters for project inspection kind 'modules': bogus",
    ):
        project.inspect("modules", bogus=True)

    def broken_summary(self: Project, *, profile: str | None = None) -> dict[str, object]:
        raise TypeError("internal summary bug")

    monkeypatch.setattr(Project, "summary", broken_summary)

    with pytest.raises(TypeError, match="internal summary bug"):
        project.inspect("summary")


def test_project_inspect_allows_kind_as_filter_name(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    payload = project.inspect(
        "dependencies",
        module_id="focus/GER_sample",
        kind="after",
    )

    assert payload["dependencies"] == [
        {
            "source": "module:focus/GER_sample",
            "target": "focus:GER_rhineland",
            "kind": "after",
        }
    ]


def test_project_inspections_describes_sdk_dispatch_contract(tmp_path: Path) -> None:
    from paradev.sdk import get_project_inspection_contract

    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    payload = project.inspections()
    static_contract = get_project_inspection_contract()
    rows = {row["kind"]: row for row in payload["inspections"]}
    static_rows = {row["kind"]: row for row in static_contract["inspections"]}

    assert payload["schema"] == "paradev.sdk.inspections.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert "project_id" not in static_contract
    assert static_rows["modules"]["filters"] == [
        "profile",
        "family",
        "module_id",
        "collection_id",
        "source_slot",
    ]
    assert static_contract["index"]["filter"]["module_id"] == payload["index"]["filter"]["module_id"]
    assert project.inspect("inspections") == payload
    assert rows["modules"] == {
        "kind": "modules",
        "method": "Project.modules",
        "cli_command": "modules",
        "filters": ["profile", "family", "module_id", "collection_id", "source_slot"],
    }
    assert rows["dependencies"]["filters"] == [
        "profile",
        "source",
        "target",
        "kind",
        "module_id",
    ]
    assert rows["catalog-preview"] == {
        "kind": "catalog-preview",
        "method": "Project.catalog_preview",
        "cli_command": "hb catalog-preview",
        "filters": ["profile"],
    }
    assert rows["catalog-query"] == {
        "kind": "catalog-query",
        "method": "Project.catalog_query",
        "cli_command": "hb catalog-query",
        "filters": [
            "database",
            "entity",
            "target_id",
            "name",
            "tag",
            "limit",
            "offset",
            "include_data",
        ],
    }
    assert rows["sources"] == {
        "kind": "sources",
        "method": "Project.sources",
        "cli_command": "sources",
        "filters": [
            "profile",
            "family",
            "module_id",
            "collection_id",
            "slot",
            "loader",
            "status",
            "owner_kind",
        ],
    }
    assert rows["source-slots"] == {
        "kind": "source-slots",
        "method": "Project.source_slots",
        "cli_command": "source-slots",
        "filters": [
            "profile",
            "family",
            "module_id",
            "collection_id",
            "slot",
            "status",
        ],
    }
    assert rows["build-explain"]["filters"] == [
        "module_id",
        "collection_id",
        "source_path",
        "artifact_path",
        "diagnostic_code",
        "target_root",
        "profile",
    ]
    assert "registry" not in rows["modules"]["filters"]
    assert payload["index"]["kind"]["modules"] == [payload["inspections"].index(rows["modules"])]
    assert "modules" in payload["index"]["filter"]["module_id"]
    assert "catalog-query" in payload["index"]["filter"]["entity"]


def test_project_cli_outputs_inspections_contract_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(build_app(), ["inspections", str(project_root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = {row["kind"]: row for row in payload["inspections"]}
    assert payload["schema"] == "paradev.sdk.inspections.v1"
    assert rows["source-map"]["cli_command"] == "source-map"
    assert rows["families"]["filters"] == [
        "profile",
        "family",
        "kind",
        "source_slot",
        "collection_source_slot",
        "sprite_slot",
        "route",
        "artifact_type",
    ]


def test_project_cli_filters_modules_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "modules",
            str(project_root),
            "--family",
            "focus",
            "--module",
            "focus/GER_sample",
            "--collection",
            "GER_main",
            "--slot",
            "loc",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.modules.v1"
    assert [row["module_id"] for row in payload["modules"]] == ["focus/GER_sample"]
    assert payload["modules"][0]["source_slots"] == {
        "def": ["def.txt"],
        "loc": ["main.loc"],
    }
    assert payload["index"] == {
        "collection": {"GER_main": [0]},
        "family": {"focus": [0]},
        "id": {"focus/GER_sample": [0]},
        "slot": {"def": [0], "loc": [0]},
    }


def test_project_cli_prints_project_browser_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "project-browser",
            str(project_root),
            "--kind",
            "module",
            "--module",
            "focus/GER_sample",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.sdk.project-browser.v1"
    assert payload["filters"] == {"kind": "module", "module_id": "focus/GER_sample"}
    assert [row["id"] for row in payload["items"]] == ["module:focus/GER_sample"]
    assert payload["index"]["kind"] == {"module": [0]}


def test_project_cli_prints_project_browser_summary_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "project-browser",
            str(project_root),
            "--summary",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.sdk.project-browser.v1"
    assert payload["items"] == []
    assert payload["families"]
    assert payload["index"] == {}


def test_project_cli_modules_uses_sdk_inspection_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProject:
        def inspect(self, kind: str, **filters: object) -> dict[str, object]:
            assert kind == "modules"
            assert filters == {
                "profile": "test-profile",
                "family": "focus",
                "module_id": "focus/GER_sample",
                "collection_id": None,
                "source_slot": "def",
            }
            return {"schema": "fake.modules", "modules": []}

        def modules(self, **filters: object) -> dict[str, object]:
            raise AssertionError("CLI should use Project.inspect instead of Project.modules directly.")

    def fake_open_project(path: str) -> FakeProject:
        assert path == "demo"
        return FakeProject()

    monkeypatch.setattr("paradev.cli.open_project", fake_open_project)

    result = CliRunner().invoke(
        build_app(),
        [
            "modules",
            "demo",
            "--profile",
            "test-profile",
            "--family",
            "focus",
            "--module",
            "focus/GER_sample",
            "--slot",
            "def",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"schema": "fake.modules", "modules": []}


def test_project_collections_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.collections(
        family="focus",
        collection_id="GER_main",
        module_id="focus/GER_sample",
    )

    assert payload == {
        "schema": "paradev.build.collections.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "collections": [
            {
                "collection_id": "GER_main",
                "family": "focus",
                "module_ids": ["focus/GER_sample"],
                "metadata": {},
            }
        ],
        "index": {
            "family": {"focus": [0]},
            "id": {"GER_main": [0]},
            "module": {"focus/GER_sample": [0]},
        },
    }
    empty_payload = project.collections(module_id="focus/GER_missing")
    assert empty_payload["collections"] == []
    assert empty_payload["index"] == {}
    assert not project.build_root.exists()


def test_project_collections_filters_collection_descriptor_source_slots(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.collections(source_slot="strings")
    empty_payload = project.collections(source_slot="missing")

    assert payload == {
        "schema": "paradev.build.collections.v1",
        "project_id": "collection_slot_project",
        "profile": "hoi4",
        "collections": [
            {
                "collection_id": "germany",
                "family": "bulletin",
                "module_ids": ["bulletin/GER_news"],
                "source_slots": {
                    "category": ["category.txt"],
                    "strings": ["strings.yml"],
                },
                "metadata": {"object_id": "germany"},
            }
        ],
        "index": {
            "family": {"bulletin": [0]},
            "id": {"germany": [0]},
            "module": {"bulletin/GER_news": [0]},
            "slot": {"category": [0], "strings": [0]},
        },
    }
    assert empty_payload["collections"] == []
    assert empty_payload["index"] == {}
    assert not project.build_root.exists()


def test_project_cli_filters_collections_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "collections",
            str(project_root),
            "--family",
            "focus",
            "--collection",
            "GER_main",
            "--module",
            "focus/GER_sample",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.collections.v1"
    assert [row["collection_id"] for row in payload["collections"]] == ["GER_main"]
    assert payload["collections"][0]["module_ids"] == ["focus/GER_sample"]
    assert payload["index"] == {
        "family": {"focus": [0]},
        "id": {"GER_main": [0]},
        "module": {"focus/GER_sample": [0]},
    }


def test_project_cli_filters_collection_descriptor_source_slots(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)

    result = CliRunner().invoke(
        build_app(),
        [
            "collections",
            str(tmp_path),
            "--slot",
            "strings",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["collection_id"] for row in payload["collections"]] == ["germany"]
    assert payload["collections"][0]["source_slots"] == {
        "category": ["category.txt"],
        "strings": ["strings.yml"],
    }
    assert payload["index"] == {
        "family": {"bulletin": [0]},
        "id": {"germany": [0]},
        "module": {"bulletin/GER_news": [0]},
        "slot": {"category": [0], "strings": [0]},
    }


def test_project_artifacts_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.artifacts(
        artifact_type="pdx",
        target_root="output",
        path="common/national_focus/GER_main.txt",
        mode="plan",
        collection_id="GER_main",
    )

    assert payload == {
        "schema": "paradev.build.artifacts.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "artifacts": [
            {
                "path": "common/national_focus/GER_main.txt",
                "type": "pdx",
                "owner": "collection:GER_main",
                "inputs": [str(project_root / "src/modules/focus/GER_sample/def.txt")],
                "mode": "plan",
                "target_root": "output",
                "metadata": {
                    "module_ids": ["focus/GER_sample"],
                    "family": "focus",
                    "collection_id": "GER_main",
                },
            }
        ],
        "index": {
            "collection": {"GER_main": [0]},
            "mode": {"plan": [0]},
            "module": {"focus/GER_sample": [0]},
            "owner": {"collection:GER_main": [0]},
            "path": {"common/national_focus/GER_main.txt": [0]},
            "target_root": {"output": [0]},
            "type": {"pdx": [0]},
        },
    }
    module_payload = project.artifacts(module_id="focus/GER_sample")
    assert [row["path"] for row in module_payload["artifacts"]] == [
        "common/national_focus/GER_main.txt",
        "views/focus-tree/GER_main.json",
        "localisation/english/GER_sample_l_english.yml",
    ]
    assert module_payload["index"]["collection"] == {"GER_main": [0, 1]}
    assert module_payload["index"]["module"] == {"focus/GER_sample": [0, 1, 2]}
    empty_payload = project.artifacts(module_id="focus/GER_missing")
    assert empty_payload["artifacts"] == []
    assert empty_payload["index"] == {}
    assert not project.build_root.exists()


def test_project_cli_filters_artifacts_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "artifacts",
            str(project_root),
            "--type",
            "pdx",
            "--target-root",
            "output",
            "--path",
            "common/national_focus/GER_main.txt",
            "--collection",
            "GER_main",
            "--module",
            "focus/GER_sample",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.artifacts.v1"
    assert [row["path"] for row in payload["artifacts"]] == ["common/national_focus/GER_main.txt"]
    assert payload["artifacts"][0]["owner"] == "collection:GER_main"
    assert payload["index"] == {
        "collection": {"GER_main": [0]},
        "mode": {"plan": [0]},
        "module": {"focus/GER_sample": [0]},
        "owner": {"collection:GER_main": [0]},
        "path": {"common/national_focus/GER_main.txt": [0]},
        "target_root": {"output": [0]},
        "type": {"pdx": [0]},
    }


def test_project_localization_filters_rows_by_language_alias_key_prefix_and_module(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    payload = project.localization(
        language="en",
        key="GER_sample_desc",
        key_prefix="GER_sample",
        module_id="focus/GER_sample",
    )

    assert [row["key"] for row in payload["localization"]] == ["GER_sample_desc"]
    assert payload["localization"][0]["language"] == "l_english"
    assert payload["index"] == {"l_english": {"GER_sample_desc": {"duplicate": False, "rows": [0]}}}


def test_project_cli_filters_localization_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "localization",
            str(project_root),
            "--language",
            "en",
            "--key",
            "GER_sample_desc",
            "--key-prefix",
            "GER_sample",
            "--module",
            "focus/GER_sample",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["key"] for row in payload["localization"]] == ["GER_sample_desc"]
    assert payload["localization"][0]["language"] == "l_english"
    assert payload["index"] == {"l_english": {"GER_sample_desc": {"duplicate": False, "rows": [0]}}}


def test_project_cli_filters_collection_localization_manifest_json(
    tmp_path: Path,
) -> None:
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
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_localization",
                "title: Test Collection Localization",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        build_app(),
        [
            "localization",
            str(tmp_path),
            "--collection",
            "GER_industry",
            "--language",
            "en",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["key"] for row in payload["localization"]] == [
        "GER_industry",
        "GER_industry_desc",
    ]
    assert payload["localization"][0]["collection_id"] == "GER_industry"
    assert payload["localization"][0]["source"] == {
        "collection_id": "GER_industry",
        "family": "decision",
        "path": str(collection_root / "main.loc"),
        "slot": "loc",
    }
    assert payload["index"] == {
        "l_english": {
            "GER_industry": {"duplicate": False, "rows": [0]},
            "GER_industry_desc": {"duplicate": False, "rows": [1]},
        }
    }


def test_project_assets_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    asset_root = project_root / "src/modules/focus/GER_sample/assets/interface"
    asset_root.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n" + pack(">I4sIIBBBBBI", 13, b"IHDR", 40, 30, 8, 6, 0, 0, 0, 0)
    (asset_root / "icon.png").write_bytes(payload)
    project = Project.load(project_root)

    manifest = project.assets(module_id="focus/GER_sample", family="focus", slot="assets", file_format="png")

    assert manifest == {
        "schema": "paradev.build.assets.v1",
        "project_id": "minimal_hoi4",
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
                "sha256": sha256hash(payload),
                "size": len(payload),
                "media_type": "image/png",
                "format": "png",
                "width": 40,
                "height": 30,
                "source": {
                    "family": "focus",
                    "module_id": "focus/GER_sample",
                    "path": str(asset_root / "icon.png"),
                    "slot": "assets",
                },
            }
        ],
        "index": {"focus/GER_sample": {"assets": [0]}},
    }
    assert not project.build_root.exists()


def test_project_cli_filters_asset_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    asset_root = project_root / "src/modules/focus/GER_sample/assets/interface"
    asset_root.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\n" + pack(">I4sIIBBBBBI", 13, b"IHDR", 40, 30, 8, 6, 0, 0, 0, 0)
    (asset_root / "icon.png").write_bytes(payload)

    result = CliRunner().invoke(
        build_app(),
        [
            "assets",
            str(project_root),
            "--family",
            "focus",
            "--module",
            "focus/GER_sample",
            "--slot",
            "assets",
            "--format",
            "png",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    assert manifest["schema"] == "paradev.build.assets.v1"
    assert manifest["index"] == {"focus/GER_sample": {"assets": [0]}}
    assert [row["artifact_path"] for row in manifest["assets"]] == ["gfx/paradev/GER_sample/assets/interface/icon.png"]
    assert manifest["assets"][0]["format"] == "png"
    assert manifest["assets"][0]["source"] == {
        "family": "focus",
        "module_id": "focus/GER_sample",
        "path": str(asset_root / "icon.png"),
        "slot": "assets",
    }


def test_project_sprites_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    idea_root = project_root / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = { country = { GER_industry_spirit = { picture = GER_industry_spirit } } }",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  GER_industry_spirit: German Industry Spirit\n  GER_industry_spirit_desc: Industrial production spirit.\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(b"sample idea icon bytes")
    project = Project.load(project_root)

    payload = project.sprites(
        module_id="idea/GER_industry_spirit",
        family="idea",
        slot="icon",
        name="GFX_idea_GER_industry_spirit",
    )

    assert payload == {
        "schema": "paradev.build.sprites.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "sprites": [
            {
                "name": "GFX_idea_GER_industry_spirit",
                "texturefile": "gfx/interface/ideas/idea_GER_industry_spirit.dds",
                "artifact_path": "interface/paradev_idea.gfx",
                "owner": "project:minimal_hoi4",
                "target_root": "output",
                "family": "idea",
                "module_id": "idea/GER_industry_spirit",
                "slot": "icon",
                "source_path": str(idea_root / "icon.dds"),
                "source": {
                    "family": "idea",
                    "module_id": "idea/GER_industry_spirit",
                    "path": str(idea_root / "icon.dds"),
                    "slot": "icon",
                },
            }
        ],
        "index": {"idea/GER_industry_spirit": {"icon": [0]}},
    }
    assert not project.build_root.exists()


def test_project_cli_filters_sprite_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    idea_root = project_root / "src/modules/idea/GER_industry_spirit"
    idea_root.mkdir(parents=True)
    (idea_root / "meta.yaml").write_text("type: idea\n", encoding="utf-8")
    (idea_root / "def.txt").write_text(
        "ideas = { country = { GER_industry_spirit = { picture = GER_industry_spirit } } }",
        encoding="utf-8",
    )
    (idea_root / "main.loc").write_text(
        "en:\n  GER_industry_spirit: German Industry Spirit\n  GER_industry_spirit_desc: Industrial production spirit.\n",
        encoding="utf-8",
    )
    (idea_root / "icon.dds").write_bytes(b"sample idea icon bytes")

    result = CliRunner().invoke(
        build_app(),
        [
            "sprites",
            str(project_root),
            "--family",
            "idea",
            "--module",
            "idea/GER_industry_spirit",
            "--slot",
            "icon",
            "--name",
            "GFX_idea_GER_industry_spirit",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.sprites.v1"
    assert payload["index"] == {"idea/GER_industry_spirit": {"icon": [0]}}
    assert [row["name"] for row in payload["sprites"]] == ["GFX_idea_GER_industry_spirit"]
    assert payload["sprites"][0]["texturefile"] == "gfx/interface/ideas/idea_GER_industry_spirit.dds"
    assert payload["sprites"][0]["source"] == {
        "family": "idea",
        "module_id": "idea/GER_industry_spirit",
        "path": str(idea_root / "icon.dds"),
        "slot": "icon",
    }


def test_project_cli_filters_collection_asset_manifest_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                "project_id: test_collection_assets",
                "title: Test Collection Assets",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            copy_path_template="gfx/events/{object_id}/{source_name}",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(Slot("media", "media/*", many=True, kind="copy"),),
        )
    )
    monkeypatch.setattr("paradev.games.registry_for_profile", lambda _profile: registry)

    result = CliRunner().invoke(
        build_app(),
        [
            "assets",
            str(tmp_path),
            "--collection",
            "germany",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    assert manifest["index"] == {"germany": {"media": [0]}}
    assert manifest["assets"] == [
        {
            "collection_id": "germany",
            "family": "event",
            "slot": "media",
            "source_path": "media/banner.png",
            "artifact_path": "gfx/events/germany/banner.png",
            "owner": "collection:germany",
            "target_root": "output",
            "sha256": sha256hash(payload),
            "size": len(payload),
            "source": {
                "path": str(collection_root / "media/banner.png"),
                "collection_id": "germany",
                "family": "event",
                "slot": "media",
            },
        }
    ]


def test_project_diagnostics_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    loc_path = project_root / "src/modules/focus/GER_sample/main.loc"
    loc_path.write_text("en:\n  GER_sample: Sample Focus\n", encoding="utf-8")
    project = Project.load(project_root)

    payload = project.diagnostics(
        severity="error",
        code="focus.missing_localization",
        family="focus",
        module_id="focus/GER_sample",
        source_path="def.txt",
        slot="loc",
    )

    assert payload == {
        "schema": "paradev.build.diagnostics.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "diagnostics": [
            {
                "code": "focus.missing_localization",
                "message": "Focus GER_sample missing localization key 'GER_sample_desc' for l_english.",
                "severity": "error",
                "family": "focus",
                "module_id": "focus/GER_sample",
                "slot": "loc",
                "source_path": "def.txt",
                "span": {"line": 2, "column": 7},
                "source": {
                    "family": "focus",
                    "module_id": "focus/GER_sample",
                    "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
                    "slot": "def",
                },
            }
        ],
        "index": {"error": {"focus.missing_localization": [0]}},
        "family_index": {"focus": {"error": {"focus.missing_localization": [0]}}},
    }
    resolved_payload = project.diagnostics(
        severity="error",
        code="focus.missing_localization",
        family="focus",
        module_id="focus/GER_sample",
        source_path=str(project_root / "src/modules/focus/GER_sample/def.txt"),
        slot="def",
    )
    assert resolved_payload["diagnostics"] == payload["diagnostics"]
    assert resolved_payload["index"] == payload["index"]
    assert resolved_payload["family_index"] == payload["family_index"]
    empty_payload = project.diagnostics(
        severity="error",
        code="focus.missing_localization",
        family="idea",
        module_id="focus/GER_sample",
        source_path="def.txt",
        slot="loc",
    )
    assert empty_payload["diagnostics"] == []
    assert empty_payload["index"] == {}
    assert empty_payload["family_index"] == {}
    assert not project.build_root.exists()


def test_project_diagnostics_reads_validated_published_manifest_without_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)
    project.build(emit_manifests=True)
    manifest_path = project.build_root / "diagnostics.json"
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))

    def fail_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("published diagnostics must not start another build")

    monkeypatch.setattr(Project, "build", fail_build)

    assert project.diagnostics(published=True) == expected
    filtered = project.inspect(
        "diagnostics",
        published=True,
        severity="error",
    )
    assert all(row["severity"] == "error" for row in filtered["diagnostics"])


def test_project_diagnostics_rejects_tampered_published_manifest(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "published_diagnostics", project_id="published_diagnostics")
    project.build(emit_manifests=True)
    manifest_path = project.build_root / "diagnostics.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["index"] = {"error": {"tampered": [0]}}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid severity/code index"):
        project.diagnostics(published=True)


def test_project_diagnostics_filters_slot_collision_by_collided_slot(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "src/modules/badge/GER_badge"
    module_root.mkdir(parents=True)
    (module_root / "main.loc").write_text("en:\n  GER_badge: Badge\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_slot_collision_diagnostics",
                "title: Test Slot Collision Diagnostics",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    registry = BuildRegistry().add(
        SimpleSourceFamily(
            family="badge",
            source_slots=(
                Slot("copy", "*.loc", many=True, kind="copy"),
                Slot("loc", "*.loc", many=True, kind="loc"),
            ),
        )
    )
    project = Project.load(tmp_path)

    payload = project.diagnostics(registry=registry, code="slot.source_collision", slot="loc")
    copy_payload = project.diagnostics(registry=registry, code="slot.source_collision", slot="copy")
    empty_payload = project.diagnostics(registry=registry, code="slot.source_collision", slot="def")

    assert payload["diagnostics"] == [
        {
            "code": "slot.source_collision",
            "message": "Source file main.loc matched multiple slots: copy, loc.",
            "severity": "error",
            "family": "badge",
            "module_id": "badge/GER_badge",
            "slot": "copy",
            "slots": ["copy", "loc"],
            "source_path": "main.loc",
            "source": {
                "path": str(module_root / "main.loc"),
                "module_id": "badge/GER_badge",
                "family": "badge",
                "slot": "copy",
            },
        }
    ]
    assert copy_payload["diagnostics"] == payload["diagnostics"]
    assert empty_payload["diagnostics"] == []
    assert not project.build_root.exists()


def test_project_diagnostics_filters_artifact_collision_by_owner(
    tmp_path: Path,
) -> None:
    badge_root = tmp_path / "src/modules/badge/GER_badge"
    medal_root = tmp_path / "src/modules/medal/GER_medal"
    badge_root.mkdir(parents=True)
    medal_root.mkdir(parents=True)
    (badge_root / "def.txt").write_text("badge = { id = GER_badge }", encoding="utf-8")
    (badge_root / "meta.yaml").write_text("type: badge\n", encoding="utf-8")
    (medal_root / "def.txt").write_text("medal = { id = GER_medal }", encoding="utf-8")
    (medal_root / "meta.yaml").write_text("type: medal\n", encoding="utf-8")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_artifact_collision_owner_diagnostics",
                "title: Test Artifact Collision Owner Diagnostics",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
                "families:",
                "  badge:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/shared/{source_name}",
                "  medal:",
                "    kind: simple_source",
                "    source_slots:",
                "      - name: def",
                "        match: def.txt",
                "        kind: pdx",
                "    templates:",
                "      pdx: common/shared/{source_name}",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)

    badge_payload = project.diagnostics(code="build.artifact_path_collision", owner="module:badge/GER_badge")
    medal_payload = project.diagnostics(code="build.artifact_path_collision", owner="module:medal/GER_medal")
    empty_payload = project.diagnostics(code="build.artifact_path_collision", owner="module:badge/GER_missing")
    output_payload = project.diagnostics(code="build.artifact_path_collision", target_root="output")
    build_payload = project.diagnostics(code="build.artifact_path_collision", target_root="build")
    cli_result = CliRunner().invoke(
        build_app(),
        [
            "diagnostics",
            str(tmp_path),
            "--code",
            "build.artifact_path_collision",
            "--owner",
            "module:badge/GER_badge",
            "--target-root",
            "output",
            "--json",
        ],
    )

    expected = [
        {
            "code": "build.artifact_path_collision",
            "message": "Artifact path common/shared/def.txt under output root is produced by module:badge/GER_badge and module:medal/GER_medal.",
            "severity": "error",
            "artifact_path": "common/shared/def.txt",
            "target_root": "output",
            "owners": ["module:badge/GER_badge", "module:medal/GER_medal"],
        }
    ]
    assert badge_payload["diagnostics"] == expected
    assert medal_payload["diagnostics"] == expected
    assert output_payload["diagnostics"] == expected
    assert badge_payload["index"] == {"error": {"build.artifact_path_collision": [0]}}
    assert badge_payload["family_index"] == {}
    assert empty_payload["diagnostics"] == []
    assert empty_payload["index"] == {}
    assert empty_payload["family_index"] == {}
    assert build_payload["diagnostics"] == []
    assert build_payload["index"] == {}
    assert build_payload["family_index"] == {}
    assert cli_result.exit_code == 0, cli_result.output
    assert json.loads(cli_result.output)["diagnostics"] == expected
    assert not project.build_root.exists()


def test_project_cli_filters_diagnostics_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    loc_path = project_root / "src/modules/focus/GER_sample/main.loc"
    loc_path.write_text("en:\n  GER_sample: Sample Focus\n", encoding="utf-8")

    result = CliRunner().invoke(
        build_app(),
        [
            "diagnostics",
            str(project_root),
            "--severity",
            "error",
            "--code",
            "focus.missing_localization",
            "--family",
            "focus",
            "--module",
            "focus/GER_sample",
            "--source",
            "def.txt",
            "--slot",
            "loc",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.diagnostics.v1"
    assert payload["index"] == {"error": {"focus.missing_localization": [0]}}
    assert payload["family_index"] == {"focus": {"error": {"focus.missing_localization": [0]}}}
    assert [row["code"] for row in payload["diagnostics"]] == ["focus.missing_localization"]
    assert payload["diagnostics"][0]["source"] == {
        "family": "focus",
        "module_id": "focus/GER_sample",
        "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
        "slot": "def",
    }
    resolved_result = CliRunner().invoke(
        build_app(),
        [
            "diagnostics",
            str(project_root),
            "--severity",
            "error",
            "--code",
            "focus.missing_localization",
            "--family",
            "focus",
            "--module",
            "focus/GER_sample",
            "--source",
            str(project_root / "src/modules/focus/GER_sample/def.txt"),
            "--slot",
            "def",
            "--json",
        ],
    )

    assert resolved_result.exit_code == 0, resolved_result.output
    resolved_payload = json.loads(resolved_result.output)
    assert resolved_payload["diagnostics"] == payload["diagnostics"]
    assert resolved_payload["index"] == payload["index"]
    assert resolved_payload["family_index"] == payload["family_index"]
    empty_result = CliRunner().invoke(
        build_app(),
        [
            "diagnostics",
            str(project_root),
            "--severity",
            "error",
            "--code",
            "focus.missing_localization",
            "--family",
            "idea",
            "--module",
            "focus/GER_sample",
            "--source",
            "def.txt",
            "--slot",
            "loc",
            "--json",
        ],
    )

    assert empty_result.exit_code == 0, empty_result.output
    empty_payload = json.loads(empty_result.output)
    assert empty_payload["diagnostics"] == []
    assert empty_payload["index"] == {}
    assert empty_payload["family_index"] == {}


def test_project_cli_filters_collection_diagnostics_manifest_json(
    tmp_path: Path,
) -> None:
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
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_diagnostics",
                "title: Test Collection Diagnostics",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        build_app(),
        [
            "diagnostics",
            str(tmp_path),
            "--collection",
            "GER_industry",
            "--severity",
            "error",
            "--code",
            "decision.category_missing_localization",
            "--source",
            "main.loc",
            "--slot",
            "loc",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["index"] == {"error": {"decision.category_missing_localization": [0]}}
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


def test_project_source_map_returns_filtered_manifest_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.source_map(
        module_id="focus/GER_sample",
        slot="def",
        artifact_type="pdx",
        target_root="output",
    )

    assert payload == {
        "schema": "paradev.build.source-map.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "source_map": [
            {
                "artifact_path": "common/national_focus/GER_main.txt",
                "type": "pdx",
                "target_root": "output",
                "owner": "collection:GER_main",
                "inputs": [str(project_root / "src/modules/focus/GER_sample/def.txt")],
                "sources": [
                    {
                        "family": "focus",
                        "module_id": "focus/GER_sample",
                        "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
                        "slot": "def",
                    }
                ],
            }
        ],
        "index": {"focus/GER_sample": {"def": [0]}},
    }
    assert not project.build_root.exists()


def test_project_sources_returns_filtered_compiler_input_payload_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.sources(module_id="focus/GER_sample", slot="def", loader="pdx", status="loaded")

    assert payload == {
        "schema": "paradev.build.sources.v1",
        "project_id": "minimal_hoi4",
        "profile": "hoi4",
        "sources": [
            {
                "owner_kind": "module",
                "module_id": "focus/GER_sample",
                "family": "focus",
                "root": str(project_root / "src/modules/focus/GER_sample"),
                "slot": "def",
                "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
                "relative_path": "def.txt",
                "loader": "pdx",
                "status": "loaded",
                "entry_count": 1,
            }
        ],
        "index": {"focus/GER_sample": {"def": [0]}},
    }
    assert not project.build_root.exists()


def test_project_sources_filters_collection_descriptor_owner_kind(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.sources(collection_id="germany", owner_kind="collection", loader="loc")

    assert payload == {
        "schema": "paradev.build.sources.v1",
        "project_id": "collection_slot_project",
        "profile": "hoi4",
        "sources": [
            {
                "owner_kind": "collection",
                "collection_id": "germany",
                "family": "bulletin",
                "root": str(tmp_path / "src/collections/bulletin/germany"),
                "slot": "strings",
                "path": str(tmp_path / "src/collections/bulletin/germany/strings.yml"),
                "relative_path": "strings.yml",
                "loader": "loc",
                "status": "loaded",
                "localization_count": 1,
                "languages": ["l_english"],
            }
        ],
        "index": {"germany": {"strings": [0]}},
    }

    assert project.sources(collection_id="germany", owner_kind="module", loader="loc")["sources"] == []
    assert not project.build_root.exists()


def test_project_source_slots_returns_contract_status_payload_without_writing(
    tmp_path: Path,
) -> None:
    _write_required_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.source_slots(family="badge")

    assert payload == {
        "schema": "paradev.build.source-slots.v1",
        "project_id": "required_slot_project",
        "profile": "hoi4",
        "source_slots": [
            {
                "owner_kind": "module",
                "module_id": "badge/GER_missing_icon",
                "family": "badge",
                "root": str(tmp_path / "src/modules/badge/GER_missing_icon"),
                "slot": "body",
                "match": "body.txt",
                "required": True,
                "many": False,
                "regex": False,
                "loader": "pdx",
                "status": "satisfied",
                "source_count": 1,
                "relative_paths": ["body.txt"],
                "paths": [str(tmp_path / "src/modules/badge/GER_missing_icon/body.txt")],
                "suggested_relative_paths": ["body.txt"],
                "suggested_paths": [str(tmp_path / "src/modules/badge/GER_missing_icon/body.txt")],
                "source_statuses": ["loaded"],
            },
            {
                "owner_kind": "module",
                "module_id": "badge/GER_missing_icon",
                "family": "badge",
                "root": str(tmp_path / "src/modules/badge/GER_missing_icon"),
                "slot": "icon",
                "match": "icon.png",
                "required": True,
                "many": False,
                "regex": False,
                "loader": "copy",
                "status": "missing",
                "source_count": 0,
                "relative_paths": [],
                "paths": [],
                "suggested_relative_paths": ["icon.png"],
                "suggested_paths": [str(tmp_path / "src/modules/badge/GER_missing_icon/icon.png")],
                "diagnostic_codes": ["slot.missing_required"],
            },
        ],
        "index": {
            "owner": {"module:badge/GER_missing_icon": {"body": [0], "icon": [1]}},
            "family": {"badge": [0, 1]},
            "slot": {"body": [0], "icon": [1]},
            "status": {"missing": [1], "satisfied": [0]},
        },
    }
    assert not project.build_root.exists()


def test_project_source_slots_filters_collection_descriptor_status(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.source_slots(collection_id="germany", status="satisfied")

    assert payload["schema"] == "paradev.build.source-slots.v1"
    assert payload["index"] == {
        "owner": {"collection:germany": {"category": [0], "strings": [1]}},
        "family": {"bulletin": [0, 1]},
        "slot": {"category": [0], "strings": [1]},
        "status": {"satisfied": [0, 1]},
    }
    assert payload["source_slots"] == [
        {
            "owner_kind": "collection",
            "collection_id": "germany",
            "family": "bulletin",
            "root": str(tmp_path / "src/collections/bulletin/germany"),
            "slot": "category",
            "match": "category.txt",
            "required": False,
            "many": False,
            "regex": False,
            "loader": "pdx",
            "status": "satisfied",
            "source_count": 1,
            "relative_paths": ["category.txt"],
            "paths": [str(tmp_path / "src/collections/bulletin/germany/category.txt")],
            "suggested_relative_paths": ["category.txt"],
            "suggested_paths": [str(tmp_path / "src/collections/bulletin/germany/category.txt")],
            "source_statuses": ["loaded"],
        },
        {
            "owner_kind": "collection",
            "collection_id": "germany",
            "family": "bulletin",
            "root": str(tmp_path / "src/collections/bulletin/germany"),
            "slot": "strings",
            "match": "strings.yml",
            "required": False,
            "many": False,
            "regex": False,
            "loader": "loc",
            "status": "satisfied",
            "source_count": 1,
            "relative_paths": ["strings.yml"],
            "paths": [str(tmp_path / "src/collections/bulletin/germany/strings.yml")],
            "suggested_relative_paths": ["strings.yml"],
            "suggested_paths": [str(tmp_path / "src/collections/bulletin/germany/strings.yml")],
            "source_statuses": ["loaded"],
        },
    ]


def test_project_source_slots_merges_repeated_slot_declarations(tmp_path: Path) -> None:
    _write_repeated_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.source_slots(module_id="notice/ALERT", slot="loc")

    assert payload["index"] == {
        "owner": {"module:notice/ALERT": {"loc": [0]}},
        "family": {"notice": [0]},
        "slot": {"loc": [0]},
        "status": {"satisfied": [0]},
    }
    assert payload["source_slots"] == [
        {
            "owner_kind": "module",
            "module_id": "notice/ALERT",
            "family": "notice",
            "root": str(tmp_path / "src/modules/notice/ALERT"),
            "slot": "loc",
            "match": "main.loc",
            "matches": ["main.loc", "loc/*.loc"],
            "required": False,
            "many": True,
            "regex": False,
            "loader": "loc",
            "status": "satisfied",
            "source_count": 2,
            "relative_paths": ["loc/english.loc", "main.loc"],
            "paths": [
                str(tmp_path / "src/modules/notice/ALERT/loc/english.loc"),
                str(tmp_path / "src/modules/notice/ALERT/main.loc"),
            ],
            "suggested_relative_paths": ["main.loc"],
            "suggested_paths": [str(tmp_path / "src/modules/notice/ALERT/main.loc")],
            "source_statuses": ["loaded"],
        }
    ]


def test_project_cli_filters_source_map_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "source-map",
            str(project_root),
            "--module",
            "focus/GER_sample",
            "--slot",
            "loc",
            "--type",
            "loc",
            "--target-root",
            "output",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.source-map.v1"
    assert payload["index"] == {"focus/GER_sample": {"loc": [0]}}
    assert [row["artifact_path"] for row in payload["source_map"]] == ["localisation/english/GER_sample_l_english.yml"]
    assert payload["source_map"][0]["sources"] == [
        {
            "family": "focus",
            "module_id": "focus/GER_sample",
            "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
            "slot": "loc",
        }
    ]


def test_project_cli_filters_source_slots_json(tmp_path: Path) -> None:
    _write_required_slot_project(tmp_path)

    result = CliRunner().invoke(
        build_app(),
        [
            "source-slots",
            str(tmp_path),
            "--family",
            "badge",
            "--status",
            "missing",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.source-slots.v1"
    assert payload["index"] == {
        "owner": {"module:badge/GER_missing_icon": {"icon": [0]}},
        "family": {"badge": [0]},
        "slot": {"icon": [0]},
        "status": {"missing": [0]},
    }
    assert payload["source_slots"] == [
        {
            "owner_kind": "module",
            "module_id": "badge/GER_missing_icon",
            "family": "badge",
            "root": str(tmp_path / "src/modules/badge/GER_missing_icon"),
            "slot": "icon",
            "match": "icon.png",
            "required": True,
            "many": False,
            "regex": False,
            "loader": "copy",
            "status": "missing",
            "source_count": 0,
            "relative_paths": [],
            "paths": [],
            "suggested_relative_paths": ["icon.png"],
            "suggested_paths": [str(tmp_path / "src/modules/badge/GER_missing_icon/icon.png")],
            "diagnostic_codes": ["slot.missing_required"],
        }
    ]


def test_project_cli_filters_sources_manifest_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "sources",
            str(project_root),
            "--module",
            "focus/GER_sample",
            "--slot",
            "loc",
            "--loader",
            "loc",
            "--status",
            "loaded",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.sources.v1"
    assert payload["index"] == {"focus/GER_sample": {"loc": [0]}}
    assert payload["sources"] == [
        {
            "owner_kind": "module",
            "module_id": "focus/GER_sample",
            "family": "focus",
            "root": str(project_root / "src/modules/focus/GER_sample"),
            "slot": "loc",
            "path": str(project_root / "src/modules/focus/GER_sample/main.loc"),
            "relative_path": "main.loc",
            "loader": "loc",
            "status": "loaded",
            "localization_count": 2,
            "languages": ["l_english"],
        }
    ]


def test_project_cli_filters_collection_source_map_manifest_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_root = tmp_path / "src/modules/event/GER_news"
    collection_root = tmp_path / "src/collections/event/germany"
    module_root.mkdir(parents=True)
    (collection_root / "media").mkdir(parents=True)
    (module_root / "body.txt").write_text("country_event = { id = germany.1 }", encoding="utf-8")
    (module_root / "meta.yaml").write_text("type: event\ncollection: germany\n", encoding="utf-8")
    (collection_root / "media/banner.png").write_bytes(b"collection image")
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: test_collection_source_map",
                "title: Test Collection Source Map",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    registry = BuildRegistry().add(
        CollectionSourceFamily(
            family="event",
            pdx_path_template="events/{collection_id}.txt",
            copy_path_template="gfx/events/{object_id}/{source_name}",
            source_slots=(Slot("body", "body.txt", required=True, kind="pdx"),),
            collection_source_slots=(Slot("media", "media/*", many=True, kind="copy"),),
        )
    )
    monkeypatch.setattr("paradev.games.registry_for_profile", lambda _profile: registry)

    result = CliRunner().invoke(
        build_app(),
        [
            "source-map",
            str(tmp_path),
            "--collection",
            "germany",
            "--type",
            "copy",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["index"] == {"germany": {"media": [0]}}
    assert [row["artifact_path"] for row in payload["source_map"]] == ["gfx/events/germany/banner.png"]
    assert payload["source_map"][0]["sources"] == [
        {
            "path": str(collection_root / "media/banner.png"),
            "collection_id": "germany",
            "family": "event",
            "slot": "media",
        }
    ]


def test_project_build_graph_returns_source_artifact_and_dependency_edges_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.build_graph(module_id="focus/GER_sample")

    def_source = f"source:{project_root / 'src/modules/focus/GER_sample/def.txt'}"
    loc_source = f"source:{project_root / 'src/modules/focus/GER_sample/main.loc'}"
    focus_artifact = "artifact:output:common/national_focus/GER_main.txt"
    view_artifact = "artifact:build:views/focus-tree/GER_main.json"
    loc_artifact = "artifact:output:localisation/english/GER_sample_l_english.yml"
    nodes = {node["id"]: node for node in payload["nodes"]}

    assert payload["schema"] == "paradev.build.graph.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["summary"] == {
        "node_count": 8,
        "edge_count": 5,
        "nodes_by_type": {"artifact": 3, "module": 1, "reference": 2, "source": 2},
        "nodes_by_group": {
            "artifact:build": 1,
            "artifact:output": 2,
            "module:focus": 1,
            "reference:focus": 1,
            "reference:idea": 1,
            "source:focus": 2,
        },
        "edges_by_kind": {"after": 1, "emits": 3, "requires": 1},
    }
    assert nodes["module:focus/GER_sample"] == {
        "id": "module:focus/GER_sample",
        "type": "module",
        "group": "module:focus",
        "label": "focus/GER_sample",
        "display_label": "GER_sample",
        "display_detail": "focus",
        "display_path": "focus/GER_sample",
        "module_id": "focus/GER_sample",
        "family": "focus",
    }
    assert nodes[def_source] == {
        "id": def_source,
        "type": "source",
        "group": "source:focus",
        "label": "def.txt",
        "display_label": "def.txt",
        "display_detail": "focus/GER_sample / def",
        "display_path": "modules/focus/GER_sample/def.txt",
        "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
        "module_id": "focus/GER_sample",
        "family": "focus",
        "slot": "def",
    }
    assert nodes[focus_artifact] == {
        "id": focus_artifact,
        "type": "artifact",
        "group": "artifact:output",
        "label": "common/national_focus/GER_main.txt",
        "display_label": "GER_main.txt",
        "display_detail": "output / common/national_focus",
        "display_path": "common/national_focus/GER_main.txt",
        "path": "common/national_focus/GER_main.txt",
        "artifact_type": "pdx",
        "target_root": "output",
        "owner": "collection:GER_main",
        "module_ids": ["focus/GER_sample"],
        "collection_ids": ["GER_main"],
    }
    assert nodes["reference:idea:GER_industrial_spirit"] == {
        "id": "reference:idea:GER_industrial_spirit",
        "type": "reference",
        "group": "reference:idea",
        "label": "idea:GER_industrial_spirit",
        "display_label": "GER_industrial_spirit",
        "display_detail": "idea",
        "display_path": "idea:GER_industrial_spirit",
        "target": "idea:GER_industrial_spirit",
        "target_kind": "idea",
    }
    assert nodes[view_artifact]["module_ids"] == ["focus/GER_sample"]
    assert nodes[view_artifact]["collection_ids"] == ["GER_main"]
    assert nodes[loc_artifact]["module_ids"] == ["focus/GER_sample"]
    assert "collection_ids" not in nodes[loc_artifact]
    assert payload["index"]["nodes_by_group"]["artifact:output"] == [1, 2]
    assert {
        "source": def_source,
        "target": focus_artifact,
        "kind": "emits",
        "artifact_type": "pdx",
        "target_root": "output",
    } in payload["edges"]
    assert {
        "source": def_source,
        "target": view_artifact,
        "kind": "emits",
        "artifact_type": "view",
        "target_root": "build",
    } in payload["edges"]
    assert {
        "source": loc_source,
        "target": loc_artifact,
        "kind": "emits",
        "artifact_type": "loc",
        "target_root": "output",
    } in payload["edges"]
    assert {
        "source": "module:focus/GER_sample",
        "target": "reference:idea:GER_industrial_spirit",
        "kind": "requires",
    } in payload["edges"]
    assert payload["index"]["edges_by_kind"]["emits"] == [1, 2, 3]
    assert not project.build_root.exists()


def test_project_cli_filters_build_graph_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "build-graph",
            str(project_root),
            "--module",
            "focus/GER_sample",
            "--kind",
            "requires",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.graph.v1"
    assert payload["summary"] == {
        "node_count": 2,
        "edge_count": 1,
        "nodes_by_type": {"module": 1, "reference": 1},
        "nodes_by_group": {"module:focus": 1, "reference:idea": 1},
        "edges_by_kind": {"requires": 1},
    }
    assert payload["edges"] == [
        {
            "source": "module:focus/GER_sample",
            "target": "reference:idea:GER_industrial_spirit",
            "kind": "requires",
        }
    ]


def test_project_build_explain_returns_module_context_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.build_explain(module_id="focus/GER_sample")

    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["project_id"] == "minimal_hoi4"
    assert payload["profile"] == "hoi4"
    assert payload["target"] == {
        "id": "module:focus/GER_sample",
        "type": "module",
        "module_id": "focus/GER_sample",
        "family": "focus",
    }
    assert payload["summary"] == {
        "source_count": 2,
        "artifact_count": 3,
        "dependency_count": 2,
        "diagnostic_count": 0,
        "graph_node_count": 8,
        "graph_edge_count": 5,
        "blocked": False,
    }
    assert [source["slot"] for source in payload["sources"]] == ["def", "loc"]
    assert [artifact["artifact_path"] for artifact in payload["artifacts"]] == [
        "common/national_focus/GER_main.txt",
        "views/focus-tree/GER_main.json",
        "localisation/english/GER_sample_l_english.yml",
    ]
    assert payload["dependencies"] == [
        {
            "source": "module:focus/GER_sample",
            "target": "idea:GER_industrial_spirit",
            "kind": "requires",
        },
        {
            "source": "module:focus/GER_sample",
            "target": "focus:GER_rhineland",
            "kind": "after",
        },
    ]
    assert payload["diagnostics"] == []
    assert payload["graph"]["summary"]["edges_by_kind"] == {
        "after": 1,
        "emits": 3,
        "requires": 1,
    }
    graph_nodes = {node["id"]: node for node in payload["graph"]["nodes"]}
    assert graph_nodes["artifact:output:common/national_focus/GER_main.txt"]["module_ids"] == ["focus/GER_sample"]
    assert graph_nodes["artifact:output:common/national_focus/GER_main.txt"]["collection_ids"] == ["GER_main"]
    assert not project.build_root.exists()


def test_project_cli_build_explain_outputs_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        ["build-explain", str(project_root), "--module", "focus/GER_sample", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"]["module_id"] == "focus/GER_sample"
    assert payload["summary"]["artifact_count"] == 3
    assert payload["summary"]["dependency_count"] == 2


def test_project_build_explain_returns_collection_context_without_writing(
    tmp_path: Path,
) -> None:
    _write_collection_slot_project(tmp_path)
    project = Project.load(tmp_path)

    payload = project.build_explain(collection_id="germany")

    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["project_id"] == "collection_slot_project"
    assert payload["profile"] == "hoi4"
    assert payload["target"] == {
        "id": "collection:germany",
        "type": "collection",
        "collection_id": "germany",
        "family": "bulletin",
        "module_ids": ["bulletin/GER_news"],
        "source_slots": {"category": ["category.txt"], "strings": ["strings.yml"]},
    }
    assert payload["summary"] == {
        "source_count": 2,
        "artifact_count": 1,
        "dependency_count": 0,
        "diagnostic_count": 0,
        "graph_node_count": 3,
        "graph_edge_count": 2,
        "blocked": False,
    }
    assert payload["sources"] == [
        {
            "path": str(tmp_path / "src/collections/bulletin/germany/category.txt"),
            "collection_id": "germany",
            "family": "bulletin",
            "slot": "category",
        },
        {
            "path": str(tmp_path / "src/modules/bulletin/GER_news/body.txt"),
            "module_id": "bulletin/GER_news",
            "family": "bulletin",
            "slot": "body",
        },
    ]
    assert [artifact["artifact_path"] for artifact in payload["artifacts"]] == ["common/bulletins/germany.txt"]
    assert payload["dependencies"] == []
    assert payload["diagnostics"] == []
    assert payload["graph"]["summary"]["edges_by_kind"] == {"emits": 2}
    assert not project.build_root.exists()


def test_project_cli_build_explain_outputs_collection_json(tmp_path: Path) -> None:
    _write_collection_slot_project(tmp_path)

    result = CliRunner().invoke(
        build_app(),
        ["build-explain", str(tmp_path), "--collection", "germany", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"]["id"] == "collection:germany"
    assert payload["target"]["source_slots"] == {
        "category": ["category.txt"],
        "strings": ["strings.yml"],
    }
    assert payload["summary"]["source_count"] == 2
    assert payload["summary"]["graph_edge_count"] == 2


def test_project_build_explain_returns_artifact_context_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)

    payload = project.build_explain(artifact_path="common/national_focus/GER_main.txt", target_root="output")

    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"] == {
        "id": "artifact:output:common/national_focus/GER_main.txt",
        "type": "artifact",
        "artifact_path": "common/national_focus/GER_main.txt",
        "artifact_type": "pdx",
        "target_root": "output",
        "owner": "collection:GER_main",
    }
    assert payload["summary"] == {
        "source_count": 1,
        "artifact_count": 1,
        "dependency_count": 0,
        "diagnostic_count": 0,
        "graph_node_count": 2,
        "graph_edge_count": 1,
        "blocked": False,
    }
    assert payload["sources"] == [
        {
            "path": str(project_root / "src/modules/focus/GER_sample/def.txt"),
            "module_id": "focus/GER_sample",
            "family": "focus",
            "slot": "def",
        }
    ]
    assert [artifact["artifact_path"] for artifact in payload["artifacts"]] == ["common/national_focus/GER_main.txt"]
    assert payload["dependencies"] == []
    assert payload["diagnostics"] == []
    assert payload["graph"]["summary"]["edges_by_kind"] == {"emits": 1}
    assert not project.build_root.exists()


def test_project_cli_build_explain_outputs_artifact_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "build-explain",
            str(project_root),
            "--artifact",
            "common/national_focus/GER_main.txt",
            "--target-root",
            "output",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"]["id"] == "artifact:output:common/national_focus/GER_main.txt"
    assert payload["summary"]["source_count"] == 1
    assert payload["summary"]["graph_edge_count"] == 1


def test_project_build_explain_returns_source_context_without_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    delete_dir(project_root / ".paradev")
    project = Project.load(project_root)
    source_path = "src/modules/focus/GER_sample/def.txt"

    payload = project.build_explain(source_path=source_path)

    source_abs = str(project_root / source_path)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"] == {
        "id": f"source:{source_abs}",
        "type": "source",
        "label": "def.txt",
        "path": source_abs,
        "module_id": "focus/GER_sample",
        "family": "focus",
        "slot": "def",
    }
    assert payload["summary"] == {
        "source_count": 1,
        "artifact_count": 2,
        "dependency_count": 0,
        "diagnostic_count": 0,
        "graph_node_count": 3,
        "graph_edge_count": 2,
        "blocked": False,
    }
    assert payload["sources"] == [
        {
            "path": source_abs,
            "module_id": "focus/GER_sample",
            "family": "focus",
            "slot": "def",
        }
    ]
    assert [artifact["artifact_path"] for artifact in payload["artifacts"]] == [
        "common/national_focus/GER_main.txt",
        "views/focus-tree/GER_main.json",
    ]
    assert payload["dependencies"] == []
    assert payload["diagnostics"] == []
    assert payload["graph"]["summary"]["edges_by_kind"] == {"emits": 2}
    assert not project.build_root.exists()


def test_project_cli_build_explain_outputs_source_json(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(
        build_app(),
        [
            "build-explain",
            str(project_root),
            "--source",
            "src/modules/focus/GER_sample/def.txt",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"]["id"].endswith("/src/modules/focus/GER_sample/def.txt")
    assert payload["summary"]["artifact_count"] == 2
    assert payload["summary"]["graph_edge_count"] == 2


def test_project_build_explain_returns_diagnostic_context_without_writing(
    tmp_path: Path,
) -> None:
    _write_notice_project(tmp_path)
    project = Project.load(tmp_path)
    registry = BuildRegistry(source_slots=(Slot("def", "def.txt", kind="pdx"),)).add(ExplainFailingNormalizeFamily())

    payload = project.build_explain(diagnostic_code="family.normalize_failed", registry=registry)

    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"] == {
        "id": "diagnostic:family.normalize_failed",
        "type": "diagnostic",
        "code": "family.normalize_failed",
    }
    assert payload["summary"] == {
        "source_count": 0,
        "artifact_count": 0,
        "dependency_count": 0,
        "diagnostic_count": 1,
        "graph_node_count": 0,
        "graph_edge_count": 0,
        "blocked": True,
    }
    assert payload["sources"] == []
    assert payload["artifacts"] == []
    assert payload["dependencies"] == []
    assert payload["diagnostics"] == [
        {
            "code": "family.normalize_failed",
            "message": "Build family 'notice' normalize failed: RuntimeError: metadata schema unavailable.",
            "severity": "error",
            "family": "notice",
        }
    ]
    assert payload["graph"]["summary"]["node_count"] == 0
    assert not project.build_root.exists()


def test_project_build_explain_scopes_diagnostic_artifacts_by_target_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "paradev.yaml").write_text(
        "\n".join(
            [
                "project_id: diagnostic_explain",
                "title: Diagnostic Explain",
                "game: hoi4",
                "source_roots: [src]",
                "output_root: build/mod",
                "build_root: .paradev/.cache/build",
            ]
        ),
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    registry = BuildRegistry().add(ExplainArtifactCollisionFamily())

    payload = project.build_explain(diagnostic_code="build.artifact_path_collision", registry=registry)

    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"] == {
        "id": "diagnostic:build.artifact_path_collision",
        "type": "diagnostic",
        "code": "build.artifact_path_collision",
    }
    assert payload["summary"]["artifact_count"] == 2
    assert payload["diagnostics"] == [
        {
            "code": "build.artifact_path_collision",
            "message": "Artifact path views/shared.json under output root is produced by module:collision/output_a and module:collision/output_b.",
            "severity": "error",
            "artifact_path": "views/shared.json",
            "target_root": "output",
            "owners": ["module:collision/output_a", "module:collision/output_b"],
        }
    ]
    assert [artifact["target_root"] for artifact in payload["artifacts"]] == [
        "output",
        "output",
    ]
    assert {artifact["owner"] for artifact in payload["artifacts"]} == {
        "module:collision/output_a",
        "module:collision/output_b",
    }
    assert payload["summary"]["graph_node_count"] == 1
    assert payload["summary"]["graph_edge_count"] == 0
    assert payload["graph"]["summary"] == {
        "node_count": 1,
        "edge_count": 0,
        "nodes_by_type": {"artifact": 1},
        "nodes_by_group": {"artifact:output": 1},
        "edges_by_kind": {},
    }
    assert payload["graph"]["nodes"] == [
        {
            "id": "artifact:output:views/shared.json",
            "type": "artifact",
            "group": "artifact:output",
            "label": "views/shared.json",
            "display_label": "shared.json",
            "display_detail": "output / views",
            "display_path": "views/shared.json",
            "path": "views/shared.json",
            "artifact_type": "view",
            "target_root": "output",
            "owner": "module:collision/output_a",
            "owners": ["module:collision/output_a", "module:collision/output_b"],
            "module_ids": ["collision/output_a", "collision/output_b"],
        }
    ]
    assert not project.build_root.exists()


def test_project_cli_build_explain_outputs_diagnostic_json(tmp_path: Path) -> None:
    _write_notice_python_project(tmp_path)

    result = CliRunner().invoke(
        build_app(),
        [
            "build-explain",
            str(tmp_path),
            "--diagnostic-code",
            "family.normalize_failed",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "paradev.build.explain.v1"
    assert payload["target"] == {
        "id": "diagnostic:family.normalize_failed",
        "type": "diagnostic",
        "code": "family.normalize_failed",
    }
    assert payload["summary"]["blocked"] is True
    assert payload["diagnostics"][0]["code"] == "family.normalize_failed"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"module_id": " "}, "--module must be non-empty"),
        ({"collection_id": " "}, "--collection must be non-empty"),
        ({"source_path": " "}, "--source must be non-empty"),
        ({"artifact_path": " "}, "--artifact must be non-empty"),
        ({"diagnostic_code": " "}, "--diagnostic-code must be non-empty"),
    ],
)
def test_project_build_explain_rejects_empty_targets(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    with pytest.raises(ValueError, match=message):
        project.build_explain(**kwargs)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--module", " "], "--module must be non-empty"),
        (["--collection", " "], "--collection must be non-empty"),
        (["--source", " "], "--source must be non-empty"),
        (["--artifact", " "], "--artifact must be non-empty"),
        (["--diagnostic-code", " "], "--diagnostic-code must be non-empty"),
    ],
)
def test_project_cli_build_explain_rejects_empty_targets(tmp_path: Path, args: list[str], message: str) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    result = CliRunner().invoke(build_app(), ["build-explain", str(project_root), *args, "--json"])

    assert result.exit_code == 2
    assert message in result.output


def test_project_build_explain_reports_unknown_source_with_requested_path(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)
    project = Project.load(project_root)

    with pytest.raises(
        ValueError,
        match=r"Unknown build source: src/modules/focus/GER_sample/missing\.pdx",
    ):
        project.build_explain(source_path="src/modules/focus/GER_sample/missing.pdx")


def test_project_cli_build_explain_reports_unknown_targets(tmp_path: Path) -> None:
    project_root = tmp_path / "minimal"
    copy_dir(PROJECT_ROOT, project_root)

    cases = [
        (["--module", "focus/NOPE"], ("Unknown build module:", "focus/NOPE")),
        (["--collection", "NOPE"], ("Unknown build collection:", "NOPE")),
        (
            ["--source", "src/modules/focus/GER_sample/missing.pdx"],
            ("Unknown build source:", "src/modules/focus/GER_sample/missing.pdx"),
        ),
        (
            ["--artifact", "common/national_focus/NOPE.txt"],
            ("Unknown build artifact:", "common/national_focus/NOPE.txt"),
        ),
        (
            ["--diagnostic-code", "family.nope"],
            ("Unknown build diagnostic code:", "family.nope"),
        ),
    ]
    for args, message_parts in cases:
        result = CliRunner().invoke(build_app(), ["build-explain", str(project_root), *args, "--json"])

        assert result.exit_code == 2
        for message in message_parts:
            assert message in result.output
