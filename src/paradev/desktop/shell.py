"""Transport-neutral desktop shell command planners."""

from __future__ import annotations

import os

# Process creation stays in heavenbase.utils.cmd; subprocess only supplies the
# portable DEVNULL sentinel for detached GUI launchers.
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from heavenbase.utils import cmd

from paradev.config import CM_PARADEV
from paradev.pdx import PDXBlock, PDXParseError

HOI4_STEAM_APP_ID = "394360"
HOI4_STEAM_LAUNCH_URL = f"steam://run/{HOI4_STEAM_APP_ID}"
HOI4_MACOS_GAME_ROOT_SUFFIX = "Library/Application Support/Steam/steamapps/common/Hearts of Iron IV"
_HOI4_MACOS_APP_NAMES = ("dowser.app", "hoi4.app")
_HOI4_MACOS_EXECUTABLE_NAMES = frozenset({"dowser", "hoi4"})
_HOI4_LINUX_EXECUTABLE_NAMES = frozenset({"hoi4"})
BUILD_STRICT_METADATA_CONFIG_KEY = "paradev.build.strict_metadata"
HOI4_LAUNCH_MODE_CONFIG_KEY = "paradev.hoi4.launch_mode"
HOI4_GAME_ROOT_CONFIG_KEY = "paradev.hoi4.game_root"
HOI4_LAUNCH_READINESS_SCHEMA = "paradev.desktop.hoi4-launch-readiness.v1"
OPEN_PATH_TARGETS_SCHEMA = "paradev.desktop.open-path-targets.v1"
OPEN_PATH_PLATFORM_VALUES: tuple[str, ...] = ("macos", "windows", "linux", "unknown")
_OPEN_PATH_EDITOR_PLATFORMS = OPEN_PATH_PLATFORM_VALUES
OPEN_PATH_TARGET_ROWS: tuple[dict[str, object], ...] = (
    {"id": "finder", "labelKey": "openTarget.finder", "platforms": ("macos",)},
    {"id": "explorer", "labelKey": "openTarget.explorer", "platforms": ("windows",)},
    {
        "id": "cursor",
        "labelKey": "openTarget.cursor",
        "platforms": _OPEN_PATH_EDITOR_PLATFORMS,
    },
    {
        "id": "vscode",
        "labelKey": "openTarget.vscode",
        "platforms": _OPEN_PATH_EDITOR_PLATFORMS,
    },
    {
        "id": "sublimeText",
        "labelKey": "openTarget.sublimeText",
        "platforms": _OPEN_PATH_EDITOR_PLATFORMS,
    },
    {"id": "terminal", "labelKey": "openTarget.terminal", "platforms": ("macos",)},
    {"id": "iterm2", "labelKey": "openTarget.iterm2", "platforms": ("macos",)},
    {"id": "cmd", "labelKey": "openTarget.cmd", "platforms": ("windows",)},
    {
        "id": "powershell",
        "labelKey": "openTarget.powershell",
        "platforms": ("windows",),
    },
)
OPEN_PATH_DEFAULT_TARGETS: Mapping[str, str] = {
    "macos": "finder",
    "windows": "explorer",
    "linux": "cursor",
    "unknown": "cursor",
}


def desktop_project_build_command(
    project_root: str | Path,
    *,
    mode: str | None = None,
    profile: str | None = None,
    strict_metadata: bool | None = None,
    parallelism: int | None = None,
    target: Mapping[str, object] | None = None,
    progress_jsonl: str | Path | None = None,
) -> list[str]:
    """Return the CLI command used by the desktop shell to start a build.

    Args:
        project_root: Project root passed to `paradev build`.
        mode: Build mode, either `cached` or `full`. Empty values default to
            `cached`.
        profile: Optional build profile.
        strict_metadata: Whether to pass `--strict-metadata` or
            `--no-strict-metadata`. When omitted, defaults to
            `paradev.build.strict_metadata` from `CM_PARADEV`.
        parallelism: Optional build worker count.
        target: Optional target mapping with `kind`, `id`, and optional
            `family` keys. Supported kinds are `module`, `collection`, and
            `family`.
        progress_jsonl: Optional path for compiler progress JSON lines.

    Returns:
        Command tokens for the desktop build service.

    Raises:
        ValueError: If the request contains an empty required value,
            unsupported mode, unsupported target kind, or invalid parallelism.
    """

    root = _required_text(project_root, "project root")
    build_mode = _build_mode(mode)
    tokens = ["uv", "run", "paradev", "build", root]
    if clean_profile := _normalized_optional_text(profile):
        tokens.extend(["--profile", clean_profile])
    _push_build_target_args(tokens, target)
    if build_mode == "full" and target is not None:
        raise ValueError("A full rebuild cannot be combined with a family, collection, or module target.")
    build_strict_metadata = _configured_build_strict_metadata(strict_metadata)
    if build_strict_metadata:
        tokens.append("--strict-metadata")
    elif strict_metadata is False:
        tokens.append("--no-strict-metadata")
    if parallelism is not None:
        if parallelism < 1:
            raise ValueError("build parallelism must be at least 1.")
        tokens.extend(["--parallelism", str(parallelism)])
    tokens.extend(
        [
            "--emit-artifacts",
            "--emit-manifests",
            "--no-sync-launcher-descriptor",
        ]
    )
    if build_mode == "full":
        tokens.append("--full-rebuild")
    if progress_jsonl is not None:
        tokens.extend(["--progress-jsonl", str(progress_jsonl)])
    tokens.extend(["--summary", "--json"])
    return tokens


def desktop_hoi4_launch_command(
    *,
    mode: str | None = None,
    game_root: str | Path | None = None,
    platform: str | None = None,
) -> list[str]:
    """Return the platform command used by the desktop shell to launch HOI4.

    Args:
        mode: Launch mode, either `steam` or `local`. Empty values default to
            `steam`.
        game_root: Optional local game root for `local` mode.
        platform: Optional platform override for deterministic tests. Defaults
            to the current Python platform.

    Returns:
        Command tokens for the desktop HOI4 launcher service.

    Raises:
        ValueError: If the mode or platform is unsupported, or local mode
            cannot resolve a launchable HOI4 app or executable.
    """

    launch_target = _hoi4_launch_target_for_mode(_configured_hoi4_launch_mode(mode), game_root, platform)
    return _launcher_command(launch_target, platform)


def desktop_run_hoi4(
    *,
    project_root: str | Path | None = None,
    game_root: str | Path | None = None,
    mode: str | None = None,
    launch: bool = True,
    platform: str | None = None,
) -> dict[str, object]:
    """Launch HOI4 or return the desktop launch payload without spawning.

    Args:
        project_root: Optional active project root. The desktop shell validates
            that the value is non-empty when supplied.
        game_root: Optional local game root for `local` mode.
        mode: Launch mode, either `steam` or `local`.
        launch: Whether to spawn the launcher command. Pass `False` for dry
            planning and tests.
        platform: Optional platform override for deterministic tests. Defaults
            to the current Python platform.

    Returns:
        JSON-safe payload matching `paradev.desktop.game-launch.v1`.

    Raises:
        ValueError: If any requested launch value is invalid, project output
            is not launcher-ready, or Steam launch availability cannot be
            verified for a real launch.
    """

    if project_root is not None:
        _preflight_hoi4_project_launch(_required_text(project_root, "project root"))
    launch_mode = _configured_hoi4_launch_mode(mode)
    launch_target = _hoi4_launch_target_for_mode(launch_mode, game_root, platform)
    command = _launcher_command(launch_target, platform)
    if launch:
        if launch_mode == "steam":
            _require_steam_launch_available(platform)
        cmd(
            command,
            wait=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return {
        "schema": "paradev.desktop.game-launch.v1",
        "game": "hoi4",
        "status": "started",
        "mode": launch_mode,
        "gameRoot": launch_target,
        "command": command,
    }


def desktop_hoi4_launch_readiness(project_root: str | Path) -> dict[str, object]:
    """Return authoritative, read-only HOI4 launch readiness for one project.

    The hidden publication ledger is the source of truth. Renderer history is
    deliberately excluded so readiness survives app restarts, local-storage
    cleanup, and builds started through another ParaDev surface.

    Args:
        project_root: ParaDev project root to inspect.

    Returns:
        JSON-safe payload matching
        ``paradev.desktop.hoi4-launch-readiness.v1``. ``code`` is stable for
        clients; ``reason`` is an actionable user-facing explanation.
    """

    # Keep SDK/build imports lazy: the desktop shell is imported while the
    # public SDK facade is initialized.
    from paradev.build.publication import load_publication_state
    from paradev.sdk.project import _hoi4_launcher_descriptor_target, open_project

    project = open_project(_required_text(project_root, "project root"))
    payload = {
        "schema": HOI4_LAUNCH_READINESS_SCHEMA,
        "game": project.game,
        "projectId": project.project_id,
        "projectRoot": str(project.root),
        "outputRoot": str(project.output_root),
    }

    def result(code: str, reason: str, *, ready: bool = False) -> dict[str, object]:
        return {**payload, "ready": ready, "code": code, "reason": reason}

    if project.game != "hoi4":
        return result(
            "unsupported_game",
            f"Run Game currently supports only HOI4 projects; {project.project_id!r} targets {project.game!r}.",
        )

    output_descriptor = project.output_root / "descriptor.mod"
    if not output_descriptor.is_file():
        return result(
            "generated_descriptor_missing",
            f"{project.title} is not ready to launch: generated descriptor is missing at {output_descriptor}. "
            "Run a full or cached build with artifact emission first.",
        )

    launcher_descriptor = _hoi4_launcher_descriptor_target(project)
    if launcher_descriptor is None:
        return result(
            "output_not_launcher_visible",
            f"{project.title} is not launcher-visible: output folder {project.output_root} is outside the active HOI4 user mod folder. "
            "Use the project output path shown in Settings > Projects, then rebuild.",
        )
    if not launcher_descriptor.is_file():
        return result(
            "launcher_descriptor_missing",
            f"{project.title} is not registered with the HOI4 launcher: descriptor is missing at {launcher_descriptor}. "
            "Run a full or cached build with artifact emission first.",
        )

    try:
        publication_state = load_publication_state(
            project.build_root,
            project_id=project.project_id,
            project_root=project.root,
            output_root=project.output_root,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return result(
            "publication_invalid",
            f"{project.title} publication metadata could not be validated: {error}. " "Run a whole-project Cached build to repair it before launching HOI4.",
        )
    if not publication_state.complete:
        return result(
            "publication_incomplete",
            f"{project.title} does not have a complete whole-project publication matching the current build and output folders. "
            "Build the whole project once with Clean/Full or Cached before launching HOI4.",
        )
    if not publication_state.whole_project_baseline:
        return result(
            "whole_project_baseline_missing",
            f"{project.title} does not have a complete whole-project publication; the current ledger contains only a partial build. "
            "Build the whole project once with Clean/Full or Cached before launching HOI4.",
        )

    try:
        launcher_data = PDXBlock.from_file(launcher_descriptor).to_dict()
    except (OSError, UnicodeError, PDXParseError) as error:
        return result(
            "launcher_descriptor_unreadable",
            f"{project.title} has an unreadable HOI4 launcher descriptor at {launcher_descriptor}: {error}. " "Rebuild the project to repair it.",
        )
    target = launcher_data.get("path") if isinstance(launcher_data, Mapping) else None
    if not isinstance(target, str) or not target.strip():
        return result(
            "launcher_path_missing",
            f"{project.title} has an invalid HOI4 launcher descriptor at {launcher_descriptor}: path is missing. " "Rebuild the project to repair it.",
        )
    try:
        registered_output = Path(target).expanduser().resolve(strict=False)
        expected_output = project.output_root.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        return result(
            "launcher_path_invalid",
            f"Cannot validate the HOI4 launcher path for {project.title}: {error}. Rebuild the project to repair it.",
        )
    if registered_output != expected_output:
        return result(
            "launcher_path_mismatch",
            f"{project.title} launcher descriptor points to {registered_output}, not the current output folder {expected_output}. "
            "Rebuild the project to refresh launcher registration.",
        )
    return result(
        "ready",
        f"{project.title} has a complete whole-project publication and is ready to launch.",
        ready=True,
    )


def _preflight_hoi4_project_launch(project_root: str) -> None:
    """Require a compiled, launcher-visible HOI4 project before starting the game."""

    readiness = desktop_hoi4_launch_readiness(project_root)
    if readiness["ready"] is not True:
        raise ValueError(str(readiness["reason"]))


def desktop_open_path_command(
    path: str | Path,
    target: str | None = None,
    *,
    platform: str | None = None,
) -> list[str]:
    """Return the platform command used by the desktop shell to open a path.

    Args:
        path: Existing file or directory path.
        target: Desktop opener id, such as `finder`, `vscode`, `cursor`,
            `sublimeText`, `terminal`, `iterm2`, `explorer`, `cmd`, or
            `powershell`. Empty values and the legacy `default` alias use the
            same platform default target as the GUI picker.
        platform: Optional platform override for deterministic tests. Defaults
            to the current Python platform.

    Returns:
        Command tokens for the desktop open-path service.

    Raises:
        ValueError: If the path is empty, missing, unsupported, or incompatible
            with the selected platform.
    """

    clean_path = _required_text(path, "Open path")
    if not Path(clean_path).exists():
        raise ValueError(f"Open path does not exist: {clean_path}")
    platform_id = _platform_id(platform)
    target_id = _open_path_target(target, platform_id)
    if target_id == "finder":
        return _macos_open_app_command("Finder", None, clean_path, platform_id)
    if target_id == "explorer":
        return _windows_command("Explorer", ["explorer", clean_path], platform_id)
    if target_id == "vscode":
        return ["code", "-r", clean_path]
    if target_id == "cursor":
        return ["cursor", "-r", clean_path]
    if target_id == "sublimeText":
        if platform_id == "macos":
            return _macos_open_app_command("Sublime Text", "Sublime Text", clean_path, platform_id)
        return ["subl", clean_path]
    if target_id == "terminal":
        return _macos_open_app_command("Terminal", "Terminal", clean_path, platform_id)
    if target_id == "iterm2":
        return _macos_open_app_command("iTerm2", "iTerm", clean_path, platform_id)
    if target_id == "cmd":
        return _windows_command(
            "Command Prompt",
            ["cmd", "/C", "start", "", "/D", clean_path, "cmd"],
            platform_id,
        )
    if target_id == "powershell":
        return _windows_command(
            "PowerShell",
            [
                "powershell",
                "-NoExit",
                "-Command",
                "Set-Location -LiteralPath $args[0]",
                clean_path,
            ],
            platform_id,
        )
    raise ValueError(f"Unsupported open path target: {target_id}")


def desktop_open_path(
    path: str | Path,
    target: str | None = None,
    *,
    launch: bool = True,
    platform: str | None = None,
) -> list[str]:
    """Open a local path or return the desktop opener command without spawning.

    Args:
        path: Existing file or directory path.
        target: Optional desktop opener id.
        launch: Whether to spawn the opener command. Pass `False` for dry
            planning and tests.
        platform: Optional platform override for deterministic tests. Defaults
            to the current Python platform.

    Returns:
        Command tokens for the desktop open-path service.

    Raises:
        ValueError: If the path or opener target is invalid.
    """

    command = desktop_open_path_command(path, target, platform=platform)
    if launch:
        cmd(
            command,
            wait=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return command


def desktop_open_path_targets(*, platform: str | None = None) -> dict[str, object]:
    """Return the SDK-owned desktop open-path target catalog.

    Args:
        platform: Optional platform override. Unknown labels normalize to the
            cross-platform `unknown` bucket used by the GUI.

    Returns:
        JSON-safe catalog with every target row, the normalized platform,
        platform-compatible target ids, and the platform default target.
    """

    platform_id = _platform_id(platform)
    targets = [_open_path_target_row(row) for row in OPEN_PATH_TARGET_ROWS]
    return {
        "schema": OPEN_PATH_TARGETS_SCHEMA,
        "platform": platform_id,
        "defaultTarget": _default_open_path_target(platform_id),
        "availableTargetIds": [str(row["id"]) for row in targets if platform_id in row["platforms"]],
        "targets": targets,
    }


def desktop_select_project_path(*, platform: str | None = None) -> str | None:
    """Open the native macOS folder picker for one ParaDev project.

    Args:
        platform: Optional platform override for deterministic tests. Defaults
            to the current Python platform.

    Returns:
        The resolved selected directory, or `None` when the user cancels.

    Raises:
        OSError: If the macOS picker executable is unavailable.
        RuntimeError: If the picker fails or returns a non-directory path.
        ValueError: If the browser-hosted picker is requested on an unsupported
            platform.
    """

    if _platform_id(platform) == "windows":
        selected = _windows_picker_path(
            capability="Project folder selection",
            prompt="Open a ParaDev project",
            kind="folder",
        )
    else:
        selected = _macos_picker_path(
            platform=platform,
            capability="Project folder selection",
            script='POSIX path of (choose folder with prompt "Open a ParaDev project")',
        )
    if selected is None:
        return None
    selected_path = Path(selected).expanduser().resolve(strict=False)
    if not selected_path.is_dir():
        raise RuntimeError(f"The project folder picker returned a non-directory path: {selected or '<empty>'}")
    return str(selected_path)


def desktop_select_project_package_path(*, platform: str | None = None) -> str | None:
    """Open the native macOS file picker for one ParaDev project package.

    Args:
        platform (str | None): Optional platform override for deterministic
            tests. Defaults to the current Python platform.

    Returns:
        str | None: Resolved selected ZIP path, or `None` when the user
        cancels.

    Raises:
        OSError: If the macOS picker executable is unavailable.
        RuntimeError: If the picker fails or returns a non-ZIP file path.
        ValueError: If the browser-hosted picker is requested on an
            unsupported platform.
    """

    if _platform_id(platform) == "windows":
        selected = _windows_picker_path(
            capability="Project package selection",
            prompt="Install a ParaDev project package",
            kind="zip",
        )
    else:
        selected = _macos_picker_path(
            platform=platform,
            capability="Project package selection",
            script=('POSIX path of (choose file of type {"public.zip-archive"} ' 'with prompt "Install a ParaDev project package")'),
        )
    if selected is None:
        return None
    selected_path = Path(selected).expanduser().resolve(strict=False)
    if not selected_path.is_file() or selected_path.suffix.casefold() != ".zip":
        raise RuntimeError("The project package picker returned a non-ZIP file path: " f"{selected or '<empty>'}")
    return str(selected_path)


def _windows_picker_path(
    *,
    capability: str,
    prompt: str,
    kind: str,
) -> str | None:
    """Return one path from a native Win32 picker, sharing the cancellation policy.

    Args:
        capability: Human-readable capability name used in error messages.
        prompt: Dialog title shown to the user.
        kind: Either `"folder"` or `"zip"`.

    Returns:
        The selected path, or `None` when the user cancels.

    Raises:
        OSError: If the Win32 dialog libraries are unavailable.
        RuntimeError: If the picker fails or returns an empty path.
    """

    import ctypes
    from ctypes import wintypes

    try:
        comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    except OSError as error:  # pragma: no cover - only on a crippled Windows install
        raise OSError(f"The Windows {capability.casefold()} picker is unavailable: {error}") from error

    # The dialogs are modal and run on whichever worker thread serves the request,
    # so OLE has to be initialised there. S_OK and S_FALSE both mean "usable".
    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = ctypes.c_long
    initialized = ole32.OleInitialize(None) in (0, 1)
    try:
        if kind == "folder":
            # IFileOpenDialog with FOS_PICKFOLDERS: the Explorer-style dialog, whose address
            # bar accepts a pasted path. SHBrowseForFolderW was tried first and rejected --
            # it is a tree, so a hidden folder anywhere along the path makes the target
            # unreachable, and a project path is usually deep enough that clicking down to
            # it is impractical. (BIF_EDITBOX does add a typed-path field there, but it does
            # not solve the hidden-folder case.)
            #
            # The vtable offsets below are hand-written. That is safe by COM contract: an
            # interface's method order is frozen once published, which is what lets compiled
            # callers keep working across Windows versions.
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

                def __init__(self, d1: int, d2: int, d3: int, rest: bytes) -> None:
                    super().__init__(d1, d2, d3, (ctypes.c_ubyte * 8)(*rest))

            CLSID_FileOpenDialog = GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, b"\xa5\xa1\x60\xf8\x2a\x20\xae\xf7")
            IID_IFileOpenDialog = GUID(0xD57C7288, 0xD4AD, 0x4768, b"\xbe\x02\x9d\x96\x95\x32\xd9\x60")

            ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(GUID),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            ole32.CoCreateInstance.restype = ctypes.c_long

            dialog = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(CLSID_FileOpenDialog),
                None,
                0x1 | 0x4,  # CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER
                ctypes.byref(IID_IFileOpenDialog),
                ctypes.byref(dialog),
            )
            if hr < 0:
                raise RuntimeError(f"The Windows {capability.casefold()} picker could not be created: HRESULT 0x{hr & 0xFFFFFFFF:08X}")

            vtable = ctypes.cast(dialog, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

            def method(index: int, restype: object, *argtypes: object):
                proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
                return proto(vtable[index])

            # IFileOpenDialog vtable: IUnknown 0-2, IFileDialog 3-, IModalWindow::Show at 3.
            release = method(2, ctypes.c_ulong)
            show = method(3, ctypes.c_long, ctypes.c_void_p)
            set_options = method(9, ctypes.c_long, ctypes.c_uint32)
            get_options = method(10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))
            set_title = method(17, ctypes.c_long, ctypes.c_wchar_p)
            get_result = method(20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))

            try:
                options = ctypes.c_uint32()
                if get_options(dialog, ctypes.byref(options)) < 0:
                    raise RuntimeError(f"The Windows {capability.casefold()} picker could not be configured.")
                # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
                set_options(dialog, options.value | 0x20 | 0x40 | 0x800)
                set_title(dialog, prompt)

                hr = show(dialog, None)
                if hr == -2147023673:  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
                    return None
                if hr < 0:
                    raise RuntimeError(f"The Windows {capability.casefold()} picker failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")

                item = ctypes.c_void_p()
                if get_result(dialog, ctypes.byref(item)) < 0 or not item:
                    raise RuntimeError(f"The Windows {capability.casefold()} picker returned no selection.")
                item_vtable = ctypes.cast(item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                item_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(item_vtable[2])
                get_display_name = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p)
                )(item_vtable[5])
                try:
                    name = ctypes.c_wchar_p()
                    if get_display_name(item, 0x80058000, ctypes.byref(name)) < 0:  # SIGDN_FILESYSPATH
                        raise RuntimeError(f"The Windows {capability.casefold()} picker returned an unresolvable folder.")
                    selected = name.value or ""
                    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                    ole32.CoTaskMemFree(ctypes.cast(name, ctypes.c_void_p))
                finally:
                    item_release(item)
            finally:
                release(dialog)
        elif kind == "zip":
            class OPENFILENAMEW(ctypes.Structure):
                _fields_ = [
                    ("lStructSize", wintypes.DWORD),
                    ("hwndOwner", wintypes.HWND),
                    ("hInstance", wintypes.HINSTANCE),
                    ("lpstrFilter", wintypes.LPCWSTR),
                    ("lpstrCustomFilter", wintypes.LPWSTR),
                    ("nMaxCustFilter", wintypes.DWORD),
                    ("nFilterIndex", wintypes.DWORD),
                    ("lpstrFile", wintypes.LPWSTR),
                    ("nMaxFile", wintypes.DWORD),
                    ("lpstrFileTitle", wintypes.LPWSTR),
                    ("nMaxFileTitle", wintypes.DWORD),
                    ("lpstrInitialDir", wintypes.LPCWSTR),
                    ("lpstrTitle", wintypes.LPCWSTR),
                    ("Flags", wintypes.DWORD),
                    ("nFileOffset", wintypes.WORD),
                    ("nFileExtension", wintypes.WORD),
                    ("lpstrDefExt", wintypes.LPCWSTR),
                    ("lCustData", wintypes.LPARAM),
                    ("lpfnHook", ctypes.c_void_p),
                    ("lpTemplateName", wintypes.LPCWSTR),
                    ("pvReserved", ctypes.c_void_p),
                    ("dwReserved", wintypes.DWORD),
                    ("FlagsEx", wintypes.DWORD),
                ]

            buffer = ctypes.create_unicode_buffer(32768)
            ofn = OPENFILENAMEW()
            ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
            ofn.lpstrFilter = "ParaDev project package (*.zip)\0*.zip\0"
            ofn.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
            ofn.nMaxFile = 32768
            ofn.lpstrTitle = prompt
            ofn.lpstrDefExt = "zip"
            # PATHMUSTEXIST | FILEMUSTEXIST | NOCHANGEDIR | EXPLORER
            ofn.Flags = 0x00000800 | 0x00001000 | 0x00000008 | 0x00080000

            comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
            comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
            if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
                # CommDlgExtendedError returns 0 when the user simply cancelled.
                comdlg32.CommDlgExtendedError.restype = wintypes.DWORD
                code = comdlg32.CommDlgExtendedError()
                if code == 0:
                    return None
                raise RuntimeError(f"The Windows {capability.casefold()} picker failed: CommDlgExtendedError 0x{code:04X}")
            selected = buffer.value
        else:  # pragma: no cover - guarded by the callers
            raise RuntimeError(f"Unknown Windows picker kind: {kind!r}")
    finally:
        if initialized:
            ole32.OleUninitialize()

    if not selected:
        raise RuntimeError(f"The Windows {capability.casefold()} picker returned an empty path.")
    return selected


def _macos_picker_path(
    *,
    platform: str | None,
    capability: str,
    script: str,
) -> str | None:
    """Return one path from an AppleScript picker with shared cancellation policy."""

    platform_id = _platform_id(platform)
    if platform_id != "macos":
        raise ValueError(f"{capability} in browser-hosted ParaDev currently supports macOS only. " "Use a supported ParaDev desktop application.")
    executable = shutil.which("osascript")
    if executable is None:
        raise OSError(f"The macOS {capability.casefold()} picker is unavailable because osascript was not found.")
    result = cmd(
        [executable, "-e", script],
        include=("ok", "out", "err"),
        stdin=subprocess.DEVNULL,
    )
    if not isinstance(result, Mapping):
        raise RuntimeError(f"The macOS {capability.casefold()} picker returned an invalid result.")
    if result.get("ok") is not True:
        detail = str(result.get("err") or "").strip()
        normalized = detail.casefold()
        if "-128" in normalized or "user canceled" in normalized or "user cancelled" in normalized:
            return None
        raise RuntimeError(f"The macOS {capability.casefold()} picker failed: {detail or 'unknown error'}")
    selected = str(result.get("out") or "").strip()
    if not selected:
        raise RuntimeError(f"The macOS {capability.casefold()} picker returned an empty path.")
    return selected


def desktop_path_status(path: str | Path) -> dict[str, object]:
    """Return a JSON-safe status payload for a local desktop path.

    Args:
        path: File or directory path to describe. Missing paths are valid and
            return a non-openable status so GUI clients can explain the state
            before an opener command fails.

    Returns:
        Payload matching `paradev.desktop.path-status.v1`.

    Raises:
        ValueError: If the path is empty.
    """

    clean_path = _normalized_optional_text(path)
    if clean_path is None:
        raise ValueError("Path status path is required.")
    resolved_path = Path(clean_path).expanduser().resolve(strict=False)
    exists = resolved_path.exists()
    kind = _desktop_path_kind(resolved_path, exists)
    readable = exists and os.access(resolved_path, os.R_OK)
    openable = exists and kind in {"directory", "file"}
    return {
        "schema": "paradev.desktop.path-status.v1",
        "inputPath": clean_path,
        "path": str(resolved_path),
        "exists": exists,
        "kind": kind,
        "readable": readable,
        "openable": openable,
    }


def _build_mode(value: str | None) -> str:
    clean = _normalized_optional_text(value)
    if clean in {None, "cached"}:
        return "cached"
    if clean == "full":
        return "full"
    raise ValueError(f"Unsupported build mode: {clean}")


def _configured_build_strict_metadata(value: bool | None) -> bool:
    if value is not None:
        if type(value) is not bool:
            raise ValueError("build strict metadata must be a boolean.")
        return value
    configured = CM_PARADEV.get(BUILD_STRICT_METADATA_CONFIG_KEY, default=False)
    if type(configured) is bool:
        return configured
    clean = _normalized_optional_text(configured)
    if clean is None or clean.lower() in {"0", "false", "no", "off"}:
        return False
    if clean.lower() in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("paradev.build.strict_metadata must be a boolean.")


def _push_build_target_args(tokens: list[str], target: Mapping[str, object] | None) -> None:
    if target is None:
        return
    if not isinstance(target, Mapping):
        raise ValueError("build target must be a JSON object.")
    target_id = _required_text(target.get("id"), "build target id")
    target_kind = _required_text(target.get("kind"), "build target kind")
    if target_kind == "module":
        tokens.extend(["--module", target_id])
        return
    if target_kind == "collection":
        tokens.extend(["--collection", target_id])
        if family := _normalized_optional_text(target.get("family")):
            tokens.extend(["--family", family])
        return
    if target_kind == "family":
        tokens.extend(["--family", target_id])
        return
    raise ValueError(f"Unsupported build target kind: {target_kind}")


def _hoi4_launch_mode(value: str | None) -> str:
    clean = _normalized_optional_text(value)
    if clean in {None, "steam"}:
        return "steam"
    if clean == "local":
        return "local"
    raise ValueError(f"Unsupported HOI4 launch mode: {clean}")


def _configured_hoi4_launch_mode(value: str | None) -> str:
    clean = _normalized_optional_text(value)
    if clean is not None:
        return _hoi4_launch_mode(clean)
    configured = CM_PARADEV.get(HOI4_LAUNCH_MODE_CONFIG_KEY, default="steam")
    return _hoi4_launch_mode(_normalized_optional_text(configured))


def _hoi4_launch_target_for_mode(mode: str, game_root: str | Path | None, platform: str | None) -> str:
    if _hoi4_launch_mode(mode) == "steam":
        return HOI4_STEAM_LAUNCH_URL
    return _hoi4_launch_target(_resolve_hoi4_game_root(game_root, platform), platform)


def _resolve_hoi4_game_root(game_root: str | Path | None, platform: str | None) -> str:
    if root := _configured_hoi4_game_root(game_root):
        if root.startswith("steam://"):
            raise ValueError(
                "Local app launch mode requires an installed HOI4 app or executable, not a steam:// URL. "
                "Choose the install folder in Settings > Projects, or use Steam launcher mode."
            )
        path = Path(root).expanduser()
        if not path.exists():
            raise ValueError(
                f"Hearts of Iron IV game root does not exist: {path}. " "Choose an existing install folder in Settings > Projects, or use Steam launcher mode."
            )
        return str(path)
    default_root = Path.home() / HOI4_MACOS_GAME_ROOT_SUFFIX
    if _platform_id(platform) == "macos" and default_root.exists():
        return str(default_root)
    raise ValueError("Hearts of Iron IV game root is required for Local app launch mode. " "Set it in Settings > Projects, or use Steam launcher mode.")


def _configured_hoi4_game_root(value: str | Path | None) -> str | None:
    if root := _normalized_optional_text(value):
        return root
    configured = CM_PARADEV.get(HOI4_GAME_ROOT_CONFIG_KEY, default="")
    return _normalized_optional_text(configured)


def _hoi4_launch_target(game_root: str, platform: str | None) -> str:
    root = Path(game_root)
    platform_id = _platform_id(platform)
    if platform_id == "macos":
        if root.name.casefold() in _HOI4_MACOS_APP_NAMES and _is_launchable_macos_app(root):
            return str(root)
        if _is_named_executable(root, _HOI4_MACOS_EXECUTABLE_NAMES):
            return str(root)
        for candidate in _HOI4_MACOS_APP_NAMES:
            app = root / candidate
            if _is_launchable_macos_app(app):
                return str(app)
        for candidate in sorted(_HOI4_MACOS_EXECUTABLE_NAMES):
            executable = root / candidate
            if _is_named_executable(executable, _HOI4_MACOS_EXECUTABLE_NAMES):
                return str(executable)
        raise ValueError(
            f"Hearts of Iron IV path is not launchable on macOS: {root}. "
            "Expected dowser.app or hoi4.app with an executable under Contents/MacOS, "
            "or an executable named dowser or hoi4. Choose the HOI4 install in Settings > Projects."
        )
    if platform_id == "windows":
        executable = root if root.name.casefold() == "hoi4.exe" else root / "hoi4.exe"
        if executable.is_file():
            return str(executable)
        raise ValueError(
            f"Hearts of Iron IV path is not launchable on Windows: {root}. "
            "Expected hoi4.exe in the selected install folder. Choose the HOI4 install in Settings > Projects."
        )
    if platform_id == "linux":
        if _is_named_executable(root, _HOI4_LINUX_EXECUTABLE_NAMES):
            return str(root)
        executable = root / "hoi4"
        if _is_named_executable(executable, _HOI4_LINUX_EXECUTABLE_NAMES):
            return str(executable)
        raise ValueError(
            f"Hearts of Iron IV path is not launchable on Linux: {root}. "
            "Expected an executable named hoi4 in the selected install folder. "
            "Choose the HOI4 install in Settings > Projects."
        )
    raise ValueError(f"Local HOI4 launch is not supported on platform {_platform_id(platform)!r}. " "Use a supported macOS, Windows, or Linux installation.")


def _launcher_command(launch_target: str, platform: str | None) -> list[str]:
    platform_id = _platform_id(platform)
    if platform_id == "macos":
        if launch_target.startswith("steam://") or Path(launch_target).suffix.casefold() == ".app":
            return ["open", launch_target]
        return [launch_target]
    if platform_id == "windows":
        return ["cmd", "/C", "start", "", launch_target]
    if platform_id == "linux":
        return ["xdg-open", launch_target] if launch_target.startswith("steam://") else [launch_target]
    raise ValueError(f"HOI4 launch is not supported on platform {platform_id!r}. " "Use a supported macOS, Windows, or Linux installation.")


def _is_named_executable(path: Path, names: frozenset[str]) -> bool:
    return path.name.casefold() in names and path.is_file() and os.access(path, os.X_OK)


def _is_launchable_macos_app(path: Path) -> bool:
    if not path.is_dir():
        return False
    executable_root = path / "Contents" / "MacOS"
    try:
        return any(child.is_file() and os.access(child, os.X_OK) for child in executable_root.iterdir())
    except OSError:
        return False


def _require_steam_launch_available(platform: str | None) -> None:
    platform_id = _platform_id(platform)
    problem = _steam_launch_problem(platform_id)
    if problem is None:
        return
    raise ValueError(
        f"Steam is not available for HOI4 launch on {platform_id}: {problem}. "
        "Install or repair Steam and its steam:// URL handler, or choose Local app launch mode "
        "and select a launchable HOI4 install in Settings > Projects."
    )


def _steam_launch_problem(platform: str) -> str | None:
    if platform == "macos":
        opener = shutil.which("open")
        if opener is None:
            return "the macOS open command is unavailable"
        try:
            available = cmd(
                [opener, "-Ra", "Steam"],
                include="ok",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return f"Steam application registration could not be checked ({error})"
        return None if available else "the Steam application is not registered with macOS"
    if platform == "windows":
        return _windows_steam_launch_problem()
    if platform == "linux":
        opener = shutil.which("xdg-open")
        if opener is None:
            return "xdg-open is unavailable"
        mime_tool = shutil.which("xdg-mime")
        if mime_tool is None:
            return "xdg-mime is unavailable, so the steam:// URL handler cannot be verified"
        try:
            result = cmd(
                [mime_tool, "query", "default", "x-scheme-handler/steam"],
                include=("ok", "out"),
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return f"the steam:// URL handler could not be checked ({error})"
        if not isinstance(result, Mapping) or not result.get("ok") or not _normalized_optional_text(result.get("out")):
            return "no steam:// URL handler is registered"
        return None
    return f"platform {platform!r} is unsupported"


def _windows_steam_launch_problem() -> str | None:
    try:
        import winreg
    except ImportError:
        return "the Windows registry is unavailable"

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return "no steam:// URL handler is registered"
    executable = _windows_registry_command_executable(command)
    if executable is None:
        return "the registered steam:// handler has no executable"
    executable_path = Path(executable)
    if executable_path.is_file() or shutil.which(executable):
        return None
    return f"the registered Steam client does not exist at {executable_path}"


def _windows_registry_command_executable(command: object) -> str | None:
    clean = _normalized_optional_text(command)
    if clean is None:
        return None
    expanded = os.path.expandvars(clean)
    if expanded.startswith('"'):
        closing_quote = expanded.find('"', 1)
        return expanded[1:closing_quote] if closing_quote > 1 else None
    return expanded.split(maxsplit=1)[0]


def _open_path_target(value: str | None, platform: str) -> str:
    clean = _normalized_optional_text(value)
    if clean is None or clean == "default":
        return _default_open_path_target(platform)
    normalized = {
        "sublime_text": "sublimeText",
        "sublimetext": "sublimeText",
        "iterm": "iterm2",
        "iterm_2": "iterm2",
    }.get(clean, clean)
    supported = {str(row["id"]) for row in OPEN_PATH_TARGET_ROWS}
    if normalized not in supported:
        raise ValueError(f"Unsupported open path target: {clean}")
    return normalized


def _default_open_path_target(platform: str) -> str:
    return OPEN_PATH_DEFAULT_TARGETS.get(platform, OPEN_PATH_DEFAULT_TARGETS["unknown"])


def _open_path_target_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "labelKey": str(row["labelKey"]),
        "platforms": [str(platform) for platform in row["platforms"]],
    }


def _desktop_path_kind(path: Path, exists: bool) -> str:
    if not exists:
        return "missing"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def _macos_open_app_command(display_name: str, app_name: str | None, path: str, platform: str) -> list[str]:
    if platform != "macos":
        raise ValueError(f"{display_name} opener is only available on macOS.")
    return ["open", "-a", app_name, path] if app_name else ["open", path]


def _windows_command(display_name: str, command: list[str], platform: str) -> list[str]:
    if platform != "windows":
        raise ValueError(f"{display_name} opener is only available on Windows.")
    return command


def _platform_id(value: str | None) -> str:
    clean = _normalized_optional_text(value)
    raw = clean or sys.platform
    normalized = raw.lower()
    if normalized in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if normalized.startswith("win") or normalized == "windows":
        return "windows"
    if normalized.startswith("linux"):
        return "linux"
    return "unknown"


def _normalized_optional_text(value: object) -> str | None:
    clean = str(value).strip() if value is not None else ""
    return clean or None


def _required_text(value: object, label: str) -> str:
    clean = _normalized_optional_text(value)
    if clean is None:
        raise ValueError(f"{label} cannot be empty.")
    return clean
