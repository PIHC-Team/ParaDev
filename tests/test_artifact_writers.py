from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat

import pytest
from heavenbase.utils import sha256hash

import paradev.build._fs as build_fs
import paradev.build.artifacts as build_artifacts
from paradev.build import (
    Artifact,
    BuildRegistry,
    BuildResult,
    Diagnostic,
    JsonViewWriter,
    LocalizationEntry,
    LocalizationYMLWriter,
    PDXTextWriter,
    SpriteGFXWriter,
    SpriteType,
    StaticCopyWriter,
    write_artifacts,
)
from paradev.pdx import PDXBlock


class _RaceWriter:
    artifact_type = "race"

    def write(self, artifact: Artifact, output_root: Path) -> Path:
        target = output_root / str(artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"staged")
        return target


class _RenderedWriter:
    artifact_type = "rendered"

    def __init__(self) -> None:
        self.render_calls = 0
        self.write_calls = 0

    def render_bytes(self, artifact: Artifact) -> object:
        self.render_calls += 1
        return artifact.payload

    def write(self, artifact: Artifact, output_root: Path) -> Path:
        self.write_calls += 1
        target = output_root / str(artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.payload)
        return target


class _OptionalRenderedWriter:
    artifact_type = "optional_rendered"

    def __init__(self) -> None:
        self.render_calls = 0
        self.write_calls = 0

    def render_bytes(self, _artifact: Artifact) -> None:
        self.render_calls += 1
        return None

    def write(self, artifact: Artifact, output_root: Path) -> Path:
        self.write_calls += 1
        target = output_root / str(artifact.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"staged fallback")
        return target


def test_rendered_writer_publishes_and_confirms_exact_bytes_without_staging(
    tmp_path: Path,
) -> None:
    writer = _RenderedWriter()
    artifact = Artifact(
        path="localisation/english/rendered_l_english.yml",
        artifact_type="rendered",
        owner="module:localization/rendered",
        payload=b"exact bytes",
    )
    registry = BuildRegistry().add(writer)
    result = BuildResult.plan(project_id="rendered", artifacts=(artifact,))
    confirmed: list[tuple[Artifact, Path]] = []

    first = write_artifacts(
        result,
        registry,
        tmp_path / "out",
        on_written=lambda item, path: confirmed.append((item, path)),
    )
    second = write_artifacts(
        result,
        registry,
        tmp_path / "out",
        on_written=lambda item, path: confirmed.append((item, path)),
    )

    target = tmp_path / "out/localisation/english/rendered_l_english.yml"
    assert first == second == {str(artifact.path): target}
    assert target.read_bytes() == b"exact bytes"
    assert writer.render_calls == 2
    assert writer.write_calls == 0
    assert confirmed == [(artifact, target), (artifact, target)]


def test_rendered_writer_uses_staging_when_a_transform_is_required(
    tmp_path: Path,
) -> None:
    writer = _RenderedWriter()
    artifact = Artifact(
        path="rendered.bin",
        artifact_type="rendered",
        owner="module:rendered/sample",
        payload=b"before",
    )
    registry = BuildRegistry().add(writer)
    result = BuildResult.plan(project_id="rendered", artifacts=(artifact,))

    write_artifacts(
        result,
        registry,
        tmp_path / "out",
        stage_transform=lambda _artifact, path: path.write_bytes(b"after"),
    )

    assert (tmp_path / "out/rendered.bin").read_bytes() == b"after"
    assert writer.render_calls == 0
    assert writer.write_calls == 1


def test_rendered_writer_none_requests_staging_for_one_artifact(tmp_path: Path) -> None:
    writer = _OptionalRenderedWriter()
    artifact = Artifact(
        path="optional.bin",
        artifact_type="optional_rendered",
        owner="module:optional/sample",
    )
    registry = BuildRegistry().add(writer)

    write_artifacts(
        BuildResult.plan(project_id="optional", artifacts=(artifact,)),
        registry,
        tmp_path / "out",
    )

    assert (tmp_path / "out/optional.bin").read_bytes() == b"staged fallback"
    assert writer.render_calls == 1
    assert writer.write_calls == 1


def test_stage_transform_predicate_preserves_direct_writes_for_unaffected_artifacts(
    tmp_path: Path,
) -> None:
    writer = _RenderedWriter()
    direct = Artifact(
        path="direct.bin",
        artifact_type="rendered",
        owner="module:rendered/direct",
        payload=b"direct",
    )
    transformed = Artifact(
        path="transformed.bin",
        artifact_type="rendered",
        owner="module:rendered/transformed",
        payload=b"before",
    )
    registry = BuildRegistry().add(writer)

    write_artifacts(
        BuildResult.plan(project_id="rendered", artifacts=(direct, transformed)),
        registry,
        tmp_path / "out",
        stage_transform=lambda _artifact, path: path.write_bytes(b"after"),
        stage_transform_required=lambda artifact: artifact.path == transformed.path,
    )

    assert (tmp_path / "out/direct.bin").read_bytes() == b"direct"
    assert (tmp_path / "out/transformed.bin").read_bytes() == b"after"
    assert writer.render_calls == 1
    assert writer.write_calls == 1


def test_rendered_writer_rejects_a_non_byte_result(tmp_path: Path) -> None:
    writer = _RenderedWriter()
    artifact = Artifact(
        path="rendered.bin",
        artifact_type="rendered",
        owner="module:rendered/sample",
        payload="not bytes",
    )
    registry = BuildRegistry().add(writer)
    result = BuildResult.plan(project_id="rendered", artifacts=(artifact,))

    with pytest.raises(ValueError, match="render_bytes\\(artifact\\) must return bytes"):
        write_artifacts(result, registry, tmp_path / "out")

    assert not (tmp_path / "out/rendered.bin").exists()
    assert writer.render_calls == 1
    assert writer.write_calls == 0


def test_pdx_text_writer_emits_pdx_block_payload_without_manifest_payload(tmp_path: Path) -> None:
    block = PDXBlock.from_str("focus = { id = GER_sample }")
    artifact = Artifact(
        path="common/national_focus/GER_sample.txt",
        artifact_type="pdx",
        owner="module:focus/GER_sample",
        payload=block,
    )
    registry = BuildRegistry().add(PDXTextWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    written = write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/common/national_focus/GER_sample.txt"
    assert written == {"common/national_focus/GER_sample.txt": target}
    assert target.read_text(encoding="utf-8") == block.to_str()
    assert "payload" not in artifact.to_dict()


def test_static_copy_writer_preserves_bytes_and_hash_metadata(tmp_path: Path) -> None:
    payload = b"sample image bytes"
    source = tmp_path / "modules/focus/GER_sample/icon.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/interface/goals/GER_sample.png",
        artifact_type="copy",
        owner="module:focus/GER_sample",
        inputs=(source,),
        metadata={
            "sha256": sha256hash(payload),
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
    )
    registry = BuildRegistry().add(StaticCopyWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    written = write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/gfx/interface/goals/GER_sample.png"
    assert written == {"gfx/interface/goals/GER_sample.png": target}
    assert target.read_bytes() == payload
    assert artifact.to_dict()["metadata"] == {
        "sha256": sha256hash(payload),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "size": 18,
    }


def test_hashed_static_copy_publishes_directly_without_staging_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"bounded direct copy"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    if os.name != "nt":
        source.chmod(0o750)
    artifact = Artifact(
        path="gfx/direct.bin",
        artifact_type="copy",
        owner="module:copy/direct",
        inputs=(source,),
        metadata={
            "sha256": sha256hash(payload),
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
    )

    def reject_copy(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("bounded hashed copy used private staging")

    monkeypatch.setattr(build_artifacts, "copy_file", reject_copy)
    write_artifacts(
        BuildResult.plan(project_id="copy", artifacts=(artifact,)),
        BuildRegistry().add(StaticCopyWriter()),
        tmp_path / "out",
    )

    target = tmp_path / "out/gfx/direct.bin"
    assert target.read_bytes() == payload
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_unchanged_hashed_static_copy_uses_target_digest_without_rereading_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"exact cached copy"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/cached.bin",
        artifact_type="copy",
        owner="module:copy/cached",
        inputs=(source,),
        metadata={
            "sha256": sha256hash(payload),
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
    )
    result = BuildResult.plan(project_id="copy", artifacts=(artifact,))
    registry = BuildRegistry().add(StaticCopyWriter())
    source_reads = 0
    original_read_bytes = Path.read_bytes

    def record_read_bytes(path: Path) -> bytes:
        nonlocal source_reads
        if path == source:
            source_reads += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", record_read_bytes)
    write_artifacts(result, registry, tmp_path / "out")
    write_artifacts(result, registry, tmp_path / "out")

    assert source_reads == 1
    target = tmp_path / "out/gfx/cached.bin"
    target.write_bytes(b"externally changed")
    write_artifacts(result, registry, tmp_path / "out")
    assert source_reads == 2
    assert target.read_bytes() == payload


def test_large_hashed_static_copy_keeps_bounded_staging_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"larger than synthetic direct limit"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/large.bin",
        artifact_type="copy",
        owner="module:copy/large",
        inputs=(source,),
        metadata={"sha256": sha256hash(payload), "size": len(payload)},
    )
    calls = 0
    original_copy = build_artifacts.copy_file

    def record_copy(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(build_artifacts, "_MAX_DIRECT_COPY_BYTES", 1)
    monkeypatch.setattr(build_artifacts, "copy_file", record_copy)
    write_artifacts(
        BuildResult.plan(project_id="copy", artifacts=(artifact,)),
        BuildRegistry().add(StaticCopyWriter()),
        tmp_path / "out",
    )

    assert calls == 1
    assert (tmp_path / "out/gfx/large.bin").read_bytes() == payload


def test_hashed_static_copy_rejects_source_content_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"changed")
    artifact = Artifact(
        path="gfx/drift.bin",
        artifact_type="copy",
        owner="module:copy/drift",
        inputs=(source,),
        metadata={"sha256": sha256hash(b"planned"), "size": len(b"planned")},
    )

    with pytest.raises(ValueError, match="hash does not match metadata"):
        write_artifacts(
            BuildResult.plan(project_id="copy", artifacts=(artifact,)),
            BuildRegistry().add(StaticCopyWriter()),
            tmp_path / "out",
        )


def test_static_copy_rejects_invalid_raw_content_digest(tmp_path: Path) -> None:
    payload = b"copy"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/invalid-digest.bin",
        artifact_type="copy",
        owner="module:copy/invalid-digest",
        inputs=(source,),
        metadata={
            "sha256": sha256hash(payload),
            "content_sha256": "not-a-sha256",
            "size": len(payload),
        },
    )

    with pytest.raises(ValueError, match="expected_sha256.*must return one SHA-256"):
        write_artifacts(
            BuildResult.plan(project_id="copy", artifacts=(artifact,)),
            BuildRegistry().add(StaticCopyWriter()),
            tmp_path / "out",
        )


def test_static_copy_writer_reports_unreadable_hashed_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"sample image bytes"
    source = tmp_path / "modules/focus/GER_sample/icon.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/interface/goals/GER_sample.png",
        artifact_type="copy",
        owner="module:focus/GER_sample",
        inputs=(source,),
        metadata={"sha256": sha256hash(payload), "size": len(payload)},
    )
    registry = BuildRegistry().add(StaticCopyWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == source:
            raise OSError("permission denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(ValueError) as error:
        write_artifacts(result, registry, tmp_path / "out")

    message = str(error.value)
    assert f"Static copy artifact {artifact.path}" in message
    assert str(source) in message
    assert "cannot be read" in message
    assert "permission denied" in message


def test_static_copy_writer_reports_copy_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"sample image bytes"
    source = tmp_path / "modules/focus/GER_sample/icon.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    artifact = Artifact(
        path="gfx/interface/goals/GER_sample.png",
        artifact_type="copy",
        owner="module:focus/GER_sample",
        inputs=(source,),
        metadata={"size": len(payload)},
    )
    registry = BuildRegistry().add(StaticCopyWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    def copy_file(*_args: object, **_kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(build_artifacts, "copy_file", copy_file)

    with pytest.raises(ValueError) as error:
        write_artifacts(result, registry, tmp_path / "out")

    message = str(error.value)
    assert f"Static copy artifact {artifact.path}" in message
    assert str(source) in message
    assert "cannot be copied" in message
    assert "disk full" in message


def test_localization_writer_escapes_quotes_and_backslashes(tmp_path: Path) -> None:
    artifact = Artifact(
        path="localisation/english/GER_sample_l_english.yml",
        artifact_type="loc",
        owner="module:focus/GER_sample",
        payload=(LocalizationEntry(key="GER_sample", language="l_english", text='A "quoted" path C:\\hoi4', source_path="main.loc"),),
    )
    registry = BuildRegistry().add(LocalizationYMLWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/localisation/english/GER_sample_l_english.yml"
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert target.read_text(encoding="utf-8-sig") == ('l_english:\n GER_sample:0 "A \\"quoted\\" path C:\\\\hoi4"\n')


def test_localization_writer_escapes_line_breaks(tmp_path: Path) -> None:
    artifact = Artifact(
        path="localisation/english/GER_sample_l_english.yml",
        artifact_type="loc",
        owner="module:focus/GER_sample",
        payload=(
            LocalizationEntry(
                key="GER_sample_desc",
                language="l_english",
                text="First line\nSecond line\r\nThird line",
                source_path="main.loc",
            ),
        ),
    )
    registry = BuildRegistry().add(LocalizationYMLWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/localisation/english/GER_sample_l_english.yml"
    assert target.read_text(encoding="utf-8-sig") == ('l_english:\n GER_sample_desc:0 "First line\\nSecond line\\nThird line"\n')


def test_localization_writer_preserves_existing_hoi4_line_break_escapes(tmp_path: Path) -> None:
    artifact = Artifact(
        path="localisation/english/GER_sample_l_english.yml",
        artifact_type="loc",
        owner="module:focus/GER_sample",
        payload=(
            LocalizationEntry(
                key="GER_sample_desc",
                language="l_english",
                text="First line\\nSecond line\\tIndented",
                source_path="main.loc",
            ),
        ),
    )
    registry = BuildRegistry().add(LocalizationYMLWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/localisation/english/GER_sample_l_english.yml"
    assert target.read_text(encoding="utf-8-sig") == ('l_english:\n GER_sample_desc:0 "First line\\nSecond line\\tIndented"\n')


def test_json_view_writer_emits_metadata_payload(tmp_path: Path) -> None:
    artifact = Artifact(
        path="views/focus-tree/GER_main.json",
        artifact_type="view",
        owner="collection:GER_main",
        metadata={
            "schema": "focus-tree.view.v1",
            "collection_id": "GER_main",
            "nodes": [{"focus_id": "GER_sample", "order": 0}],
        },
    )
    registry = BuildRegistry().add(JsonViewWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    written = write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/views/focus-tree/GER_main.json"
    assert written == {"views/focus-tree/GER_main.json": target}
    assert json.loads(target.read_text(encoding="utf-8")) == artifact.metadata


def test_sprite_gfx_writer_emits_deterministic_sprite_types(tmp_path: Path) -> None:
    artifact = Artifact(
        path="interface/paradev_ideas.gfx",
        artifact_type="sprite_gfx",
        owner="project:minimal_hoi4",
        payload=(
            SpriteType(name="GFX_idea_beta", texturefile="gfx/interface/ideas/idea_beta.dds", properties={"noOfFrames": 2}),
            SpriteType(name="GFX_idea_alpha", texturefile="gfx/interface/ideas/idea_alpha.dds"),
        ),
    )
    registry = BuildRegistry().add(SpriteGFXWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    written = write_artifacts(result, registry, tmp_path / "out")

    target = tmp_path / "out/interface/paradev_ideas.gfx"
    assert written == {"interface/paradev_ideas.gfx": target}
    assert target.read_text(encoding="utf-8") == (
        "spriteTypes = {\n"
        "\tSpriteType = {\n"
        '\t\tname = "GFX_idea_alpha"\n'
        '\t\ttexturefile = "gfx/interface/ideas/idea_alpha.dds"\n'
        "\t}\n"
        "\tSpriteType = {\n"
        '\t\tname = "GFX_idea_beta"\n'
        '\t\ttexturefile = "gfx/interface/ideas/idea_beta.dds"\n'
        "\t\tnoOfFrames = 2\n"
        "\t}\n"
        "}\n"
    )


def test_sprite_gfx_writer_rejects_invalid_payload(tmp_path: Path) -> None:
    artifact = Artifact(
        path="interface/paradev_empty.gfx",
        artifact_type="sprite_gfx",
        owner="project:minimal_hoi4",
        payload=(),
    )
    registry = BuildRegistry().add(SpriteGFXWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    with pytest.raises(ValueError, match="at least one SpriteType"):
        write_artifacts(result, registry, tmp_path / "out")


def test_write_artifacts_rejects_mixed_target_roots(tmp_path: Path) -> None:
    output_artifact = Artifact(
        path="common/national_focus/GER_main.txt",
        artifact_type="pdx",
        owner="collection:GER_main",
        payload="focus = { id = GER_sample }",
    )
    build_artifact = Artifact(
        path="debug/focus/GER_main.txt",
        artifact_type="pdx",
        owner="collection:GER_main",
        target_root="build",
        payload="focus debug",
    )
    registry = BuildRegistry().add(PDXTextWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(output_artifact, build_artifact))

    with pytest.raises(ValueError, match="single target root"):
        write_artifacts(result, registry, tmp_path / "out")


def test_write_artifacts_rejects_blocked_build_results(tmp_path: Path) -> None:
    artifact = Artifact(
        path="common/national_focus/GER_sample.txt",
        artifact_type="pdx",
        owner="module:focus/GER_sample",
        payload="focus = { id = GER_sample }",
    )
    registry = BuildRegistry().add(PDXTextWriter())
    result = BuildResult.plan(
        project_id="minimal_hoi4",
        artifacts=(artifact,),
        diagnostics=(Diagnostic(code="test.blocked", message="Blocked build."),),
    )

    with pytest.raises(ValueError, match="blocking diagnostics"):
        write_artifacts(result, registry, tmp_path / "out")
    assert not (tmp_path / "out/common/national_focus/GER_sample.txt").exists()


def test_write_artifacts_reuses_one_staging_root_and_coalesces_progress(
    tmp_path: Path,
) -> None:
    staging_roots: set[Path] = set()
    reused_parent_observations: list[bool] = []

    class RecordingWriter:
        artifact_type = "recording"

        def write(self, artifact: Artifact, output_root: Path) -> Path:
            staging_roots.add(output_root)
            assert not any(path.is_file() for path in output_root.rglob("*"))
            target = output_root / str(artifact.path)
            reused_parent_observations.append(target.parent.is_dir())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(artifact.payload), encoding="utf-8")
            return target

    artifacts = tuple(
        Artifact(
            path=f"generated/{index:03d}.txt",
            artifact_type="recording",
            owner=f"module:test/{index:03d}",
            payload=f"artifact {index}",
        )
        for index in range(101)
    )
    result = BuildResult.plan(project_id="staging_batch", artifacts=artifacts)
    events: list[dict[str, object]] = []

    written = write_artifacts(
        result,
        BuildRegistry().add(RecordingWriter()),
        tmp_path / "out",
        progress=events.append,
    )

    assert len(staging_roots) == 1
    assert reused_parent_observations == [False, *([True] * (len(artifacts) - 1))]
    assert len(written) == len(artifacts)
    assert (tmp_path / "out/generated/100.txt").read_text(encoding="utf-8") == "artifact 100"
    assert [event["index"] for event in events][0] == 1
    assert [event["index"] for event in events][-1] == len(artifacts)
    percents = [event["percent"] for event in events]
    assert percents == sorted(percents)
    assert len(percents) - len(set(percents)) <= 1
    assert len(events) <= 2 + 90 - 76


def test_write_artifacts_rejects_paths_that_escape_output_root(tmp_path: Path) -> None:
    artifact = Artifact(
        path="../escaped.txt",
        artifact_type="pdx",
        owner="module:focus/GER_sample",
        payload="focus = { id = GER_sample }",
    )
    registry = BuildRegistry().add(PDXTextWriter())
    result = BuildResult(project_id="minimal_hoi4", artifacts=(artifact,))

    with pytest.raises(ValueError, match="Artifact path must stay under output root"):
        write_artifacts(result, registry, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_localization_writer_rejects_mixed_language_payloads(tmp_path: Path) -> None:
    artifact = Artifact(
        path="localisation/english/GER_sample_l_english.yml",
        artifact_type="loc",
        owner="module:focus/GER_sample",
        payload=(
            LocalizationEntry(key="GER_sample", language="l_english", text="Sample", source_path="main.loc"),
            LocalizationEntry(key="GER_sample", language="l_french", text="Exemple", source_path="extra.loc"),
        ),
    )
    registry = BuildRegistry().add(LocalizationYMLWriter())
    result = BuildResult.plan(project_id="minimal_hoi4", artifacts=(artifact,))

    with pytest.raises(ValueError, match="same language"):
        write_artifacts(result, registry, tmp_path / "out")


def test_write_artifacts_rejects_a_staged_file_swapped_to_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = Artifact(
        path="generated/race.txt",
        artifact_type="race",
        owner="module:test/RACE",
    )
    result = BuildResult.plan(project_id="staging_race", artifacts=(artifact,))
    registry = BuildRegistry().add(_RaceWriter())
    user_file = tmp_path / "user.txt"
    user_file.write_text("safe", encoding="utf-8")
    original_open = build_fs.os.open
    swapped = False

    def swap_before_staged_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        candidate = Path(path) if isinstance(path, (str, os.PathLike)) else None
        if candidate is not None and dir_fd is None and "paradev-artifacts-" in str(candidate) and candidate.name == "race.txt" and not swapped:
            candidate.unlink()
            candidate.symlink_to(user_file)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(build_fs.os, "open", swap_before_staged_open)

    with pytest.raises(ValueError, match="Staged artifact cannot be opened"):
        write_artifacts(result, registry, tmp_path / "out")

    assert swapped is True
    assert user_file.read_text(encoding="utf-8") == "safe"
    assert not (tmp_path / "out/generated/race.txt").exists()
