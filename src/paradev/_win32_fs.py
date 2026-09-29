"""Retained Win32 filesystem authority for safe local-tree mutations.

The build layer adapts this private primitive to its ``AnchoredDirectory``
contract. Source-authoring transactions can reuse the same retained-handle
boundary later without importing build concepts.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing_extensions import Self
from uuid import uuid4

from paradev.portable_paths import portable_path_identity, windows_portable_component_error

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002

_CREATE_NEW = 1
_OPEN_EXISTING = 3

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_DEVICE = 0x00000040
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_FILE_BASIC_INFO_CLASS = 0
_FILE_STANDARD_INFO_CLASS = 1
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_CLASS = 4
# NT information class. Distinct from FileRenameInfo above: the Win32 wrapper
# SetFileInformationByHandle rejects a non-NULL RootDirectory, the native call honours it.
_FILE_RENAME_INFORMATION_CLASS = 10

_FILE_NAME_NORMALIZED = 0x0
_VOLUME_NAME_GUID = 0x1

_DRIVE_FIXED = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_NOT_SAME_DEVICE = 17
_ERROR_SHARING_VIOLATION = 32
_ERROR_NOT_SUPPORTED = 50
_ERROR_FILE_EXISTS = 80
_ERROR_INVALID_PARAMETER = 87
_ERROR_DIR_NOT_EMPTY = 145
_ERROR_ALREADY_EXISTS = 183
_ERROR_DIRECTORY = 267

_SUPPORTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
_READ_CHUNK_SIZE = 1024 * 1024
_WINDOWS_EPOCH_100NS = 116_444_736_000_000_000


class Win32FilesystemUnavailable(ValueError):
    """Raised when a requested Win32 safety guarantee is unavailable."""


class Win32UnsafePathError(ValueError):
    """Raised when a retained path crosses a reparse or filesystem boundary."""


class Win32FileSizeError(ValueError):
    """Raised before reading a retained file beyond a caller's byte limit."""

    def __init__(self, *, path: Path, size: int, maximum: int) -> None:
        self.path = path
        self.size = size
        self.maximum = maximum
        super().__init__(f"Retained Windows file is {size} bytes, exceeding the {maximum}-byte read limit: {path}.")


class Win32DisplacedRecoveryError(Win32UnsafePathError):
    """Raised when a guarded mutation cannot restore its displaced target."""

    def __init__(
        self,
        *,
        target_path: Path,
        displaced_path: Path,
        destination_exists: bool,
    ) -> None:
        self.target_path = target_path
        self.displaced_path = displaced_path
        self.destination_exists = destination_exists
        super().__init__(f"Retained Windows target could not be restored from {displaced_path} to {target_path}.")


def _add_exception_note(error: BaseException, note: str) -> None:
    """Retain secondary failure context on Python versions before 3.11."""

    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)
        return
    notes = getattr(error, "__notes__", None)
    if notes is None:
        notes = []
        error.__notes__ = notes
    notes.append(note)


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_uint32),
        ("ReparseTag", ctypes.c_uint32),
    ]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", ctypes.c_uint32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_int32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    """Result block for NtSetInformationFile."""

    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


@dataclass(frozen=True, slots=True)
class Win32FileMetadata:
    """Stable identity and shape for one retained filesystem entry."""

    volume_serial: int
    file_id: int
    attributes: int
    size: int
    mtime_ns: int

    @property
    def identity(self) -> tuple[int, int]:
        """Return the volume and file identifier pair."""

        return self.volume_serial, self.file_id

    @property
    def is_directory(self) -> bool:
        """Return whether this entry is a directory."""

        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def st_size(self) -> int:
        """Expose the ``stat_result`` size name used by SDK readers."""

        return self.size

    @property
    def st_mtime_ns(self) -> int:
        """Expose the ``stat_result`` mtime name used by SDK readers."""

        return self.mtime_ns


@dataclass(frozen=True, slots=True)
class Win32FileSnapshot:
    """Stable metadata and optional content captured from one retained entry."""

    metadata: Win32FileMetadata
    content: bytes | None

    @property
    def content_sha256(self) -> str | None:
        """Return the captured regular-file digest."""

        return hashlib.sha256(self.content).hexdigest() if self.content is not None else None


Win32FileGuard = Callable[[tuple[int, int], Win32FileSnapshot | None], None]


class _Kernel32:
    """Typed Kernel32 calls used by the retained authority."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise Win32FilesystemUnavailable("Win32 filesystem authority is only available on Windows.")
        library = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        self.create_file = library.CreateFileW
        self.create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.create_file.restype = ctypes.c_void_p

        self.close_handle = library.CloseHandle
        self.close_handle.argtypes = [ctypes.c_void_p]
        self.close_handle.restype = ctypes.c_int32

        self.create_directory = library.CreateDirectoryW
        self.create_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        self.create_directory.restype = ctypes.c_int32

        self.get_file_information = library.GetFileInformationByHandleEx
        self.get_file_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.get_file_information.restype = ctypes.c_int32

        self.get_volume_information = getattr(library, "GetVolumeInformationByHandleW", None)
        if self.get_volume_information is None:
            raise Win32FilesystemUnavailable("This Windows runtime cannot inspect filesystem capabilities by handle.")
        self.get_volume_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        self.get_volume_information.restype = ctypes.c_int32

        self.get_drive_type = library.GetDriveTypeW
        self.get_drive_type.argtypes = [ctypes.c_wchar_p]
        self.get_drive_type.restype = ctypes.c_uint32

        self.get_final_path_name = library.GetFinalPathNameByHandleW
        self.get_final_path_name.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.get_final_path_name.restype = ctypes.c_uint32

        self.read_file = library.ReadFile
        self.read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.read_file.restype = ctypes.c_int32

        self.write_file = library.WriteFile
        self.write_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.write_file.restype = ctypes.c_int32

        self.flush_file_buffers = library.FlushFileBuffers
        self.flush_file_buffers.argtypes = [ctypes.c_void_p]
        self.flush_file_buffers.restype = ctypes.c_int32

        self.set_file_information = library.SetFileInformationByHandle
        self.set_file_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.set_file_information.restype = ctypes.c_int32

        # Renames go through the native call: SetFileInformationByHandle(FileRenameInfo)
        # answers ERROR_INVALID_PARAMETER for any non-NULL RootDirectory, which the
        # retained authority always supplies.
        native = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        self.nt_set_information_file = native.NtSetInformationFile
        self.nt_set_information_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int32,
        ]
        self.nt_set_information_file.restype = ctypes.c_long

        self.nt_status_to_dos_error = native.RtlNtStatusToDosError
        self.nt_status_to_dos_error.argtypes = [ctypes.c_long]
        self.nt_status_to_dos_error.restype = ctypes.c_ulong


@lru_cache(maxsize=1)
def _kernel32() -> _Kernel32:
    return _Kernel32()


class _Win32Handle:
    """One owned Win32 handle."""

    def __init__(self, value: int, api: _Kernel32) -> None:
        self.value = value
        self._api = api

    @property
    def closed(self) -> bool:
        return self.value == 0

    def close(self) -> None:
        if self.closed:
            return
        value = self.value
        self.value = 0
        self._api.close_handle(ctypes.c_void_p(value))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(slots=True)
class _RetainedDirectory:
    """Relative directory handles retained below one root authority."""

    authority: Win32DirectoryAuthority
    path: Path
    handles: list[_Win32Handle]

    @property
    def handle(self) -> _Win32Handle:
        return self.handles[-1] if self.handles else self.authority._root_handle

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class Win32DirectoryAuthority:
    """Retain a verified local directory and its absolute ancestor chain."""

    def __init__(
        self,
        requested_path: Path,
        path: Path,
        handles: list[_Win32Handle],
        *,
        operation_path: Path,
        api: _Kernel32,
        filesystem: str,
    ) -> None:
        self.requested_path = requested_path
        self.path = path
        self._operation_path = operation_path
        self._handles = handles
        self._api = api
        self.filesystem = filesystem
        self._identity = _metadata(handles[-1], api=api).identity

    @classmethod
    def open(cls, path: Path, *, create: bool = True) -> Win32DirectoryAuthority:
        """Open one local NTFS/ReFS directory without traversing reparse points."""

        api = _kernel32()
        requested = path.expanduser()
        absolute = Path(os.path.abspath(requested))
        handles = _open_absolute_directory_chain(absolute, create=create, api=api)
        try:
            filesystem = _supported_filesystem(handles[0], absolute=absolute, api=api)
            operation_path = _final_path(handles[-1], api=api)
            return cls(
                requested,
                absolute,
                handles,
                operation_path=operation_path,
                api=api,
                filesystem=filesystem,
            )
        except BaseException:
            _close_handles(handles)
            raise

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    @property
    def _root_handle(self) -> _Win32Handle:
        self._require_open()
        return self._handles[-1]

    def close(self) -> None:
        _close_handles(self._handles)
        self._handles.clear()

    def verify_path(self) -> None:
        self._require_open()
        current = Win32DirectoryAuthority.open(self.path, create=False)
        try:
            if current.identity != self.identity:
                raise Win32UnsafePathError(f"Retained Windows directory changed while open: {self.requested_path}.")
        finally:
            current.close()

    def is_empty(self) -> bool:
        self._require_open()
        return not _directory_names(self._operation_path)

    def directory_names(self, parts: tuple[str, ...] = ()) -> tuple[str, ...]:
        """List exact child spellings below one retained relative directory.

        Args:
            parts: Portable root-relative directory components. An empty tuple
                selects the authority root.

        Returns:
            Exact child names in deterministic portable-identity order.

        Raises:
            FileNotFoundError: If the requested directory does not exist.
            ValueError: If ``parts`` is not a normalized portable tuple.
            Win32UnsafePathError: If the path crosses an unsafe entry.
        """

        parts = _validated_relative_parts(parts, allow_empty=True)
        retained = self._open_parent(parts, create=False)
        if retained is None:
            raise FileNotFoundError(
                errno.ENOENT,
                f"Retained Windows directory does not exist: {self.path.joinpath(*parts)}",
            )
        with retained:
            return tuple(sorted(_directory_names(retained.path), key=lambda name: (portable_path_identity(name), name)))

    def create_directory(
        self,
        parts: tuple[str, ...],
        *,
        exist_ok: bool = False,
    ) -> tuple[Win32FileMetadata, bool]:
        """Create one retained directory through a no-clobber staged rename.

        Missing parents are created through the same retained mechanism.

        Args:
            parts: Non-empty portable root-relative directory components.
            exist_ok: Return the existing directory when the final component
                already exists.

        Returns:
            The retained directory metadata and whether this call created it.

        Raises:
            FileExistsError: If the directory exists and ``exist_ok`` is false.
            ValueError: If ``parts`` is not a normalized portable tuple.
            Win32UnsafePathError: If an entry is unsafe or crosses a volume.
        """

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=True)
        if parent is None:
            raise AssertionError("Creating a retained Windows parent returned no authority.")
        with parent:
            retained = self._retain_directory_child(
                parent,
                parts[-1],
                create=True,
                exist_ok=exist_ok,
                delete_capable=False,
            )
            assert retained is not None
            child, metadata, created, _actual_name = retained
            child.close()
            return metadata, created

    def entry_exists(self, parts: tuple[str, ...]) -> bool:
        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return False
        with parent:
            handle = _open_entry(parent.path / parts[-1], api=self._api, missing_ok=True)
            if handle is None:
                return False
            handle.close()
            return True

    def same_regular_entry(self, left_parts: tuple[str, ...], right_parts: tuple[str, ...]) -> bool:
        left_parts = _validated_relative_parts(left_parts)
        right_parts = _validated_relative_parts(right_parts)
        left = self._open_regular_relative(left_parts, missing_ok=True)
        if left is None:
            return False
        right = self._open_regular_relative(right_parts, missing_ok=True)
        if right is None:
            left.close()
            return False
        try:
            return _metadata(left, api=self._api).identity == _metadata(right, api=self._api).identity
        finally:
            right.close()
            left.close()

    def read_bytes(self, parts: tuple[str, ...]) -> bytes | None:
        parts = _validated_relative_parts(parts)
        handle = self._open_regular_relative(parts, missing_ok=True, read=True)
        if handle is None:
            return None
        try:
            return _read_all(handle, api=self._api)
        finally:
            handle.close()

    def read_file_snapshot(
        self,
        parts: tuple[str, ...],
        *,
        max_bytes: int | None = None,
    ) -> Win32FileSnapshot | None:
        """Read one stable regular file while blocking writers and renames."""

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return None
        with parent:
            handle = _open_entry(
                parent.path / parts[-1],
                api=self._api,
                missing_ok=True,
                read=True,
                lock_writes=True,
            )
            if handle is None:
                return None
            try:
                return _snapshot_handle(
                    handle,
                    path=parent.path / parts[-1],
                    api=self._api,
                    max_bytes=max_bytes,
                )
            finally:
                handle.close()

    def entry_metadata(self, parts: tuple[str, ...]) -> Win32FileMetadata | None:
        """Inspect one safe retained leaf without following reparse points."""

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return None
        with parent:
            handle = _open_entry(parent.path / parts[-1], api=self._api, missing_ok=True)
            if handle is None:
                return None
            try:
                return _metadata(handle, api=self._api, include_mtime=True)
            finally:
                handle.close()

    def file_matches(self, parts: tuple[str, ...], source: Path) -> bool | None:
        parts = _validated_relative_parts(parts)
        source_handle, source_ancestors = _open_absolute_regular_file(source, api=self._api)
        target = self._open_regular_relative(parts, missing_ok=True, read=True)
        if target is None:
            source_handle.close()
            _close_handles(source_ancestors)
            return None
        try:
            source_metadata = _metadata(source_handle, api=self._api)
            target_metadata = _metadata(target, api=self._api)
            if source_metadata.size != target_metadata.size:
                return False
            return _streams_equal(source_handle, target, api=self._api)
        finally:
            target.close()
            source_handle.close()
            _close_handles(source_ancestors)

    def file_sha256_matches(
        self,
        parts: tuple[str, ...],
        expected_sha256: str,
    ) -> bool | None:
        """Compare one retained regular file with an exact SHA-256 digest."""

        parts = _validated_relative_parts(parts)
        target = self._open_regular_relative(parts, missing_ok=True, read=True)
        if target is None:
            return None
        try:
            digest = hashlib.sha256()
            while chunk := _read_chunk(target, api=self._api):
                digest.update(chunk)
            return digest.hexdigest() == expected_sha256
        finally:
            target.close()

    def write_bytes(self, parts: tuple[str, ...], payload: bytes, *, replace: bool) -> None:
        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=True)
        if parent is None:
            raise AssertionError("Creating a retained Windows parent returned no authority.")
        with parent:
            self._publish(parent, parts[-1], payload=payload, replace=replace)

    def write_bytes_snapshot(
        self,
        parts: tuple[str, ...],
        payload: bytes,
        *,
        replace: bool,
        create_parent: bool = False,
    ) -> tuple[tuple[int, int], Win32FileSnapshot]:
        """Publish bytes and return the retained parent and file identities."""

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=create_parent)
        if parent is None:
            raise FileNotFoundError(errno.ENOENT, f"Windows publication parent does not exist: {self.path.joinpath(*parts[:-1])}")
        with parent:
            parent_identity = _metadata(parent.handle, api=self._api).identity
            snapshot = self._publish(
                parent,
                parts[-1],
                payload=payload,
                replace=replace,
                capture_payload=payload,
            )
            assert snapshot is not None
            return parent_identity, snapshot

    def guarded_write_bytes(
        self,
        parts: tuple[str, ...],
        payload: bytes,
        *,
        guard: Win32FileGuard,
    ) -> tuple[tuple[int, int], Win32FileSnapshot]:
        """Publish bytes only if a retained snapshot satisfies ``guard``."""

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            raise FileNotFoundError(errno.ENOENT, f"Windows publication parent does not exist: {self.path.joinpath(*parts[:-1])}")
        with parent:
            parent_identity = _metadata(parent.handle, api=self._api).identity
            return parent_identity, self._guarded_write(
                parent,
                parts[-1],
                payload=payload,
                parent_identity=parent_identity,
                guard=guard,
            )

    def guarded_remove_file(
        self,
        parts: tuple[str, ...],
        *,
        guard: Win32FileGuard,
        missing_ok: bool = False,
    ) -> tuple[tuple[int, int], Win32FileSnapshot | None]:
        """Remove a retained regular file only when ``guard`` accepts it."""

        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False, delete_capable=True)
        if parent is None:
            guard(self.identity, None)
            if missing_ok:
                return self.identity, None
            raise FileNotFoundError(errno.ENOENT, f"Windows publication parent does not exist: {self.path.joinpath(*parts[:-1])}")
        with parent:
            parent_identity = _metadata(parent.handle, api=self._api).identity
            target_path = parent.path / parts[-1]
            target = _open_entry(
                target_path,
                api=self._api,
                missing_ok=True,
                delete=True,
                read=True,
                lock_writes=True,
            )
            if target is None:
                guard(parent_identity, None)
                if missing_ok:
                    return parent_identity, None
                raise FileNotFoundError(errno.ENOENT, f"Windows retained target does not exist: {target_path}")
            quarantine_name = f".paradev-draft-{uuid4().hex}.removed"
            quarantine_path = parent.path / quarantine_name
            try:
                snapshot = _snapshot_handle(target, path=target_path, api=self._api)
                guard(parent_identity, snapshot)
                _rename_handle(
                    target,
                    parent.handle,
                    parent_path=parent.path,
                    name=quarantine_name,
                    replace=False,
                    api=self._api,
                )
                try:
                    _mark_delete(target, path=quarantine_path, api=self._api)
                except BaseException:
                    self._restore_displaced(
                        target,
                        parent,
                        target_name=parts[-1],
                        displaced_path=quarantine_path,
                    )
                    raise
                return parent_identity, snapshot
            finally:
                target.close()

    def guarded_move_entry(
        self,
        source_parts: tuple[str, ...],
        target_parts: tuple[str, ...],
        *,
        guard: Win32FileGuard,
        create_target_parent: bool = False,
        expected_target_parent_identity: tuple[int, int] | None = None,
    ) -> Win32FileSnapshot:
        """Move one retained file or directory after an immediate source guard.

        The destination never replaces an existing portable path identity.
        The held source handle is verified at the destination after the rename.
        A failed post-rename verification triggers a best-effort no-clobber
        rollback to the exact original spelling.

        Args:
            source_parts: Non-empty portable root-relative source components.
            target_parts: Non-empty portable root-relative destination
                components.
            guard: Callback that must accept the source parent identity and
                stable source snapshot immediately before the rename.
            create_target_parent: Create missing target parents through retained
                staged directory publication.
            expected_target_parent_identity: Optional required target-parent
                identity for a caller that preflighted the destination.

        Returns:
            The stable pre-move source snapshot. Its filesystem identity is
            unchanged by a successful move.

        Raises:
            FileExistsError: If the destination portable identity is occupied.
            FileNotFoundError: If the source or target parent does not exist.
            ValueError: If either component tuple is not normalized and
                portable.
            Win32UnsafePathError: If an entry changes, crosses a volume, or
                cannot be verified safely.
        """

        source_parts = _validated_relative_parts(source_parts)
        target_parts = _validated_relative_parts(target_parts)
        if expected_target_parent_identity is not None:
            expected_target_parent_identity = _validated_file_identity(expected_target_parent_identity)
        source_identity_parts = tuple(portable_path_identity(part) for part in source_parts)
        target_parent_identity_parts = tuple(portable_path_identity(part) for part in target_parts[:-1])
        if target_parent_identity_parts[: len(source_identity_parts)] == source_identity_parts:
            raise Win32UnsafePathError(f"Retained Windows target parent is inside the source entry: " f"{self.path.joinpath(*target_parts[:-1])}.")

        source_parent = self._open_parent(source_parts[:-1], create=False)
        if source_parent is None:
            raise FileNotFoundError(
                errno.ENOENT,
                f"Retained Windows source parent does not exist: {self.path.joinpath(*source_parts[:-1])}",
            )
        with source_parent:
            source_name = _resolved_directory_name(source_parent.path, source_parts[-1])
            if source_name is None:
                raise FileNotFoundError(
                    errno.ENOENT,
                    f"Retained Windows source does not exist: {self.path.joinpath(*source_parts)}",
                )
            source_path = source_parent.path / source_name
            source = _open_entry(
                source_path,
                api=self._api,
                delete=True,
                read=True,
                lock_writes=True,
            )
            assert source is not None
            try:
                source_snapshot = _snapshot_handle(source, path=source_path, api=self._api)
                if source_snapshot.metadata.volume_serial != self.identity[0]:
                    raise Win32UnsafePathError(f"Retained Windows source crosses a filesystem boundary: {source_path}.")
                target_parent = self._open_parent(
                    target_parts[:-1],
                    create=create_target_parent,
                    delete_capable=False,
                )
                if target_parent is None:
                    raise FileNotFoundError(
                        errno.ENOENT,
                        f"Retained Windows target parent does not exist: {self.path.joinpath(*target_parts[:-1])}",
                    )
                with target_parent:
                    return self._move_retained_entry(
                        source,
                        source_parent=source_parent,
                        source_name=source_name,
                        source_snapshot=source_snapshot,
                        target_parent=target_parent,
                        target_name=target_parts[-1],
                        guard=guard,
                        expected_target_parent_identity=expected_target_parent_identity,
                    )
            finally:
                source.close()

    def remove_empty_directory(
        self,
        parts: tuple[str, ...],
        *,
        expected_identity: tuple[int, int],
    ) -> bool:
        """Remove an empty retained directory only at its expected identity.

        Args:
            parts: Non-empty portable root-relative directory components.
            expected_identity: Expected volume and file identifier.

        Returns:
            ``True`` when the expected directory was marked for deletion, or
            ``False`` when the name is absent or now identifies another entry.

        Raises:
            OSError: If the expected directory is not empty.
            ValueError: If a tuple is malformed.
            Win32UnsafePathError: If the path is not a safe directory.
        """

        parts = _validated_relative_parts(parts)
        expected_identity = _validated_file_identity(expected_identity)
        retained = self._open_expected_directory(parts, expected_identity=expected_identity)
        if retained is None:
            return False
        parent, target, target_path = retained
        try:
            if _directory_names(target_path):
                raise OSError(errno.ENOTEMPTY, f"Retained Windows directory is not empty: {target_path}", str(target_path))
            _mark_delete(target, path=target_path, api=self._api)
            return True
        finally:
            target.close()
            parent.close()

    def remove_directory_tree(
        self,
        parts: tuple[str, ...],
        *,
        expected_identity: tuple[int, int],
    ) -> bool:
        """Intentionally remove one quarantined tree at its expected identity.

        This destructive primitive is for private recovery/quarantine paths.
        If recursive cleanup fails, the remaining subtree stays at the same
        quarantined name for later recovery.

        Args:
            parts: Non-empty portable root-relative directory components.
            expected_identity: Expected volume and file identifier.

        Returns:
            ``True`` when the expected tree was marked for deletion, or
            ``False`` when the name is absent or now identifies another entry.

        Raises:
            ValueError: If a tuple is malformed.
            Win32UnsafePathError: If any entry is unsafe or crosses a volume.
            OSError: If Windows cannot delete part of the retained tree.
        """

        parts = _validated_relative_parts(parts)
        expected_identity = _validated_file_identity(expected_identity)
        retained = self._open_expected_directory(parts, expected_identity=expected_identity)
        if retained is None:
            return False
        parent, target, target_path = retained
        try:
            self._clear_directory(target_path, target)
            _mark_delete(target, path=target_path, api=self._api)
            return True
        finally:
            target.close()
            parent.close()

    def publish_file(self, parts: tuple[str, ...], source: Path, *, replace: bool) -> None:
        parts = _validated_relative_parts(parts)
        source_handle, source_ancestors = _open_absolute_regular_file(source, api=self._api)
        try:
            parent = self._open_parent(parts[:-1], create=True)
            if parent is None:
                raise AssertionError("Creating a retained Windows parent returned no authority.")
            with parent:
                self._publish(parent, parts[-1], source=source_handle, replace=replace)
        finally:
            source_handle.close()
            _close_handles(source_ancestors)

    def normalize_path_spelling(self, previous_parts: tuple[str, ...], current_parts: tuple[str, ...]) -> bool:
        previous_parts = _validated_relative_parts(previous_parts)
        current_parts = _validated_relative_parts(current_parts)
        retained = _RetainedDirectory(self, self._operation_path, [])
        try:
            for index, (previous_name, current_name) in enumerate(zip(previous_parts, current_parts, strict=True)):
                names = _directory_names(retained.path)
                actual_name = current_name if current_name in names else previous_name if previous_name in names else None
                if actual_name is None:
                    return False
                child_path = retained.path / actual_name
                child = _open_entry(child_path, api=self._api, delete=True)
                assert child is not None
                metadata = _metadata(child, api=self._api)
                if actual_name != current_name:
                    _rename_case_only(child, retained.handle, parent_path=retained.path, current_name=current_name, api=self._api)
                    child_path = retained.path / current_name
                if index == len(current_parts) - 1:
                    child.close()
                    return not metadata.is_directory
                if not metadata.is_directory:
                    child.close()
                    raise Win32UnsafePathError(f"Retained Windows path contains a non-directory parent: {child_path}.")
                retained.handles.append(child)
                retained.path = child_path
            return False
        finally:
            retained.close()

    def delete_file(
        self,
        parts: tuple[str, ...],
        *,
        exact_spelling: bool,
        prune_empty_parents: bool = True,
    ) -> bool:
        parts = _validated_relative_parts(parts)
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return False
        try:
            if exact_spelling and parts[-1] not in _directory_names(parent.path):
                return False
            leaf = _open_entry(parent.path / parts[-1], api=self._api, missing_ok=True, delete=True)
            if leaf is None:
                return False
            try:
                metadata = _metadata(leaf, api=self._api)
                if metadata.is_directory:
                    raise Win32UnsafePathError(f"Retained Windows file path is a directory: {parent.path / parts[-1]}.")
                _mark_delete(leaf, path=parent.path / parts[-1], api=self._api)
            finally:
                leaf.close()
            if prune_empty_parents:
                for index in range(len(parent.handles) - 1, -1, -1):
                    handle = parent.handles[index]
                    directory_path = self._operation_path.joinpath(*parts[: index + 1])
                    if _directory_names(directory_path):
                        break
                    try:
                        _mark_delete(handle, path=directory_path, api=self._api)
                    except OSError as error:
                        if error.errno == errno.ENOTEMPTY:
                            break
                        raise
                    handle.close()
                    parent.handles[index] = _Win32Handle(0, self._api)
            return True
        finally:
            parent.close()

    def clear(self) -> None:
        self._require_open()
        self._clear_directory(self._operation_path, self._root_handle)

    def _clear_directory(self, path: Path, directory: _Win32Handle) -> None:
        for name in sorted(_directory_names(path)):
            child_path = path / name
            child = _open_entry(child_path, api=self._api, delete=True)
            if child is None:
                raise Win32UnsafePathError(f"Retained Windows entry changed during full clean: {child_path}.")
            try:
                metadata = _metadata(child, api=self._api)
                if metadata.volume_serial != self.identity[0]:
                    raise Win32UnsafePathError(f"Retained Windows entry crosses a filesystem boundary: {child_path}.")
                if metadata.is_directory:
                    self._clear_directory(child_path, child)
                _mark_delete(child, path=child_path, api=self._api)
            finally:
                child.close()

    def _move_retained_entry(
        self,
        source: _Win32Handle,
        *,
        source_parent: _RetainedDirectory,
        source_name: str,
        source_snapshot: Win32FileSnapshot,
        target_parent: _RetainedDirectory,
        target_name: str,
        guard: Win32FileGuard,
        expected_target_parent_identity: tuple[int, int] | None = None,
    ) -> Win32FileSnapshot:
        source_parent_identity = _metadata(source_parent.handle, api=self._api).identity
        target_parent_identity = _metadata(target_parent.handle, api=self._api).identity
        if expected_target_parent_identity is not None and target_parent_identity != expected_target_parent_identity:
            raise Win32UnsafePathError(
                f"Retained Windows move target parent changed before mutation: "
                f"expected {expected_target_parent_identity}, found {target_parent_identity} "
                f"at {target_parent.path}."
            )
        same_parent = source_parent_identity == target_parent_identity
        same_portable_name = portable_path_identity(source_name) == portable_path_identity(target_name)
        target_alias = _resolved_directory_name(target_parent.path, target_name)
        if target_alias is not None and not (same_parent and same_portable_name and target_alias == source_name):
            raise FileExistsError(
                errno.EEXIST,
                f"Retained Windows move target already exists: {target_parent.path / target_name}",
                str(target_parent.path / target_name),
            )

        _verify_retained_destination(
            source,
            parent_path=source_parent.path,
            name=source_name,
            expected_identity=source_snapshot.metadata.identity,
            api=self._api,
        )
        guard(source_parent_identity, source_snapshot)
        if same_parent and source_name == target_name:
            return source_snapshot

        renamed = False
        try:
            _rename_handle(
                source,
                target_parent.handle,
                parent_path=target_parent.path,
                name=target_name,
                replace=False,
                api=self._api,
            )
            renamed = True
            _verify_retained_destination(
                source,
                parent_path=target_parent.path,
                name=target_name,
                expected_identity=source_snapshot.metadata.identity,
                api=self._api,
            )
            return source_snapshot
        except BaseException as error:
            if renamed:
                self._rollback_retained_move(
                    source,
                    source_parent=source_parent,
                    source_name=source_name,
                    expected_identity=source_snapshot.metadata.identity,
                    operation_error=error,
                )
            raise

    def _rollback_retained_move(
        self,
        source: _Win32Handle,
        *,
        source_parent: _RetainedDirectory,
        source_name: str,
        expected_identity: tuple[int, int],
        operation_error: BaseException,
    ) -> None:
        try:
            _rename_handle(
                source,
                source_parent.handle,
                parent_path=source_parent.path,
                name=source_name,
                replace=False,
                api=self._api,
            )
            _verify_retained_destination(
                source,
                parent_path=source_parent.path,
                name=source_name,
                expected_identity=expected_identity,
                api=self._api,
            )
        except BaseException as rollback_error:
            _add_exception_note(
                operation_error,
                f"Best-effort retained Windows move rollback failed: {type(rollback_error).__name__}: {rollback_error}",
            )

    def _open_expected_directory(
        self,
        parts: tuple[str, ...],
        *,
        expected_identity: tuple[int, int],
    ) -> tuple[_RetainedDirectory, _Win32Handle, Path] | None:
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return None
        try:
            actual_name = _resolved_directory_name(parent.path, parts[-1])
            if actual_name is None:
                parent.close()
                return None
            target_path = parent.path / actual_name
            target = _open_directory(
                target_path,
                api=self._api,
                missing_ok=True,
                delete=True,
            )
            if target is None:
                parent.close()
                return None
            try:
                metadata = _metadata(target, api=self._api)
                if metadata.identity != expected_identity:
                    target.close()
                    parent.close()
                    return None
                if metadata.volume_serial != self.identity[0]:
                    raise Win32UnsafePathError(f"Retained Windows directory crosses a filesystem boundary: {target_path}.")
                _verify_retained_destination(
                    target,
                    parent_path=parent.path,
                    name=actual_name,
                    expected_identity=expected_identity,
                    api=self._api,
                )
                return parent, target, target_path
            except BaseException:
                target.close()
                raise
        except BaseException:
            parent.close()
            raise

    def _retain_directory_child(
        self,
        parent: _RetainedDirectory,
        name: str,
        *,
        create: bool,
        exist_ok: bool,
        delete_capable: bool,
    ) -> tuple[_Win32Handle, Win32FileMetadata, bool, str] | None:
        actual_name = _resolved_directory_name(parent.path, name)
        target_path = parent.path / (actual_name or name)
        child = _open_directory(
            target_path,
            api=self._api,
            missing_ok=True,
            delete=delete_capable,
        )
        if child is not None:
            try:
                actual_name = _resolved_directory_name(parent.path, name)
                if actual_name is None:
                    raise Win32UnsafePathError(f"Retained Windows directory disappeared after it was opened: {target_path}.")
                target_path = parent.path / actual_name
                metadata = self._require_local_directory(child, path=target_path)
            except BaseException:
                child.close()
                raise
            if not exist_ok:
                child.close()
                raise FileExistsError(
                    errno.EEXIST,
                    f"Retained Windows directory already exists: {target_path}",
                    str(target_path),
                )
            return child, metadata, False, actual_name
        if not create:
            return None

        for _attempt in range(8):
            stage_name = f".paradev-directory-{uuid4().hex}.stage"
            stage_path = parent.path / stage_name
            try:
                _create_directory_exact(stage_path, api=self._api)
            except FileExistsError:
                continue
            stage = _open_directory(stage_path, api=self._api, delete=True)
            if stage is None:
                raise Win32UnsafePathError(f"Staged Windows directory disappeared before retention: {stage_path}.")
            keep_stage = False
            try:
                metadata = self._require_local_directory(stage, path=stage_path)
                try:
                    _rename_handle(
                        stage,
                        parent.handle,
                        parent_path=parent.path,
                        name=name,
                        replace=False,
                        api=self._api,
                    )
                except FileExistsError as collision:
                    try:
                        _mark_delete(stage, path=stage_path, api=self._api)
                    except BaseException as cleanup_error:
                        if not exist_ok:
                            _add_exception_note(
                                collision,
                                f"Retained Windows staged-directory cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}",
                            )
                            raise collision
                        _add_exception_note(
                            cleanup_error,
                            f"Retained Windows staged-directory collision was: {collision}",
                        )
                        raise
                    if not exist_ok:
                        raise
                    actual_name = _resolved_directory_name(parent.path, name)
                    if actual_name is None:
                        continue
                    existing_path = parent.path / actual_name
                    child = _open_directory(
                        existing_path,
                        api=self._api,
                        missing_ok=True,
                        delete=delete_capable,
                    )
                    if child is None:
                        continue
                    try:
                        existing = self._require_local_directory(child, path=existing_path)
                    except BaseException:
                        child.close()
                        raise
                    keep_stage = False
                    return child, existing, False, actual_name
                try:
                    metadata = _verify_retained_destination(
                        stage,
                        parent_path=parent.path,
                        name=name,
                        expected_identity=metadata.identity,
                        api=self._api,
                    )
                except BaseException as verification_error:
                    try:
                        _mark_delete(stage, path=target_path, api=self._api)
                    except BaseException as cleanup_error:
                        _add_exception_note(
                            verification_error,
                            f"Best-effort staged-directory cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}",
                        )
                    raise
                # A handle that has itself been renamed cannot serve as the RootDirectory
                # of a later rename; Windows answers STATUS_SHARING_VIOLATION. Nested
                # publication uses the handle returned here as the anchor for the level
                # below, so it has to be re-opened by path. Close the staged handle first:
                # it holds DELETE with share READ|WRITE, so re-opening with DELETE while it
                # is still open collides with itself.
                keep_stage = True
                stage.close()
                reopened = _open_directory(target_path, api=self._api, missing_ok=True, delete=delete_capable)
                if reopened is None:
                    raise Win32UnsafePathError(f"Retained Windows directory vanished immediately after publication: {target_path}.")
                return reopened, metadata, True, name
            finally:
                if not keep_stage:
                    stage.close()
        raise Win32UnsafePathError(f"Retained Windows directory could not be created after repeated destination races: {target_path}.")

    def _require_local_directory(
        self,
        handle: _Win32Handle,
        *,
        path: Path,
    ) -> Win32FileMetadata:
        metadata = _metadata(handle, api=self._api)
        if not metadata.is_directory:
            raise Win32UnsafePathError(f"Retained Windows path is not a directory: {path}.")
        if metadata.volume_serial != self.identity[0]:
            raise Win32UnsafePathError(f"Retained Windows directory crosses a filesystem boundary: {path}.")
        return metadata

    def _open_parent(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
        delete_capable: bool = False,
    ) -> _RetainedDirectory | None:
        self._require_open()
        parts = _validated_relative_parts(parts, allow_empty=True)
        retained = _RetainedDirectory(self, self._operation_path, [])
        try:
            for part in parts:
                child_result = self._retain_directory_child(
                    retained,
                    part,
                    create=create,
                    exist_ok=True,
                    delete_capable=delete_capable,
                )
                if child_result is None:
                    retained.close()
                    return None
                child, _metadata_result, _created, actual_name = child_result
                retained.handles.append(child)
                retained.path /= actual_name
            return retained
        except BaseException:
            retained.close()
            raise

    def _open_regular_relative(
        self,
        parts: tuple[str, ...],
        *,
        missing_ok: bool,
        read: bool = False,
    ) -> _Win32Handle | None:
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return None
        try:
            return _open_regular(parent.path / parts[-1], api=self._api, missing_ok=missing_ok, read=read)
        finally:
            parent.close()

    def _publish(
        self,
        parent: _RetainedDirectory,
        name: str,
        *,
        payload: bytes | None = None,
        source: _Win32Handle | None = None,
        replace: bool,
        capture_payload: bytes | None = None,
    ) -> Win32FileSnapshot | None:
        temp_name = f".{name}.paradev-{uuid4().hex}.tmp"
        temp_path = parent.path / temp_name
        temp = _create_regular(temp_path, api=self._api)
        committed = False
        try:
            if payload is not None:
                _write_all(temp, payload, api=self._api)
            elif source is not None:
                _copy_stream(source, temp, api=self._api)
            else:
                raise AssertionError("Windows publication requires bytes or a staged source.")
            _flush(temp, path=temp_path, api=self._api)
            snapshot = (
                Win32FileSnapshot(
                    metadata=_metadata(temp, api=self._api, include_mtime=True),
                    content=capture_payload,
                )
                if capture_payload is not None
                else None
            )
            _rename_handle(
                temp,
                parent.handle,
                parent_path=parent.path,
                name=name,
                replace=replace,
                api=self._api,
            )
            committed = True
            return snapshot
        finally:
            if not committed:
                try:
                    _mark_delete(temp, path=temp_path, api=self._api)
                except OSError:
                    pass
            temp.close()

    def _guarded_write(
        self,
        parent: _RetainedDirectory,
        name: str,
        *,
        payload: bytes,
        parent_identity: tuple[int, int],
        guard: Win32FileGuard,
    ) -> Win32FileSnapshot:
        temp_name = f".{name}.paradev-{uuid4().hex}.tmp"
        temp_path = parent.path / temp_name
        target_path = parent.path / name
        quarantine_name = f".paradev-draft-{uuid4().hex}.previous"
        quarantine_path = parent.path / quarantine_name
        temp = _create_regular(temp_path, api=self._api)
        target: _Win32Handle | None = None
        displaced = False
        committed = False
        try:
            _write_all(temp, payload, api=self._api)
            _flush(temp, path=temp_path, api=self._api)
            written = Win32FileSnapshot(
                metadata=_metadata(temp, api=self._api, include_mtime=True),
                content=payload,
            )
            target = _open_entry(
                target_path,
                api=self._api,
                missing_ok=True,
                delete=True,
                read=True,
                lock_writes=True,
            )
            current = _snapshot_handle(target, path=target_path, api=self._api) if target is not None else None
            guard(parent_identity, current)
            if target is not None:
                _rename_handle(
                    target,
                    parent.handle,
                    parent_path=parent.path,
                    name=quarantine_name,
                    replace=False,
                    api=self._api,
                )
                displaced = True
            try:
                _rename_handle(
                    temp,
                    parent.handle,
                    parent_path=parent.path,
                    name=name,
                    replace=False,
                    api=self._api,
                )
            except BaseException:
                if target is not None and displaced:
                    self._restore_displaced(
                        target,
                        parent,
                        target_name=name,
                        displaced_path=quarantine_path,
                    )
                    displaced = False
                raise
            committed = True
            if target is not None and displaced:
                try:
                    _mark_delete(target, path=quarantine_path, api=self._api)
                except OSError:
                    pass
            return written
        finally:
            if not committed:
                try:
                    _mark_delete(temp, path=temp_path, api=self._api)
                except OSError:
                    pass
            temp.close()
            if target is not None:
                target.close()

    def _restore_displaced(
        self,
        displaced: _Win32Handle,
        parent: _RetainedDirectory,
        *,
        target_name: str,
        displaced_path: Path,
    ) -> None:
        try:
            _rename_handle(
                displaced,
                parent.handle,
                parent_path=parent.path,
                name=target_name,
                replace=False,
                api=self._api,
            )
        except OSError as error:
            raise Win32DisplacedRecoveryError(
                target_path=parent.path / target_name,
                displaced_path=displaced_path,
                destination_exists=isinstance(error, FileExistsError),
            ) from error

    def _require_open(self) -> None:
        if not self._handles:
            raise ValueError("Retained Windows directory authority is already closed.")


@contextmanager
def open_win32_directory_authority(path: Path, *, create: bool = True) -> Iterator[Win32DirectoryAuthority]:
    """Open and verify one retained local Windows directory authority."""

    authority = Win32DirectoryAuthority.open(path, create=create)
    try:
        yield authority
        authority.verify_path()
    finally:
        authority.close()


def _validated_relative_parts(
    parts: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Validate one already-split portable root-relative path."""

    if not isinstance(parts, tuple) or (not parts and not allow_empty):
        requirement = "possibly empty" if allow_empty else "non-empty"
        raise ValueError(f"Retained Windows path must be a {requirement} tuple of portable components: {parts!r}.")
    for component in parts:
        if not isinstance(component, str):
            raise ValueError(f"Retained Windows path component must be text: {component!r}.")
        if unicodedata.normalize("NFC", component) != component:
            raise ValueError(f"Retained Windows path component must use NFC normalization: {component!r}.")
        if error := windows_portable_component_error(component):
            raise ValueError(f"Retained Windows path component {component!r} is not portable to Windows because it {error}.")
    return parts


def _validated_file_identity(identity: tuple[int, int]) -> tuple[int, int]:
    """Validate one retained volume and file identifier pair."""

    if not isinstance(identity, tuple) or len(identity) != 2 or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in identity):
        raise ValueError(f"Retained Windows file identity must be a pair of non-negative integers: {identity!r}.")
    return identity


def _resolved_directory_name(path: Path, requested_name: str) -> str | None:
    """Resolve one portable identity to its single exact directory spelling."""

    requested_identity = portable_path_identity(requested_name)
    matches = tuple(name for name in _directory_names(path) if portable_path_identity(name) == requested_identity)
    if not matches:
        return None
    if len(matches) > 1:
        raise Win32UnsafePathError(f"Retained Windows directory contains ambiguous portable aliases for {requested_name!r}: " f"{', '.join(sorted(matches))}.")
    return matches[0]


def _verify_retained_destination(
    handle: _Win32Handle,
    *,
    parent_path: Path,
    name: str,
    expected_identity: tuple[int, int],
    api: _Kernel32,
) -> Win32FileMetadata:
    """Verify that one retained identity is published at an exact destination."""

    metadata = _metadata(handle, api=api, include_mtime=True)
    if metadata.identity != expected_identity:
        raise Win32UnsafePathError(f"Retained Windows entry identity changed after publication: {parent_path / name}.")
    expected_path = parent_path / name
    actual_path = _final_path(handle, api=api)
    if portable_path_identity(actual_path) != portable_path_identity(expected_path):
        raise Win32UnsafePathError(f"Retained Windows entry was not published at its expected destination: " f"expected {expected_path}, found {actual_path}.")
    if _resolved_directory_name(parent_path, name) != name:
        raise Win32UnsafePathError(f"Retained Windows destination does not preserve its exact requested spelling: {expected_path}.")
    return metadata


def _open_absolute_directory_chain(path: Path, *, create: bool, api: _Kernel32) -> list[_Win32Handle]:
    if not path.is_absolute() or not path.anchor:
        raise Win32FilesystemUnavailable(f"Windows authority requires an absolute local path: {path}.")
    if path.drive.startswith("\\\\") or api.get_drive_type(str(Path(path.anchor))) != _DRIVE_FIXED:
        raise Win32FilesystemUnavailable(f"Windows authority supports only fixed local drives: {path}.")
    handles: list[_Win32Handle] = []
    current = Path(path.anchor)
    try:
        root = _open_directory(current, api=api)
        assert root is not None
        handles.append(root)
        _supported_filesystem(root, absolute=path, api=api)
        current = _final_path(root, api=api)
        for part in path.parts[1:]:
            current /= part
            if create:
                _create_directory(current, api=api)
            child = _open_directory(current, api=api, missing_ok=not create)
            if child is None:
                raise FileNotFoundError(errno.ENOENT, f"Windows directory does not exist: {current}", str(current))
            handles.append(child)
        return handles
    except BaseException:
        _close_handles(handles)
        raise


def _open_absolute_regular_file(path: Path, *, api: _Kernel32) -> tuple[_Win32Handle, list[_Win32Handle]]:
    absolute = Path(os.path.abspath(path.expanduser()))
    parent_handles = _open_absolute_directory_chain(absolute.parent, create=False, api=api)
    try:
        canonical_parent = _final_path(parent_handles[-1], api=api)
        handle = _open_regular(canonical_parent / absolute.name, api=api, read=True)
        assert handle is not None
        return handle, parent_handles
    except BaseException:
        _close_handles(parent_handles)
        raise


def _open_directory(
    path: Path,
    *,
    api: _Kernel32,
    missing_ok: bool = False,
    delete: bool = False,
) -> _Win32Handle | None:
    access = _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if delete:
        access |= _DELETE
    handle = _create_file_handle(
        path,
        access=access,
        disposition=_OPEN_EXISTING,
        flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        api=api,
        missing_ok=missing_ok,
    )
    if handle is None:
        return None
    try:
        metadata = _metadata(handle, api=api)
        _require_safe_metadata(path, metadata, directory=True)
        return handle
    except BaseException:
        handle.close()
        raise


def _open_regular(
    path: Path,
    *,
    api: _Kernel32,
    missing_ok: bool = False,
    read: bool = False,
    lock_writes: bool = False,
) -> _Win32Handle | None:
    access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if read:
        access |= _GENERIC_READ
    handle = _create_file_handle(
        path,
        access=access,
        disposition=_OPEN_EXISTING,
        flags=_FILE_FLAG_OPEN_REPARSE_POINT,
        api=api,
        missing_ok=missing_ok,
        share_write=not lock_writes,
    )
    if handle is None:
        return None
    try:
        metadata = _metadata(handle, api=api)
        _require_safe_metadata(path, metadata, directory=False)
        return handle
    except BaseException:
        handle.close()
        raise


def _open_entry(
    path: Path,
    *,
    api: _Kernel32,
    missing_ok: bool = False,
    delete: bool = False,
    read: bool = False,
    lock_writes: bool = False,
) -> _Win32Handle | None:
    access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if delete:
        access |= _DELETE
    if read:
        access |= _GENERIC_READ
    handle = _create_file_handle(
        path,
        access=access,
        disposition=_OPEN_EXISTING,
        flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        api=api,
        missing_ok=missing_ok,
        share_write=not lock_writes,
    )
    if handle is None:
        return None
    try:
        metadata = _metadata(handle, api=api)
        _require_safe_metadata(path, metadata)
        return handle
    except BaseException:
        handle.close()
        raise


def _create_regular(path: Path, *, api: _Kernel32) -> _Win32Handle:
    handle = _create_file_handle(
        path,
        access=_GENERIC_READ | _GENERIC_WRITE | _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        disposition=_CREATE_NEW,
        flags=_FILE_ATTRIBUTE_NORMAL,
        api=api,
    )
    assert handle is not None
    return handle


def _create_file_handle(
    path: Path,
    *,
    access: int,
    disposition: int,
    flags: int,
    api: _Kernel32,
    missing_ok: bool = False,
    share_write: bool = True,
) -> _Win32Handle | None:
    share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE
    if not share_write:
        share_mode = _FILE_SHARE_READ
    value = api.create_file(
        _extended_path(path),
        access,
        share_mode,
        None,
        disposition,
        flags,
        None,
    )
    if value == _INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if missing_ok and code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            return None
        raise _windows_error(code, path=path, action="open")
    if value is None:
        raise Win32FilesystemUnavailable(f"Windows returned a null filesystem handle: {path}.")
    return _Win32Handle(int(value), api)


def _create_directory(path: Path, *, api: _Kernel32) -> None:
    try:
        _create_directory_exact(path, api=api)
    except FileExistsError:
        pass


def _create_directory_exact(path: Path, *, api: _Kernel32) -> None:
    """Create exactly one absent directory without accepting an existing path."""

    if api.create_directory(_extended_path(path), None):
        return
    code = ctypes.get_last_error()
    raise _windows_error(code, path=path, action="create directory")


def _metadata(
    handle: _Win32Handle,
    *,
    api: _Kernel32,
    include_mtime: bool = False,
) -> Win32FileMetadata:
    file_id = _FILE_ID_INFO()
    _get_file_information(handle, _FILE_ID_INFO_CLASS, file_id, api=api)
    attribute_tag = _FILE_ATTRIBUTE_TAG_INFO()
    _get_file_information(handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, attribute_tag, api=api)
    mtime_ns = 0
    if include_mtime:
        basic = _FILE_BASIC_INFO()
        _get_file_information(handle, _FILE_BASIC_INFO_CLASS, basic, api=api)
        mtime_ns = (int(basic.LastWriteTime) - _WINDOWS_EPOCH_100NS) * 100
    standard = _FILE_STANDARD_INFO()
    _get_file_information(handle, _FILE_STANDARD_INFO_CLASS, standard, api=api)
    identifier = int.from_bytes(bytes(file_id.FileId.Identifier), "little")
    return Win32FileMetadata(
        volume_serial=int(file_id.VolumeSerialNumber),
        file_id=identifier,
        attributes=int(attribute_tag.FileAttributes),
        size=int(standard.EndOfFile),
        mtime_ns=mtime_ns,
    )


def _get_file_information(handle: _Win32Handle, info_class: int, target: ctypes.Structure, *, api: _Kernel32) -> None:
    if api.get_file_information(
        ctypes.c_void_p(handle.value),
        info_class,
        ctypes.byref(target),
        ctypes.sizeof(target),
    ):
        return
    raise _windows_error(ctypes.get_last_error(), action="inspect retained handle")


def _snapshot_handle(
    handle: _Win32Handle,
    *,
    path: Path,
    api: _Kernel32,
    max_bytes: int | None = None,
) -> Win32FileSnapshot:
    before = _metadata(handle, api=api, include_mtime=True)
    if before.is_directory:
        return Win32FileSnapshot(metadata=before, content=None)
    if max_bytes is not None and before.size > max_bytes:
        raise Win32FileSizeError(
            path=path,
            size=before.size,
            maximum=max_bytes,
        )
    content = _read_all(handle, api=api)
    after = _metadata(handle, api=api, include_mtime=True)
    if after.identity != before.identity or after.size != before.size or after.mtime_ns != before.mtime_ns or len(content) != before.size:
        raise Win32UnsafePathError(f"Retained Windows file changed while it was read: {path}.")
    return Win32FileSnapshot(metadata=after, content=content)


def _require_safe_metadata(path: Path, metadata: Win32FileMetadata, *, directory: bool | None = None) -> None:
    if metadata.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise Win32UnsafePathError(f"Retained Windows path crosses a reparse point: {path}.")
    if metadata.attributes & _FILE_ATTRIBUTE_DEVICE:
        raise Win32UnsafePathError(f"Retained Windows path is a device, not a regular filesystem entry: {path}.")
    if directory is True and not metadata.is_directory:
        raise Win32UnsafePathError(f"Retained Windows path is not a directory: {path}.")
    if directory is False and metadata.is_directory:
        raise Win32UnsafePathError(f"Retained Windows path is not a regular file: {path}.")


def _supported_filesystem(root: _Win32Handle, *, absolute: Path, api: _Kernel32) -> str:
    filesystem_buffer = ctypes.create_unicode_buffer(64)
    volume_name_buffer = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    maximum_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    if not api.get_volume_information(
        ctypes.c_void_p(root.value),
        volume_name_buffer,
        len(volume_name_buffer),
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        filesystem_buffer,
        len(filesystem_buffer),
    ):
        raise _windows_error(ctypes.get_last_error(), path=absolute, action="inspect filesystem")
    filesystem = filesystem_buffer.value.upper()
    if filesystem not in _SUPPORTED_FILESYSTEMS:
        supported = ", ".join(sorted(_SUPPORTED_FILESYSTEMS))
        raise Win32FilesystemUnavailable(f"Safe Windows publication requires {supported}; found {filesystem or 'unknown'} at {absolute}.")
    return filesystem


def _final_path(handle: _Win32Handle, *, api: _Kernel32) -> Path:
    """Return a stable volume-GUID path for path-based child operations."""

    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = api.get_final_path_name(
            ctypes.c_void_p(handle.value),
            buffer,
            size,
            _FILE_NAME_NORMALIZED | _VOLUME_NAME_GUID,
        )
        if length == 0:
            raise _windows_error(ctypes.get_last_error(), action="canonicalize retained handle")
        if length < size:
            path = buffer.value
            if not path.startswith("\\\\?\\Volume{"):
                raise Win32FilesystemUnavailable(f"Windows retained handle has no stable local volume-GUID path: {path or '<empty>'}.")
            return Path(path)
        size = length + 1


def _directory_names(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(_extended_path(path)) as entries:
            return tuple(entry.name for entry in entries)
    except OSError as error:
        raise Win32UnsafePathError(f"Retained Windows directory cannot be enumerated safely: {path}.") from error


def _write_all(handle: _Win32Handle, payload: bytes, *, api: _Kernel32) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        chunk = bytes(view[offset : offset + _READ_CHUNK_SIZE])
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not api.write_file(
            ctypes.c_void_p(handle.value),
            buffer,
            len(chunk),
            ctypes.byref(written),
            None,
        ):
            raise _windows_error(ctypes.get_last_error(), action="write retained file")
        if written.value <= 0:
            raise OSError(errno.EIO, "Windows retained write made no progress.")
        offset += written.value


def _read_chunk(handle: _Win32Handle, *, api: _Kernel32) -> bytes:
    buffer = ctypes.create_string_buffer(_READ_CHUNK_SIZE)
    read = ctypes.c_uint32()
    if not api.read_file(
        ctypes.c_void_p(handle.value),
        buffer,
        _READ_CHUNK_SIZE,
        ctypes.byref(read),
        None,
    ):
        raise _windows_error(ctypes.get_last_error(), action="read retained file")
    return buffer.raw[: read.value]


def _read_all(handle: _Win32Handle, *, api: _Kernel32) -> bytes:
    chunks: list[bytes] = []
    while chunk := _read_chunk(handle, api=api):
        chunks.append(chunk)
    return b"".join(chunks)


def _streams_equal(left: _Win32Handle, right: _Win32Handle, *, api: _Kernel32) -> bool:
    while True:
        left_chunk = _read_chunk(left, api=api)
        right_chunk = _read_chunk(right, api=api)
        if left_chunk != right_chunk:
            return False
        if not left_chunk:
            return True


def _copy_stream(source: _Win32Handle, target: _Win32Handle, *, api: _Kernel32) -> None:
    while chunk := _read_chunk(source, api=api):
        _write_all(target, chunk, api=api)


def _flush(handle: _Win32Handle, *, path: Path, api: _Kernel32) -> None:
    if api.flush_file_buffers(ctypes.c_void_p(handle.value)):
        return
    raise _windows_error(ctypes.get_last_error(), path=path, action="flush retained file")


def _rename_handle(
    handle: _Win32Handle,
    parent: _Win32Handle,
    *,
    parent_path: Path,
    name: str,
    replace: bool,
    api: _Kernel32,
) -> None:
    buffer, size = _rename_info_buffer(parent.value, name, replace=replace)
    iosb = _IO_STATUS_BLOCK()
    status = api.nt_set_information_file(
        ctypes.c_void_p(handle.value),
        ctypes.byref(iosb),
        buffer,
        size,
        _FILE_RENAME_INFORMATION_CLASS,
    )
    if status == 0:
        return
    code = int(api.nt_status_to_dos_error(status))
    if not replace and code in {_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION, _ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
        # The four codes are distinguishable only by NTSTATUS; a bare "already exists"
        # hides a sharing violation, which is a different defect with a different owner.
        raise FileExistsError(
            errno.EEXIST,
            f"Windows publication target already exists (NTSTATUS=0x{status & 0xFFFFFFFF:08X}): {parent_path / name}",
            str(parent_path / name),
        )
    raise _windows_error(code, path=parent_path / name, action="rename retained file")


def _rename_case_only(
    handle: _Win32Handle,
    parent: _Win32Handle,
    *,
    parent_path: Path,
    current_name: str,
    api: _Kernel32,
) -> None:
    _rename_handle(handle, parent, parent_path=parent_path, name=current_name, replace=False, api=api)
    if current_name not in _directory_names(parent_path):
        raise Win32UnsafePathError(f"Windows did not preserve the requested case-only spelling: {parent_path / current_name}.")


def _rename_info_buffer(root_handle: int, name: str, *, replace: bool) -> tuple[ctypes.Array[ctypes.c_char], int]:
    encoded = name.encode("utf-16-le")
    offset = _FILE_RENAME_INFO.FileName.offset
    size = ctypes.sizeof(_FILE_RENAME_INFO) + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    info = _FILE_RENAME_INFO.from_buffer(buffer)
    info.ReplaceIfExists = int(replace)
    info.RootDirectory = root_handle
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    return buffer, size


def _mark_delete(handle: _Win32Handle, *, path: Path, api: _Kernel32) -> None:
    disposition = _FILE_DISPOSITION_INFO(DeleteFile=1)
    if api.set_file_information(
        ctypes.c_void_p(handle.value),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        return
    raise _windows_error(ctypes.get_last_error(), path=path, action="delete retained entry")


def _extended_path(path: Path) -> str:
    text = str(path).replace("/", "\\")
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return f"\\\\?\\UNC\\{text[2:]}"
    text = str(path.absolute()).replace("/", "\\")
    return f"\\\\?\\{text}"


def _windows_error(code: int, *, path: Path | None = None, action: str) -> OSError:
    detail = ctypes.FormatError(code).strip() if os.name == "nt" else f"Win32 error {code}"
    message = f"Cannot {action}: {detail}"
    filename = str(path) if path is not None else None
    if code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        error: OSError = FileNotFoundError(errno.ENOENT, message, filename)
    elif code in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
        error = FileExistsError(errno.EEXIST, message, filename)
    elif code in {_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION}:
        error = PermissionError(errno.EACCES, message, filename)
    elif code == _ERROR_DIR_NOT_EMPTY:
        error = OSError(errno.ENOTEMPTY, message, filename)
    elif code == _ERROR_NOT_SAME_DEVICE:
        error = OSError(errno.EXDEV, message, filename)
    elif code in {_ERROR_NOT_SUPPORTED, _ERROR_INVALID_PARAMETER, _ERROR_DIRECTORY}:
        error = OSError(errno.ENOTSUP, message, filename)
    else:
        error = OSError(errno.EIO, message, filename)
    error.winerror = code
    return error


def _close_handles(handles: list[_Win32Handle]) -> None:
    for handle in reversed(handles):
        handle.close()
