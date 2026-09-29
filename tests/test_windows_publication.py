"""Contracts for the retained Win32 generated-publication backend."""

from __future__ import annotations

import ctypes
import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import paradev._win32_fs as win32_fs
import paradev.build._fs as build_fs
import paradev.build._fs_windows as windows_build_fs
from paradev.build._fs_windows import WindowsAnchoredDirectory, _windows_relative_parts


@pytest.mark.unit
def test_windows_publication_adapter_preserves_anchored_directory_identity() -> None:
    assert issubclass(WindowsAnchoredDirectory, build_fs.AnchoredDirectory)


@pytest.mark.unit
def test_windows_publication_dispatch_is_lazy_and_injectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[tuple[Path, bool]] = []

    @contextmanager
    def fake_open(path: Path, *, create: bool) -> Iterator[object]:
        calls.append((path, create))
        yield sentinel

    monkeypatch.setattr(build_fs, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(build_fs, "_open_windows_anchored_directory", fake_open)

    with build_fs.open_anchored_directory(tmp_path / "output", create=False) as authority:
        assert authority is sentinel

    assert calls == [(tmp_path / "output", False)]


@pytest.mark.unit
def test_windows_publication_preserves_operation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeNativeAuthority:
        requested_path = tmp_path
        path = tmp_path
        identity = (1, 2)
        closed = False

        def close(self) -> None:
            self.closed = True

        def verify_path(self) -> None:
            raise AssertionError("Verification must not mask a failed operation.")

    native = FakeNativeAuthority()
    monkeypatch.setattr(
        windows_build_fs.Win32DirectoryAuthority,
        "open",
        lambda path, *, create: native,
    )
    expected = FileExistsError("publication target already exists")

    with (
        pytest.raises(FileExistsError) as raised,
        windows_build_fs.open_windows_anchored_directory(tmp_path),
    ):
        raise expected

    assert raised.value is expected
    assert native.closed is True


@pytest.mark.unit
def test_windows_publication_adapter_delegates_exact_content_digest(
    tmp_path: Path,
) -> None:
    class FakeNativeAuthority:
        requested_path = tmp_path
        path = tmp_path
        identity = (1, 2)

        def file_sha256_matches(
            self,
            parts: tuple[str, ...],
            expected_sha256: str,
        ) -> bool:
            assert parts == ("nested", "asset.bin")
            assert expected_sha256 == "a" * 64
            return True

        def close(self) -> None:
            pass

    authority = WindowsAnchoredDirectory(FakeNativeAuthority())
    try:
        assert authority.file_sha256_matches("nested/asset.bin", "a" * 64) is True
    finally:
        authority.close()


@pytest.mark.unit
def test_windows_rename_buffer_uses_parent_handle_and_utf16_leaf() -> None:
    name = "PIHC3-目标.txt"
    encoded = name.encode("utf-16-le")

    buffer, size = win32_fs._rename_info_buffer(0x1234, name, replace=True)
    info = win32_fs._FILE_RENAME_INFO.from_buffer(buffer)
    offset = win32_fs._FILE_RENAME_INFO.FileName.offset
    stored_name = ctypes.string_at(ctypes.addressof(buffer) + offset, len(encoded))

    assert info.ReplaceIfExists == 1
    assert info.RootDirectory == 0x1234
    assert info.FileNameLength == len(encoded)
    assert stored_name == encoded
    assert size == ctypes.sizeof(win32_fs._FILE_RENAME_INFO) + len(encoded)
    assert win32_fs._FILE_RENAME_INFO.RootDirectory.offset == 8
    assert win32_fs._FILE_RENAME_INFO.FileNameLength.offset == 16
    assert win32_fs._FILE_RENAME_INFO.FileName.offset == 20
    assert ctypes.sizeof(win32_fs._FILE_BASIC_INFO) == 40
    assert ctypes.sizeof(win32_fs._FILE_STANDARD_INFO) == 24
    assert ctypes.sizeof(win32_fs._FILE_DISPOSITION_INFO) == 1


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="Non-Windows import-safety contract.")
def test_windows_authority_imports_safely_but_fails_closed_off_windows(tmp_path: Path) -> None:
    with pytest.raises(win32_fs.Win32FilesystemUnavailable, match="only available on Windows"):
        win32_fs.Win32DirectoryAuthority.open(tmp_path)


@pytest.mark.unit
def test_windows_authority_declares_required_native_safety_calls() -> None:
    source = Path(win32_fs.__file__).read_text(encoding="utf-8")

    assert "CreateFileW" in source
    assert "FILE_FLAG_OPEN_REPARSE_POINT" in source
    assert "GetFileInformationByHandleEx" in source
    assert "GetFinalPathNameByHandleW" in source
    assert "GetVolumeInformationByHandleW" in source
    assert "FlushFileBuffers" in source
    assert "SetFileInformationByHandle" in source
    # Renames are the one operation that cannot use the Win32 wrapper: it answers
    # ERROR_INVALID_PARAMETER for any non-NULL RootDirectory, which the retained
    # authority always supplies. The native call honours it, so FileRenameInformation
    # is issued through ntdll while every other operation stays on kernel32.
    assert "NtSetInformationFile" in source
    assert "_FILE_RENAME_INFORMATION_CLASS = 10" in source
    assert "_FILE_SHARE_READ | _FILE_SHARE_WRITE" in source
    assert "FILE_SHARE_DELETE" not in source
    assert '_SUPPORTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})' in source


@pytest.mark.unit
def test_windows_directory_handles_request_explicit_traverse_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        def close(self) -> None:
            return

    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
        size=0,
        mtime_ns=0,
    )
    requested_access: list[int] = []

    def open_handle(
        _path: Path,
        *,
        access: int,
        **_kwargs: object,
    ) -> FakeHandle:
        requested_access.append(access)
        return FakeHandle()

    monkeypatch.setattr(win32_fs, "_create_file_handle", open_handle)
    monkeypatch.setattr(win32_fs, "_metadata", lambda *_args, **_kwargs: metadata)

    handle = win32_fs._open_directory(Path("/volume/root"), api=object())  # type: ignore[arg-type]

    assert handle is not None
    assert requested_access == [win32_fs._FILE_LIST_DIRECTORY | win32_fs._FILE_TRAVERSE | win32_fs._FILE_READ_ATTRIBUTES | win32_fs._SYNCHRONIZE]


@pytest.mark.unit
def test_windows_authority_canonicalizes_to_volume_guid_namespace() -> None:
    canonical = "\\\\?\\Volume{11111111-2222-3333-4444-555555555555}\\PIHC3"

    class FakeApi:
        def __init__(self) -> None:
            self.flags: list[int] = []

        def get_final_path_name(
            self,
            handle: ctypes.c_void_p,
            buffer: ctypes.Array[ctypes.c_wchar],
            size: int,
            flags: int,
        ) -> int:
            assert handle.value == 0x1234
            assert size >= len(canonical) + 1
            self.flags.append(flags)
            buffer.value = canonical
            return len(canonical)

    api = FakeApi()
    handle = win32_fs._Win32Handle(0x1234, api)

    assert str(win32_fs._final_path(handle, api=api)) == canonical
    assert api.flags == [win32_fs._VOLUME_NAME_GUID]


@pytest.mark.unit
@pytest.mark.parametrize(
    "parts",
    [
        (),
        ("nested/path",),
        ("..",),
        ("CON.txt",),
        ("trailing.",),
        ("bad:name",),
        ("Cafe\u0301",),
        ("valid", 1),
    ],
)
def test_windows_directory_primitives_reject_malformed_component_tuples(parts: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        win32_fs._validated_relative_parts(parts)  # type: ignore[arg-type]

    assert win32_fs._validated_relative_parts((), allow_empty=True) == ()


@pytest.mark.unit
def test_windows_directory_creation_stages_retains_and_no_clobber_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def close_handle(self, handle: ctypes.c_void_p) -> int:
            assert handle.value is not None
            self.closed.append(handle.value)
            return 1

    api = FakeApi()
    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = api
    authority._identity = (7, 1)
    authority._handles = [win32_fs._Win32Handle(1, api)]
    parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/root"),
        [win32_fs._Win32Handle(2, api)],
    )
    stage = win32_fs._Win32Handle(3, api)
    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
        size=0,
        mtime_ns=0,
    )
    created_paths: list[Path] = []
    renames: list[tuple[Path, str, bool]] = []
    monkeypatch.setattr(win32_fs, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    monkeypatch.setattr(win32_fs, "_resolved_directory_name", lambda _path, _name: None)
    # After the staging rename the directory is re-opened by path, so the stub has to
    # answer for the published name as well as the staged one. The handle handed back to
    # the caller is the re-opened one: a handle that has itself been renamed cannot serve
    # as the RootDirectory of a nested rename.
    reopened = win32_fs._Win32Handle(4, api)
    monkeypatch.setattr(
        win32_fs,
        "_open_directory",
        lambda path, **_kwargs: (
            stage
            if path.name.startswith(".paradev-directory-")
            # "ideas" does not exist until the staging rename publishes it, so the
            # pre-flight probe must still miss; only the post-rename re-open resolves.
            else (reopened if path.name == "ideas" and renames else None)
        ),
    )
    monkeypatch.setattr(win32_fs, "_create_directory_exact", lambda path, **_kwargs: created_paths.append(path))
    monkeypatch.setattr(win32_fs, "_metadata", lambda handle, **_kwargs: metadata)
    monkeypatch.setattr(
        win32_fs,
        "_rename_handle",
        lambda _handle, _parent, *, parent_path, name, replace, **_kwargs: renames.append((parent_path, name, replace)),
    )
    monkeypatch.setattr(win32_fs, "_verify_retained_destination", lambda *_args, **_kwargs: metadata)

    retained, actual, created, actual_name = authority._retain_directory_child(
        parent,
        "ideas",
        create=True,
        exist_ok=False,
        delete_capable=False,
    )

    assert retained is reopened
    assert actual == metadata
    assert created is True
    assert actual_name == "ideas"
    assert created_paths == [Path("/volume/root/.paradev-directory-" + "a" * 32 + ".stage")]
    assert renames == [(Path("/volume/root"), "ideas", False)]
    retained.close()
    parent.close()


@pytest.mark.unit
def test_windows_directory_creation_preserves_collision_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def close_handle(self, _handle: ctypes.c_void_p) -> int:
            return 1

    api = FakeApi()
    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = api
    authority._identity = (7, 1)
    authority._handles = [win32_fs._Win32Handle(1, api)]
    parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/root"),
        [win32_fs._Win32Handle(2, api)],
    )
    stage = win32_fs._Win32Handle(3, api)
    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
        size=0,
        mtime_ns=0,
    )
    collision = FileExistsError("injected no-clobber collision")

    def collide(*_args: object, **_kwargs: object) -> None:
        raise collision

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(win32_fs, "_resolved_directory_name", lambda _path, _name: None)
    monkeypatch.setattr(win32_fs, "_create_directory_exact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        win32_fs,
        "_open_directory",
        lambda path, **_kwargs: stage if path.name.startswith(".paradev-directory-") else None,
    )
    monkeypatch.setattr(win32_fs, "_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(win32_fs, "_rename_handle", collide)
    monkeypatch.setattr(win32_fs, "_mark_delete", fail_cleanup)

    with pytest.raises(FileExistsError) as raised:
        authority._retain_directory_child(
            parent,
            "ideas",
            create=True,
            exist_ok=False,
            delete_capable=False,
        )

    assert raised.value is collision
    assert any("cleanup also failed" in note for note in collision.__notes__)
    parent.close()


@pytest.mark.unit
def test_windows_parent_retention_tracks_existing_exact_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def close_handle(self, _handle: ctypes.c_void_p) -> int:
            return 1

    api = FakeApi()
    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = api
    authority._identity = (7, 1)
    authority._operation_path = Path("/volume/root")
    authority._handles = [win32_fs._Win32Handle(1, api)]
    child = win32_fs._Win32Handle(2, api)
    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
        size=0,
        mtime_ns=0,
    )
    monkeypatch.setattr(
        authority,
        "_retain_directory_child",
        lambda _parent, _name, **_kwargs: (child, metadata, False, "ExactSpelling"),
    )

    retained = authority._open_parent(("exactspelling",), create=False)

    assert retained is not None
    assert retained.path == Path("/volume/root/ExactSpelling")
    retained.close()


@pytest.mark.unit
def test_windows_directory_name_resolution_rejects_portable_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        win32_fs,
        "_directory_names",
        lambda _path: ("Caf\u00e9", "Cafe\u0301"),
    )

    with pytest.raises(win32_fs.Win32UnsafePathError, match="ambiguous portable aliases"):
        win32_fs._resolved_directory_name(Path("/volume/root"), "Caf\u00e9")


@pytest.mark.unit
def test_windows_guarded_move_rolls_back_after_destination_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def close_handle(self, _handle: ctypes.c_void_p) -> int:
            return 1

    api = FakeApi()
    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = api
    authority._identity = (7, 1)
    authority._handles = [win32_fs._Win32Handle(1, api)]
    source_parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/source-parent"),
        [win32_fs._Win32Handle(2, api)],
    )
    target_parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/target-parent"),
        [win32_fs._Win32Handle(3, api)],
    )
    source = win32_fs._Win32Handle(4, api)
    source_snapshot = win32_fs.Win32FileSnapshot(
        metadata=win32_fs.Win32FileMetadata(
            volume_serial=7,
            file_id=44,
            attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
            size=0,
            mtime_ns=0,
        ),
        content=None,
    )
    events: list[tuple[str, object]] = []
    parent_identities = {
        2: win32_fs.Win32FileMetadata(7, 20, win32_fs._FILE_ATTRIBUTE_DIRECTORY, 0, 0),
        3: win32_fs.Win32FileMetadata(7, 30, win32_fs._FILE_ATTRIBUTE_DIRECTORY, 0, 0),
    }
    monkeypatch.setattr(win32_fs, "_metadata", lambda handle, **_kwargs: parent_identities[handle.value])
    monkeypatch.setattr(win32_fs, "_resolved_directory_name", lambda _path, _name: None)
    monkeypatch.setattr(
        win32_fs,
        "_rename_handle",
        lambda _handle, _parent, *, parent_path, name, replace, **_kwargs: events.append(("rename", (parent_path, name, replace))),
    )
    verification_calls = 0

    def verify(
        _handle: win32_fs._Win32Handle,
        *,
        parent_path: Path,
        name: str,
        **_kwargs: object,
    ) -> win32_fs.Win32FileMetadata:
        nonlocal verification_calls
        verification_calls += 1
        events.append(("verify", (parent_path, name)))
        if verification_calls == 2:
            raise win32_fs.Win32UnsafePathError("injected destination verification failure")
        return source_snapshot.metadata

    monkeypatch.setattr(win32_fs, "_verify_retained_destination", verify)

    with pytest.raises(win32_fs.Win32UnsafePathError, match="injected destination"):
        authority._move_retained_entry(
            source,
            source_parent=source_parent,
            source_name="before",
            source_snapshot=source_snapshot,
            target_parent=target_parent,
            target_name="after",
            guard=lambda parent_identity, snapshot: events.append(("guard", (parent_identity, snapshot))),
        )

    assert events == [
        ("verify", (Path("/volume/source-parent"), "before")),
        ("guard", ((7, 20), source_snapshot)),
        ("rename", (Path("/volume/target-parent"), "after", False)),
        ("verify", (Path("/volume/target-parent"), "after")),
        ("rename", (Path("/volume/source-parent"), "before", False)),
        ("verify", (Path("/volume/source-parent"), "before")),
    ]


@pytest.mark.unit
def test_windows_guarded_move_rejects_replaced_target_parent_before_guard_or_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def close_handle(self, _handle: ctypes.c_void_p) -> int:
            return 1

    api = FakeApi()
    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = api
    authority._identity = (7, 1)
    authority._handles = [win32_fs._Win32Handle(1, api)]
    source_parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/source-parent"),
        [win32_fs._Win32Handle(2, api)],
    )
    target_parent = win32_fs._RetainedDirectory(
        authority,
        Path("/volume/replaced-target-parent"),
        [win32_fs._Win32Handle(3, api)],
    )
    source = win32_fs._Win32Handle(4, api)
    source_snapshot = win32_fs.Win32FileSnapshot(
        metadata=win32_fs.Win32FileMetadata(
            volume_serial=7,
            file_id=44,
            attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
            size=0,
            mtime_ns=0,
        ),
        content=None,
    )
    parent_identities = {
        2: win32_fs.Win32FileMetadata(7, 20, win32_fs._FILE_ATTRIBUTE_DIRECTORY, 0, 0),
        3: win32_fs.Win32FileMetadata(7, 31, win32_fs._FILE_ATTRIBUTE_DIRECTORY, 0, 0),
    }
    monkeypatch.setattr(win32_fs, "_metadata", lambda handle, **_kwargs: parent_identities[handle.value])
    monkeypatch.setattr(
        win32_fs,
        "_rename_handle",
        lambda *_args, **_kwargs: pytest.fail("Target-parent mismatch must prevent rename."),
    )

    with pytest.raises(win32_fs.Win32UnsafePathError, match="target parent changed before mutation"):
        authority._move_retained_entry(
            source,
            source_parent=source_parent,
            source_name="before",
            source_snapshot=source_snapshot,
            target_parent=target_parent,
            target_name="after",
            guard=lambda *_args: pytest.fail("Target-parent mismatch must prevent guard."),
            expected_target_parent_identity=(7, 30),
        )


@pytest.mark.unit
def test_windows_destination_verification_requires_identity_path_and_exact_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=win32_fs._FILE_ATTRIBUTE_DIRECTORY,
        size=0,
        mtime_ns=0,
    )
    handle = object()
    monkeypatch.setattr(win32_fs, "_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(win32_fs, "_final_path", lambda *_args, **_kwargs: Path("/volume/root/Ideas"))
    monkeypatch.setattr(win32_fs, "_directory_names", lambda _path: ("Ideas",))

    assert (
        win32_fs._verify_retained_destination(
            handle,  # type: ignore[arg-type]
            parent_path=Path("/volume/root"),
            name="Ideas",
            expected_identity=(7, 44),
            api=object(),  # type: ignore[arg-type]
        )
        == metadata
    )

    with pytest.raises(win32_fs.Win32UnsafePathError, match="identity changed"):
        win32_fs._verify_retained_destination(
            handle,  # type: ignore[arg-type]
            parent_path=Path("/volume/root"),
            name="Ideas",
            expected_identity=(7, 45),
            api=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(win32_fs.Win32UnsafePathError, match="exact requested spelling"):
        win32_fs._verify_retained_destination(
            handle,  # type: ignore[arg-type]
            parent_path=Path("/volume/root"),
            name="ideas",
            expected_identity=(7, 44),
            api=object(),  # type: ignore[arg-type]
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attributes", "content"),
    [
        (win32_fs._FILE_ATTRIBUTE_DIRECTORY, None),
        (win32_fs._FILE_ATTRIBUTE_NORMAL, b"payload"),
    ],
)
def test_windows_entry_metadata_matches_guard_snapshot_shape(
    attributes: int,
    content: bytes | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHandle:
        def close(self) -> None:
            return

    class FakeParent:
        path = Path("/volume/root")
        handle = FakeHandle()

        def __enter__(self) -> FakeParent:
            return self

        def __exit__(self, *_args: object) -> None:
            return

    authority = object.__new__(win32_fs.Win32DirectoryAuthority)
    authority._api = object()
    metadata = win32_fs.Win32FileMetadata(
        volume_serial=7,
        file_id=44,
        attributes=attributes,
        size=len(content or b""),
        mtime_ns=123_456_700,
    )
    snapshot = win32_fs.Win32FileSnapshot(metadata=metadata, content=content)
    include_mtime_calls: list[bool] = []
    monkeypatch.setattr(authority, "_open_parent", lambda *_args, **_kwargs: FakeParent())
    monkeypatch.setattr(win32_fs, "_open_entry", lambda *_args, **_kwargs: FakeHandle())

    def inspect_metadata(*_args: object, include_mtime: bool = False, **_kwargs: object) -> win32_fs.Win32FileMetadata:
        include_mtime_calls.append(include_mtime)
        return metadata

    monkeypatch.setattr(win32_fs, "_metadata", inspect_metadata)
    monkeypatch.setattr(win32_fs, "_snapshot_handle", lambda *_args, **_kwargs: snapshot)

    assert authority.entry_metadata(("entry",)) == metadata
    assert authority.read_file_snapshot(("entry",)) == snapshot
    assert include_mtime_calls == [True]


@pytest.mark.unit
@pytest.mark.parametrize("path", ["CON.txt", "nested/trailing.", "nested/bad:name.txt"])
def test_windows_publication_rejects_nonportable_components(path: str) -> None:
    with pytest.raises(ValueError, match="not portable to Windows"):
        _windows_relative_parts(path)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Win32 filesystem semantics.")
def test_windows_anchored_publication_reads_replaces_and_no_clobbers(tmp_path: Path) -> None:
    root = tmp_path / "output"
    staged = tmp_path / "staged.txt"
    staged.write_bytes(b"staged")

    with build_fs.open_anchored_directory(root) as authority:
        assert isinstance(authority, WindowsAnchoredDirectory)
        assert authority.write_bytes("nested/generated.txt", b"first", replace=False) == root / "nested/generated.txt"
        assert authority.read_bytes("nested/generated.txt") == b"first"
        with pytest.raises(FileExistsError):
            authority.write_bytes("nested/generated.txt", b"unsafe", replace=False)
        assert authority.read_bytes("nested/generated.txt") == b"first"
        authority.write_bytes("nested/generated.txt", b"second")
        assert authority.read_bytes("nested/generated.txt") == b"second"
        assert authority.file_matches("nested/generated.txt", staged) is False
        authority.publish_file("nested/generated.txt", staged)
        assert authority.file_matches("nested/generated.txt", staged) is True
        assert authority.same_entry("nested/generated.txt", "nested/generated.txt") is True


@pytest.mark.skipif(os.name != "nt", reason="Requires native Win32 sharing semantics.")
def test_windows_authority_prevents_root_swap_while_open(tmp_path: Path) -> None:
    root = tmp_path / "output"
    detached = tmp_path / "detached"

    with build_fs.open_anchored_directory(root) as authority:
        with pytest.raises(OSError):
            root.rename(detached)
        authority.write_bytes("safe.txt", b"safe")

    assert (root / "safe.txt").read_bytes() == b"safe"
    assert not detached.exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows symlink or junction behavior.")
def test_windows_publication_rejects_reparse_parent(tmp_path: Path) -> None:
    root = tmp_path / "output"
    user_root = tmp_path / "user"
    user_root.mkdir()
    victim = user_root / "victim.txt"
    victim.write_text("safe", encoding="utf-8")

    with build_fs.open_anchored_directory(root) as authority:
        redirect = root / "redirect"
        try:
            redirect.symlink_to(user_root, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"This Windows host cannot create the reparse fixture: {error}")
        with pytest.raises(ValueError, match="reparse point"):
            authority.write_bytes("redirect/victim.txt", b"unsafe")

    assert victim.read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(os.name != "nt", reason="Requires native Win32 case and deletion semantics.")
def test_windows_publication_normalizes_case_deletes_and_clears(tmp_path: Path) -> None:
    root = tmp_path / "output"

    with build_fs.open_anchored_directory(root) as authority:
        authority.write_bytes("Generated/Old.txt", b"old")
        assert authority.normalize_path_spelling("Generated/Old.txt", "generated/old.txt") is True
        assert {path.name for path in root.iterdir()} == {"generated"}
        assert {path.name for path in (root / "generated").iterdir()} == {"old.txt"}
        assert authority.delete_file("generated/old.txt", exact_spelling=True) is True
        assert authority.is_empty()
        authority.write_bytes("one/a.txt", b"a")
        authority.write_bytes("two/b.txt", b"b")
        authority.clear()
        assert authority.is_empty()


@pytest.mark.skipif(os.name != "nt", reason="Requires native retained Win32 directory semantics.")
def test_windows_directory_authority_creates_moves_and_removes_entries(tmp_path: Path) -> None:
    root = tmp_path / "authority"

    with win32_fs.open_win32_directory_authority(root) as authority:
        source_metadata, created = authority.create_directory(("Families", "Source"))
        assert created is True
        assert source_metadata.is_directory
        existing, created = authority.create_directory(("Families", "Source"), exist_ok=True)
        assert existing.identity == source_metadata.identity
        assert created is False
        with pytest.raises(FileExistsError):
            authority.create_directory(("Families", "Source"))

        authority.write_bytes(("Families", "Source", "payload.txt"), b"payload", replace=False)
        directory_metadata = authority.entry_metadata(("Families", "Source"))
        file_metadata = authority.entry_metadata(("Families", "Source", "payload.txt"))
        file_snapshot = authority.read_file_snapshot(("Families", "Source", "payload.txt"))
        assert directory_metadata is not None
        assert file_metadata is not None
        assert file_snapshot is not None
        assert file_snapshot.metadata == file_metadata
        guarded: list[tuple[tuple[int, int], win32_fs.Win32FileSnapshot]] = []
        moved = authority.guarded_move_entry(
            ("Families", "Source"),
            ("Moved", "Renamed"),
            guard=lambda parent_identity, snapshot: guarded.append((parent_identity, snapshot)),
            create_target_parent=True,
        )
        assert moved.metadata == directory_metadata
        assert moved.content is None
        assert len(guarded) == 1
        assert guarded[0][1] is moved
        assert guarded[0][0][0] == source_metadata.volume_serial
        assert authority.directory_names(("Families",)) == ()
        assert authority.directory_names(("Moved",)) == ("Renamed",)
        with pytest.raises(OSError) as nonempty:
            authority.remove_empty_directory(("Moved", "Renamed"), expected_identity=source_metadata.identity)
        assert nonempty.value.errno == errno.ENOTEMPTY

        authority.guarded_remove_file(
            ("Moved", "Renamed", "payload.txt"),
            guard=lambda _parent_identity, snapshot: assert_file_snapshot(snapshot, b"payload"),
        )
        assert authority.remove_empty_directory(
            ("Moved", "Renamed"),
            expected_identity=source_metadata.identity,
        )
        assert not authority.remove_empty_directory(
            ("Moved", "Renamed"),
            expected_identity=source_metadata.identity,
        )


@pytest.mark.skipif(os.name != "nt", reason="Requires native retained Win32 directory semantics.")
def test_windows_directory_authority_supports_case_moves_and_quarantined_tree_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "authority"

    with win32_fs.open_win32_directory_authority(root) as authority:
        case_metadata, _created = authority.create_directory(("Family", "CaseName"))
        family_metadata = authority.entry_metadata(("Family",))
        assert family_metadata is not None
        authority.guarded_move_entry(
            ("Family", "CaseName"),
            ("Family", "casename"),
            guard=lambda _parent_identity, snapshot: assert_directory_snapshot(snapshot, case_metadata.identity),
            expected_target_parent_identity=family_metadata.identity,
        )
        assert authority.directory_names(("Family",)) == ("casename",)

        tree_metadata, _created = authority.create_directory(("quarantine", "tree"))
        authority.write_bytes(("quarantine", "tree", "nested", "data.txt"), b"data", replace=False)
        assert authority.remove_directory_tree(
            ("quarantine", "tree"),
            expected_identity=tree_metadata.identity,
        )
        assert authority.directory_names(("quarantine",)) == ()


def assert_file_snapshot(snapshot: win32_fs.Win32FileSnapshot | None, content: bytes) -> None:
    assert snapshot is not None
    assert snapshot.content == content
    assert not snapshot.metadata.is_directory


def assert_directory_snapshot(snapshot: win32_fs.Win32FileSnapshot, identity: tuple[int, int]) -> None:
    assert snapshot.content is None
    assert snapshot.metadata.is_directory
    assert snapshot.metadata.identity == identity
