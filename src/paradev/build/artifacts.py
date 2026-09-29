"""Artifact writer helpers for build outputs."""

from __future__ import annotations

import stat
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from heavenbase.utils import copy_file, save_json, sha256hash

from paradev.pdx import PDXBlock, PDXEntry
from paradev.portable_paths import windows_portable_component_error

from ._fs import AnchoredDirectory, open_anchored_directory
from .loaders import LocalizationEntry
from .progress import BuildProgressCallback, emit_progress, scaled_percent
from .records import Artifact, BuildResult
from .registry import BuildRegistry

_MAX_DIRECT_COPY_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SpriteType:
    """One interface sprite declaration."""

    name: str
    texturefile: str
    properties: Mapping[str, Any] = field(default_factory=dict)


class PDXTextWriter:
    """Write PDX text artifacts from `PDXBlock` payloads."""

    artifact_type = "pdx"

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Write one PDX artifact under an output root."""

        target = _artifact_target(output_root, artifact)
        payload = artifact.payload
        if isinstance(payload, PDXBlock):
            payload.to_file(target)
            return target
        if isinstance(payload, str):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            return target
        raise ValueError("PDX artifacts must provide a PDXBlock or text payload.")


class StaticCopyWriter:
    """Copy static artifact inputs into the output tree."""

    artifact_type = "copy"

    def expected_sha256(self, artifact: Artifact) -> object:
        """Return the source snapshot digest used for unchanged comparison."""

        return artifact.metadata.get("content_sha256")

    def render_bytes(self, artifact: Artifact) -> bytes | None:
        """Return bounded exact source bytes, or request ordinary staging."""

        if artifact.metadata.get("sha256") is None:
            return None
        source = _static_copy_source(artifact)
        try:
            if source.stat().st_size > _MAX_DIRECT_COPY_BYTES:
                return None
        except OSError as error:
            raise ValueError(f"Static copy artifact {artifact.path} source {source} cannot be read. " f"{_os_error_text(error)}.") from error
        return _static_copy_payload(artifact, source)

    def render_mode(self, artifact: Artifact) -> int:
        """Return source permission bits for direct publication."""

        source = _static_copy_source(artifact)
        try:
            return stat.S_IMODE(source.stat().st_mode)
        except OSError as error:
            raise ValueError(f"Static copy artifact {artifact.path} source {source} cannot be read. " f"{_os_error_text(error)}.") from error

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Copy the first artifact input to the artifact path."""

        source = _static_copy_source(artifact)
        if artifact.metadata.get("sha256") is not None:
            _static_copy_payload(artifact, source)
        target = _artifact_target(output_root, artifact)
        try:
            return Path(copy_file(source, target, mode="replace"))
        except OSError as error:
            raise ValueError(f"Static copy artifact {artifact.path} source {source} cannot be copied to {target}. {_os_error_text(error)}.") from error


class LocalizationYMLWriter:
    """Write localization YML artifacts from localization entry payloads."""

    artifact_type = "loc"

    def render_bytes(self, artifact: Artifact) -> bytes:
        """Render one localization document with its UTF-8 BOM.

        Args:
            artifact (Artifact): Localization artifact containing one language.

        Returns:
            bytes: Engine-ready localization, including the UTF-8 BOM.
        """

        return _loc_yml(_loc_entries(artifact)).encode("utf-8-sig")

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Write one localization artifact under an output root."""

        target = _artifact_target(output_root, artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.render_bytes(artifact))
        return target


class JsonViewWriter:
    """Write JSON view artifacts from artifact payload or metadata."""

    artifact_type = "view"

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Write one JSON view artifact under an output root."""

        target = _artifact_target(output_root, artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        save_json(_view_payload(artifact), str(target), sort_keys=True, indent=2)
        return target


class SpriteGFXWriter:
    """Write interface sprite declaration GFX artifacts."""

    artifact_type = "sprite_gfx"

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Write one sprite declaration artifact under an output root."""

        sprites = _sprite_types(artifact)
        target = _artifact_target(output_root, artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_sprite_gfx(sprites), encoding="utf-8")
        return target


class ModDescriptorWriter:
    """Write HOI4 mod descriptor and launcher preview artifacts."""

    artifact_type = "mod_descriptor"

    def write(self, artifact: Artifact, output_root: str | Path) -> Path:
        """Write one `.mod` descriptor artifact under an output root."""

        target = _artifact_target(output_root, artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_mod_descriptor(_descriptor_fields(artifact)), encoding="utf-8")
        return target


def write_artifacts(
    result: BuildResult,
    registry: BuildRegistry,
    output_root: str | Path,
    *,
    progress: BuildProgressCallback | None = None,
    progress_start: int = 76,
    progress_end: int = 90,
    on_written: Callable[[Artifact, Path], None] | None = None,
    stage_transform: Callable[[Artifact, Path], None] | None = None,
    stage_transform_required: Callable[[Artifact], bool] | None = None,
) -> dict[str, Path]:
    """Write build artifacts with registered artifact writers.

    Args:
        result: Build result containing planned artifacts.
        registry: Registry with matching artifact writers.
        output_root: Game-ready output root.
        progress: Optional callback for JSON-safe build progress events.
        progress_start: Percent to use before artifact writing starts.
        progress_end: Percent to use when this artifact batch is complete.
        on_written: Optional callback invoked after each artifact writer
            successfully returns its output path.
        stage_transform: Optional callback that converts a privately staged
            writer output into its final publishable form before comparison.
        stage_transform_required: Optional predicate limiting private staging
            to artifacts the transform can change. Omit it when every artifact
            requires the transform.

    Returns:
        Mapping from artifact path to written filesystem path.
    """

    _validate_artifact_batch(result)
    with open_anchored_directory(Path(output_root)) as publication_root:
        return _write_artifacts_anchored(
            result,
            registry,
            publication_root,
            progress=progress,
            progress_start=progress_start,
            progress_end=progress_end,
            on_written=on_written,
            stage_transform=stage_transform,
            stage_transform_required=stage_transform_required,
        )


def _write_artifacts_anchored(
    result: BuildResult,
    registry: BuildRegistry,
    publication_root: AnchoredDirectory,
    *,
    progress: BuildProgressCallback | None = None,
    progress_start: int = 76,
    progress_end: int = 90,
    on_written: Callable[[Artifact, Path], None] | None = None,
    replace_paths: Set[str] | None = None,
    adopt_paths: Set[str] | None = None,
    rename_paths: Mapping[str, str] | None = None,
    stage_transform: Callable[[Artifact, Path], None] | None = None,
    stage_transform_required: Callable[[Artifact], bool] | None = None,
) -> dict[str, Path]:
    """Write one artifact batch through a retained publication authority."""

    _validate_artifact_batch(result)
    written: dict[str, Path] = {}
    total = len(result.artifacts)
    if total == 0:
        return written
    with TemporaryDirectory(prefix="paradev-artifacts-") as temp_dir:
        staging_root = Path(temp_dir).resolve(strict=True)
        with open_anchored_directory(staging_root, create=False) as staging_publication:
            for index, artifact in enumerate(result.artifacts, start=1):
                writer = registry.writer(artifact.artifact_type)
                write = getattr(writer, "write", None)
                if not callable(write):
                    raise ValueError(f"Artifact writer {artifact.artifact_type!r} must define write(artifact, output_root).")
                artifact_path = _artifact_path(artifact)
                requires_transform = stage_transform is not None and (stage_transform_required is None or stage_transform_required(artifact))
                previous_spelling = rename_paths.get(artifact_path) if rename_paths is not None else None
                replace_existing = replace_paths is None or artifact_path in replace_paths
                unchanged = False
                expected: Path | None = None
                expected_sha256 = _writer_expected_sha256(
                    writer,
                    artifact,
                    enabled=not requires_transform,
                )
                if expected_sha256 is not None:
                    if replace_existing:
                        try:
                            comparison = publication_root.file_sha256_matches(
                                artifact_path,
                                expected_sha256,
                            )
                        except ValueError:
                            comparison = None
                        if comparison is None:
                            replace_existing = False
                        elif comparison:
                            unchanged = previous_spelling is None
                    elif adopt_paths is not None and artifact_path in adopt_paths:
                        try:
                            comparison = publication_root.file_sha256_matches(
                                artifact_path,
                                expected_sha256,
                            )
                        except ValueError as error:
                            raise ValueError(f"Cached build cannot recover non-regular untracked artifact path: {artifact_path}.") from error
                        if comparison is not None:
                            if not comparison:
                                raise ValueError("Cached build refuses to adopt an untracked artifact whose content differs: " f"{artifact_path}.")
                            unchanged = True
                if unchanged:
                    final_path = publication_root.requested_path / artifact_path
                else:
                    render_bytes = getattr(writer, "render_bytes", None)
                    direct_bytes = not requires_transform and callable(render_bytes)
                    rendered = render_bytes(artifact) if direct_bytes else None
                    if rendered is None:
                        direct_bytes = False
                    elif direct_bytes and not isinstance(rendered, bytes):
                        raise ValueError(f"Artifact writer {artifact.artifact_type!r} render_bytes(artifact) must return bytes.")
                    rendered_mode = 0o644
                    if direct_bytes:
                        render_mode = getattr(writer, "render_mode", None)
                        if callable(render_mode):
                            rendered_mode = render_mode(artifact)
                            if not isinstance(rendered_mode, int) or isinstance(rendered_mode, bool) or not 0 <= rendered_mode <= 0o7777:
                                raise ValueError(f"Artifact writer {artifact.artifact_type!r} render_mode(artifact) " "must return permission bits.")
                    if direct_bytes:
                        if replace_existing and expected_sha256 is None:
                            try:
                                current = publication_root.read_bytes(artifact_path)
                            except ValueError:
                                current = None
                            if current is None:
                                replace_existing = False
                            else:
                                unchanged = current == rendered and previous_spelling is None
                        elif expected_sha256 is None and adopt_paths is not None and artifact_path in adopt_paths:
                            try:
                                current = publication_root.read_bytes(artifact_path)
                            except ValueError as error:
                                raise ValueError(f"Cached build cannot recover non-regular untracked artifact path: {artifact_path}.") from error
                            if current is not None:
                                if current != rendered:
                                    raise ValueError("Cached build refuses to adopt an untracked artifact whose content differs: " f"{artifact_path}.")
                                unchanged = True
                        final_path = (
                            publication_root.requested_path / artifact_path
                            if unchanged
                            else publication_root.write_bytes(
                                artifact_path,
                                rendered,
                                mode=rendered_mode,
                                replace=replace_existing,
                            )
                        )
                    else:
                        expected = staging_root / Path(artifact_path)
                        path = Path(write(artifact, staging_root))
                        if path != expected and path.resolve(strict=False) != expected.resolve(strict=False):
                            raise ValueError(f"Artifact writer {artifact.artifact_type!r} returned an unexpected path " f"for {artifact_path}: {path}.")
                        if requires_transform:
                            assert stage_transform is not None
                            stage_transform(artifact, expected)
                        if replace_existing and expected_sha256 is None:
                            try:
                                comparison = publication_root.file_matches(artifact_path, expected)
                            except ValueError:
                                comparison = None
                            if comparison is None:
                                replace_existing = False
                            else:
                                unchanged = comparison and previous_spelling is None
                        elif expected_sha256 is None and adopt_paths is not None and artifact_path in adopt_paths:
                            try:
                                comparison = publication_root.file_matches(artifact_path, expected)
                            except ValueError as error:
                                raise ValueError(f"Cached build cannot recover non-regular untracked artifact path: {artifact_path}.") from error
                            if comparison is not None:
                                if not comparison:
                                    raise ValueError("Cached build refuses to adopt an untracked artifact whose content differs: " f"{artifact_path}.")
                                unchanged = True
                        final_path = (
                            publication_root.requested_path / artifact_path
                            if unchanged
                            else publication_root.publish_file(
                                artifact_path,
                                expected,
                                replace=replace_existing,
                            )
                        )
                if previous_spelling is not None:
                    normalized = publication_root.normalize_path_spelling(
                        previous_spelling,
                        artifact_path,
                    )
                    if not normalized:
                        raise ValueError("Cached build could not normalize a tracked artifact " f"path spelling: {previous_spelling} -> {artifact_path}.")
                written[artifact_path] = final_path
                if on_written is not None:
                    on_written(artifact, final_path)
                if expected is not None:
                    if not staging_publication.delete_file(
                        artifact_path,
                        prune_empty_parents=False,
                    ):
                        raise ValueError(f"Staged artifact disappeared after publication: {artifact_path}.")
                if progress is not None and _is_artifact_progress_checkpoint(
                    index,
                    total,
                    progress_start=progress_start,
                    progress_end=progress_end,
                ):
                    emit_progress(
                        progress,
                        "artifact_generation",
                        current=artifact_path,
                        detail=f"{'Confirmed' if unchanged else 'Wrote'} {artifact_path}.",
                        index=index,
                        label="Writing artifacts",
                        percent=scaled_percent(progress_start, progress_end, index, total),
                        total=total,
                    )
    return written


def _is_artifact_progress_checkpoint(
    index: int,
    total: int,
    *,
    progress_start: int,
    progress_end: int,
) -> bool:
    """Report the first, last, and each user-visible integer-percent change."""

    if index <= 1 or index >= total:
        return True
    return scaled_percent(progress_start, progress_end, index, total) != scaled_percent(
        progress_start,
        progress_end,
        index - 1,
        total,
    )


def _validate_artifact_batch(result: BuildResult) -> None:
    if result.blocked:
        raise ValueError("write_artifacts requires a build result without blocking diagnostics.")

    target_roots = {artifact.target_root for artifact in result.artifacts}
    if len(target_roots) > 1:
        raise ValueError("write_artifacts requires artifacts for a single target root.")
    for artifact in result.artifacts:
        _validate_artifact_path(artifact)


def _artifact_target(output_root: str | Path, artifact: Artifact) -> Path:
    """Resolve one validated writer target without crossing its output root."""

    path = _validate_artifact_path(artifact)
    target = Path(output_root) / path
    try:
        target.resolve(strict=False).relative_to(Path(output_root).resolve(strict=False))
    except ValueError as error:
        raise ValueError(f"Artifact path must stay under output root: {artifact.path}") from error
    return target


def _validate_artifact_path(artifact: Artifact) -> Path:
    """Validate one portable relative artifact path without filesystem I/O."""

    artifact_path = _artifact_path(artifact)
    path = Path(artifact_path)
    if path.is_absolute():
        raise ValueError(f"Artifact path must be relative: {artifact.path}")
    if ".." in path.parts:
        raise ValueError(f"Artifact path must stay under output root: {artifact.path}")
    for component in PurePosixPath(artifact_path).parts:
        portability_error = windows_portable_component_error(component)
        if portability_error is not None:
            raise ValueError(f"Artifact path is not portable to Windows because component " f"{component!r} {portability_error}: {artifact.path}")
    return path


def _artifact_path(artifact: Artifact) -> str:
    return str(artifact.path).replace("\\", "/")


def _loc_entries(artifact: Artifact) -> tuple[LocalizationEntry, ...]:
    if not isinstance(artifact.payload, tuple) or not all(isinstance(entry, LocalizationEntry) for entry in artifact.payload):
        raise ValueError("Localization artifacts must provide a tuple of LocalizationEntry payloads.")
    if not artifact.payload:
        raise ValueError("Localization artifacts must provide at least one entry.")
    languages = {entry.language for entry in artifact.payload}
    if len(languages) != 1:
        raise ValueError("Localization artifacts must contain entries for the same language.")
    return artifact.payload


def _sprite_types(artifact: Artifact) -> tuple[SpriteType, ...]:
    if not isinstance(artifact.payload, tuple) or not all(isinstance(sprite, SpriteType) for sprite in artifact.payload):
        raise ValueError("Sprite GFX artifacts must provide a tuple of SpriteType payloads.")
    if not artifact.payload:
        raise ValueError("Sprite GFX artifacts must provide at least one SpriteType.")
    for sprite in artifact.payload:
        _validate_sprite_type(sprite)
    return artifact.payload


def _validate_sprite_type(sprite: SpriteType) -> None:
    if not isinstance(sprite.name, str) or not sprite.name.strip():
        raise ValueError("SpriteType name must be a non-empty string.")
    if not isinstance(sprite.texturefile, str) or not sprite.texturefile.strip():
        raise ValueError("SpriteType texturefile must be a non-empty string.")
    for key in sprite.properties:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("SpriteType property keys must be non-empty strings.")
        if key in {"name", "texturefile"}:
            raise ValueError("SpriteType properties must not redefine name or texturefile.")


def _view_payload(artifact: Artifact) -> object:
    if artifact.payload is not None:
        if isinstance(artifact.payload, (dict, list, tuple)):
            return artifact.payload
        raise ValueError("View artifacts must provide a JSON-safe payload or metadata.")
    return dict(artifact.metadata)


def _descriptor_fields(artifact: Artifact) -> Mapping[str, Any]:
    if artifact.payload is None:
        return artifact.metadata
    if isinstance(artifact.payload, Mapping):
        return artifact.payload
    raise ValueError("Mod descriptor artifacts must provide a mapping payload or metadata.")


def _mod_descriptor(fields: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for key in ("version", "name", "picture", "supported_version", "path", "remote_file_id"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            lines.append(f'{key}="{_descriptor_escape(value)}"')
    tags = _descriptor_strings(fields.get("tags"))
    if tags:
        lines.append("tags={")
    for tag in tags:
        lines.append(f'\t"{_descriptor_escape(tag)}"')
    if tags:
        lines.append("}")
    for replace_path in _descriptor_strings(fields.get("replace_path")):
        lines.append(f'replace_path="{_descriptor_escape(replace_path)}"')
    return "\n".join(lines) + "\n"


def _descriptor_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(value)
    else:
        return ()
    return tuple(text for item in values if isinstance(item, str) for text in (item.strip(),) if text)


def _descriptor_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _loc_yml(entries: tuple[LocalizationEntry, ...]) -> str:
    language = entries[0].language
    lines = [f"{language}:"]
    for entry in sorted(entries, key=lambda item: item.key):
        lines.append(f' {entry.key}:0 "{_loc_escape(entry.text)}"')
    return "\n".join(lines) + "\n"


def _sprite_gfx(sprites: Sequence[SpriteType]) -> str:
    sprite_entries = [_sprite_entry(sprite) for sprite in sorted(sprites, key=lambda item: item.name)]
    block = PDXBlock.from_entries([PDXEntry.kv("spriteTypes", PDXBlock.from_entries(sprite_entries))])
    return block.to_str()


def _sprite_entry(sprite: SpriteType) -> PDXEntry:
    block = PDXBlock.from_entries(
        [
            PDXEntry.kv("name", sprite.name),
            PDXEntry.kv("texturefile", sprite.texturefile),
            *(PDXEntry.kv(key, value) for key, value in sorted(sprite.properties.items())),
        ]
    )
    return PDXEntry.kv("SpriteType", block)


def _loc_escape(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    escaped: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if normalized[index : index + 2] == "\\\\":
            # An explicit pair denotes literal slashes, not a control escape.
            escaped.append("\\\\\\\\")
            index += 2
            continue
        if character == "\\" and index + 1 < len(normalized) and normalized[index + 1] in {"n", "t"}:
            # Source localization uses HoI4's textual escapes already. Do not
            # turn the leading slash into a second slash in generated YAML.
            escaped.append(normalized[index : index + 2])
            index += 2
            continue
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        else:
            escaped.append(character)
        index += 1
    return "".join(escaped)


def _static_copy_source(artifact: Artifact) -> Path:
    if not artifact.inputs:
        raise ValueError("Static copy artifacts must provide at least one input path.")
    return Path(artifact.inputs[0])


def _static_copy_payload(artifact: Artifact, source: Path) -> bytes:
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise ValueError(f"Static copy artifact {artifact.path} source {source} cannot be read. " f"{_os_error_text(error)}.") from error
    if sha256hash(payload) != artifact.metadata.get("sha256"):
        raise ValueError(f"Static copy artifact {artifact.path} hash does not match metadata.")
    return payload


def _writer_expected_sha256(
    writer: object,
    artifact: Artifact,
    *,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    expected_sha256 = getattr(writer, "expected_sha256", None)
    if not callable(expected_sha256):
        return None
    value = expected_sha256(artifact)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError(f"Artifact writer {artifact.artifact_type!r} expected_sha256(artifact) " "must return one SHA-256 digest or None.")
    return value.casefold()


def _os_error_text(error: OSError) -> str:
    return error.strerror or str(error) or error.__class__.__name__
