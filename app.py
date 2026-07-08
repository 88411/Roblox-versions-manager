from __future__ import annotations

import json
import csv
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
import tkinter.messagebox as messagebox
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import customtkinter as ctk
except ModuleNotFoundError:
    ctk = None

try:
    import requests
except ModuleNotFoundError:
    requests = None


APP_NAME = "Roblox Version Manager"
RBXCDN_HOST = "https://setup-aws.rbxcdn.com"
DEPLOY_HISTORY_URL = "https://setup-rbxcdn.github.io/DeployHistory.txt"
ROBLOX_EXE = "RobloxPlayerBeta.exe"
VERSION_PREFIX = "version-"
MAX_DROPDOWN_ITEMS = 5
DEFAULT_CHANNEL = "LIVE"
ROBLOX_PROCESS_NAMES = (
    "RobloxPlayerBeta.exe",
    "RobloxPlayerLauncher.exe",
    "RobloxStudioBeta.exe",
    "RobloxStudioLauncherBeta.exe",
    "RobloxApplicationBeta.exe",
    "RobloxCrashHandler.exe",
)

PLAYER_EXTRACT_ROOTS: dict[str, str] = {
    "RobloxApp.zip": "",
    "redist.zip": "",
    "shaders.zip": "shaders/",
    "ssl.zip": "ssl/",
    "WebView2.zip": "",
    "WebView2RuntimeInstaller.zip": "WebView2RuntimeInstaller/",
    "content-avatar.zip": "content/avatar/",
    "content-configs.zip": "content/configs/",
    "content-fonts.zip": "content/fonts/",
    "content-sky.zip": "content/sky/",
    "content-sounds.zip": "content/sounds/",
    "content-textures2.zip": "content/textures/",
    "content-models.zip": "content/models/",
    "content-platform-fonts.zip": "PlatformContent/pc/fonts/",
    "content-platform-dictionaries.zip": "PlatformContent/pc/shared_compression_dictionaries/",
    "content-terrain.zip": "PlatformContent/pc/terrain/",
    "content-textures3.zip": "PlatformContent/pc/textures/",
    "extracontent-luapackages.zip": "ExtraContent/LuaPackages/",
    "extracontent-translations.zip": "ExtraContent/translations/",
    "extracontent-models.zip": "ExtraContent/models/",
    "extracontent-textures.zip": "ExtraContent/textures/",
    "extracontent-places.zip": "ExtraContent/places/",
}


class VersionManagerError(Exception):
    """Raised for expected user-facing workflow failures."""


@dataclass(frozen=True)
class RobloxVersion:
    hash: str
    released_at: str
    file_version: str

    @property
    def folder_name(self) -> str:
        return f"{VERSION_PREFIX}{self.hash}"

    @property
    def label(self) -> str:
        return f"{self.folder_name} ({self.released_at})"


def runtime_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class RunLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"run_{timestamp_slug()}.log"
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")
        self.write(f"Log started: {self.path}")

    def write(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        with self._lock:
            self._handle.write(line)
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": subprocess.STARTUPINFO(),
    }


def list_running_roblox_processes(exclude_names: set[str] | None = None) -> list[str]:
    if os.name != "nt":
        return []
    exclude_names = {name.lower() for name in (exclude_names or set())}
    names: set[str] = set()
    completed = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    for row in csv.reader(completed.stdout.splitlines()):
        if not row:
            continue
        name = row[0].strip('"')
        lowered = name.lower()
        if lowered in exclude_names:
            continue
        if lowered.startswith("roblox") and lowered.endswith(".exe"):
            names.add(name)

    ps_script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'Roblox*' -or ($_.ExecutablePath -and $_.ExecutablePath -like '*\\Roblox*') } | "
        "ForEach-Object { $_.Name }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    for line in completed.stdout.splitlines():
        name = line.strip()
        lowered = name.lower()
        if lowered in exclude_names:
            continue
        if lowered.startswith("roblox") and lowered.endswith(".exe"):
            names.add(name)

    return sorted(names)


def close_running_roblox_processes(
    log: Callable[[str], None],
    wait_seconds: int = 10,
    exclude_names: set[str] | None = None,
) -> bool:
    if os.name != "nt":
        return False
    exclude_names = exclude_names or set()
    running = list_running_roblox_processes(exclude_names)
    if not running:
        return False

    log("Roblox processes detected. Closing them now...")
    seen: set[str] = set()
    for name in sorted(running):
        if name in seen:
            continue
        seen.add(name)
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", name],
            capture_output=True,
            text=True,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0:
            log(f"Closed {name}.")
        else:
            log(f"Could not close {name}: {completed.stdout.strip() or completed.stderr.strip()}")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not list_running_roblox_processes(exclude_names):
            return True
        time.sleep(0.5)
    still_running = list_running_roblox_processes(exclude_names)
    if still_running:
        log(f"Still running after close attempt: {', '.join(still_running)}")
    return not still_running


def default_versions_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise VersionManagerError("LOCALAPPDATA is not available on this Windows account.")
    return Path(local_app_data) / "Roblox" / "Versions"


def default_cache_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", tempfile.gettempdir())
    return Path(local_app_data) / "RobloxVersionManager" / "cache"


def default_log_dir() -> Path:
    return runtime_dir() / "logs"


def parse_deploy_history(text: str, limit: int = MAX_DROPDOWN_ITEMS) -> list[RobloxVersion]:
    pattern = re.compile(
        r"New\s+WindowsPlayer\s+version-([0-9a-fA-F]+)\s+at\s+"
        r"([^,]+),\s+file\s+versi?on:\s+([0-9,\s]+)",
        re.IGNORECASE,
    )
    matches = pattern.findall(text)
    versions: list[RobloxVersion] = []
    seen_hashes: set[str] = set()

    for version_hash, released_at, file_version in reversed(matches):
        normalized_hash = version_hash.lower()
        if normalized_hash in seen_hashes:
            continue
        seen_hashes.add(normalized_hash)
        version_parts = [part.strip() for part in file_version.split(",") if part.strip().isdigit()]
        normalized_file_version = ".".join(version_parts)
        versions.append(
            RobloxVersion(
                hash=normalized_hash,
                released_at=released_at.strip(),
                file_version=normalized_file_version,
            )
        )
        if len(versions) >= limit:
            break

    if not versions:
        raise VersionManagerError("No WindowsPlayer versions were found in DeployHistory.")
    return versions


def fetch_versions() -> list[RobloxVersion]:
    if requests is None:
        raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
    response = requests.get(DEPLOY_HISTORY_URL, timeout=30)
    if response.status_code >= 400:
        raise VersionManagerError(f"DeployHistory request failed with HTTP {response.status_code}.")
    return parse_deploy_history(response.text)


def find_latest_version_folder(versions_dir: Path) -> Path | None:
    if not versions_dir.exists():
        return None
    candidates = [
        child
        for child in versions_dir.iterdir()
        if child.is_dir() and child.name.startswith(VERSION_PREFIX)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda child: child.stat().st_mtime)


def ensure_safe_versions_dir(versions_dir: Path) -> None:
    resolved = versions_dir.resolve()
    local_app_data = os.getenv("LOCALAPPDATA")
    local_root = Path(local_app_data).resolve() if local_app_data else None
    expected_tail = ("Roblox", "Versions")
    if (
        len(resolved.parts) < 4
        or resolved.parts[-2:] != expected_tail
        or local_root is None
        or local_root not in (resolved, *resolved.parents)
    ):
        raise VersionManagerError(
            "The target folder must be the Roblox Versions folder under LOCALAPPDATA."
        )
    resolved.mkdir(parents=True, exist_ok=True)


def safe_remove_old_versions(versions_dir: Path, keep_dir: Path, log: Callable[[str], None]) -> None:
    keep_dir = keep_dir.resolve()
    removed = 0
    for child in versions_dir.iterdir():
        if not child.is_dir() or not child.name.startswith(VERSION_PREFIX):
            continue
        if child.resolve() == keep_dir:
            continue
        if child.resolve().parent != versions_dir.resolve():
            continue
        shutil.rmtree(child)
        removed += 1
        log(f"Removed old version folder: {child.name}")
    if removed == 0:
        log("No old version folders needed cleanup.")


def build_version_path(version: RobloxVersion, channel: str = DEFAULT_CHANNEL) -> str:
    if channel == DEFAULT_CHANNEL:
        return f"{RBXCDN_HOST}/version-{version.hash}-"
    channel_path = f"{RBXCDN_HOST}/channel/{channel.lower()}"
    return f"{channel_path}/version-{version.hash}-"


def fetch_manifest_text(version: RobloxVersion, log: Callable[[str], None], channel: str = DEFAULT_CHANNEL) -> tuple[str, str]:
    if requests is None:
        raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
    version_path = build_version_path(version, channel)
    manifest_candidates = [
        (f"{version_path}rbxPkgManifest.txt", version_path),
        (
            f"{RBXCDN_HOST}/channel/common/version-{version.hash}-rbxPkgManifest.txt",
            f"{RBXCDN_HOST}/channel/common/version-{version.hash}-",
        ),
    ]

    for manifest_url, resolved_version_path in manifest_candidates:
        log(f"Fetching manifest: {manifest_url}")
        response = requests.get(manifest_url, timeout=60)
        if response.status_code == 200:
            log(f"Manifest loaded successfully: {response.headers.get('Content-Type', 'unknown')}")
            return response.text, resolved_version_path
        log(f"Manifest request failed with HTTP {response.status_code}.")

    raise VersionManagerError("Could not fetch rbxPkgManifest.txt from Roblox CDN.")


def download_file_to_path(url: str, destination: Path, log: Callable[[str], None], timeout: int = 120) -> tuple[requests.Response, Path]:
    if requests is None:
        raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as response:
        if response.status_code >= 400:
            raise VersionManagerError(f"Download failed with HTTP {response.status_code}: {url}")
        log(f"Response type: {response.headers.get('Content-Type', 'unknown')}")
        downloaded = 0
        with temp_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                file.write(chunk)
                downloaded += len(chunk)
        log(f"Saved {destination.name} ({downloaded} bytes)")
        temp_path.replace(destination)
        return response, destination


def stage_windows_player_deployment(
    version: RobloxVersion,
    cache_dir: Path,
    stage_dir: Path,
    log: Callable[[str], None],
    progress: Callable[[float], None],
) -> None:
    manifest_text, version_path = fetch_manifest_text(version, log)
    manifest_lines = [line.strip() for line in manifest_text.splitlines() if line.strip()]
    if not manifest_lines or manifest_lines[0] != "v0":
        raise VersionManagerError("Unknown rbxPkgManifest format.")

    package_names = [line for line in manifest_lines[1:] if line.endswith(".zip")]
    if not package_names:
        raise VersionManagerError("No package zips were listed in the manifest.")

    cache_root = cache_dir / version.folder_name
    cache_root.mkdir(parents=True, exist_ok=True)
    total_packages = len(package_names)
    log(f"Manifest contains {total_packages} package(s).")

    for index, package_name in enumerate(package_names, start=1):
        package_url = f"{version_path}{package_name}"
        cached_package = cache_root / package_name
        cache_ok = cached_package.exists() and cached_package.stat().st_size > 0
        if cache_ok and package_name.endswith(".zip"):
            cache_ok = zipfile.is_zipfile(cached_package)
        if cache_ok:
            log(f"Using cached package: {package_name}")
        else:
            log(f"Downloading package {index}/{total_packages}: {package_name}")
            try:
                response, downloaded_path = download_file_to_path(package_url, cached_package, log)
            except Exception as exc:
                raise VersionManagerError(f"Failed to download {package_name}: {exc}") from exc

            response_headers = dict(response.headers)
            response_url = response.url
            if not zipfile.is_zipfile(downloaded_path):
                invalid_dir = log_dir_for_download()
                invalid_dir.mkdir(parents=True, exist_ok=True)
                artifact_base = invalid_dir / f"{version.folder_name}_{package_name}_{timestamp_slug()}"
                metadata_path = artifact_base.with_suffix(".json")
                payload_path = artifact_base.with_suffix(".bin")
                metadata_path.write_text(
                    json.dumps(
                        {
                            "url": response_url,
                            "status_code": response.status_code,
                            "headers": response_headers,
                            "content_type": response_headers.get("Content-Type"),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                shutil.copy2(downloaded_path, payload_path)
                raise VersionManagerError(
                    f"Package {package_name} was not a zip archive. Saved the raw response to {payload_path.parent}."
                )

        if package_name in PLAYER_EXTRACT_ROOTS:
            extract_root = PLAYER_EXTRACT_ROOTS[package_name]
            destination = stage_dir / extract_root
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(cached_package) as package_zip:
                safe_extract_zipfile(package_zip, destination)
        else:
            shutil.copy2(cached_package, stage_dir / package_name)

        progress(0.05 + (index / total_packages) * 0.8)


def safe_extract_zipfile(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        filename = member.filename.replace("\\", "/").lstrip("/")
        if not filename or filename in {".", "/"} or member.is_dir():
            continue
        member_path = Path(filename)
        if ".." in member_path.parts:
            raise VersionManagerError(f"Unsafe path in zip archive: {member.filename}")
        resolved_target = (destination / member_path).resolve()
        if destination not in (resolved_target, *resolved_target.parents):
            raise VersionManagerError(f"Unsafe path in zip archive: {member.filename}")
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, resolved_target.open("wb") as target:
            shutil.copyfileobj(source, target)


def log_dir_for_download() -> Path:
    failed_dir = default_log_dir() / "failed_downloads"
    failed_dir.mkdir(parents=True, exist_ok=True)
    return failed_dir


def copy_overwrite(source: Path, destination: Path) -> None:
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            copy_overwrite(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def copy_with_retry(source: Path, destination: Path, log: Callable[[str], None], retries: int = 1) -> None:
    for attempt in range(retries + 1):
        try:
            copy_overwrite(source, destination)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) != 32 or attempt >= retries:
                raise
            log("A Roblox file is still locked. Closing Roblox again and retrying...")
            close_running_roblox_processes(log, wait_seconds=15)
            time.sleep(2)


def rename_active_folder(active_dir: Path, target_name: str) -> Path:
    target_dir = active_dir.parent / target_name
    if active_dir.resolve() == target_dir.resolve():
        return active_dir
    if target_dir.exists():
        raise VersionManagerError(f"Cannot rename folder because {target_name} already exists.")
    active_dir.rename(target_dir)
    return target_dir


def install_version(
    version: RobloxVersion,
    versions_dir: Path,
    cache_dir: Path,
    log: Callable[[str], None],
    progress: Callable[[float], None],
) -> Path:
    ensure_safe_versions_dir(versions_dir)
    updater_name = Path(sys.executable).name if getattr(sys, "frozen", False) else Path(__file__).name
    if close_running_roblox_processes(log, wait_seconds=15, exclude_names={updater_name}):
        log("Roblox was closed automatically before updating.")
    else:
        log("No running Roblox processes were detected.")

    active_dir = find_latest_version_folder(versions_dir)
    if active_dir is None:
        active_dir = versions_dir / version.folder_name
        active_dir.mkdir(parents=True, exist_ok=True)
        log(f"Created Roblox version folder: {active_dir.name}")
    else:
        log(f"Using newest local folder: {active_dir.name}")

    with tempfile.TemporaryDirectory(prefix="rvm_extract_") as temp_name:
        extract_dir = Path(temp_name)
        stage_windows_player_deployment(version, cache_dir, extract_dir, log, progress)
        log("Copying files into the active Roblox folder...")
        copy_with_retry(extract_dir, active_dir, log, retries=1)
        progress(0.9)

    target_dir = versions_dir / version.folder_name
    if target_dir.resolve() != active_dir.resolve() and target_dir.exists():
        if target_dir.is_dir() and target_dir.resolve().parent == versions_dir.resolve():
            shutil.rmtree(target_dir)
            log(f"Removed existing target folder: {target_dir.name}")
        else:
            raise VersionManagerError(
                f"Cannot replace existing path: {target_dir}"
            )

    final_dir = rename_active_folder(active_dir, version.folder_name)
    safe_remove_old_versions(versions_dir, final_dir, log)
    progress(1.0)
    return final_dir


def get_desktop_dir() -> Path:
    onedrive_desktop = os.getenv("OneDriveConsumer") or os.getenv("OneDrive")
    if onedrive_desktop:
        candidate = Path(onedrive_desktop) / "Desktop"
        if candidate.exists():
            return candidate
    return Path.home() / "Desktop"


def create_desktop_shortcut(version_dir: Path) -> Path:
    exe_path = version_dir / ROBLOX_EXE
    if not exe_path.exists():
        raise VersionManagerError(f"{ROBLOX_EXE} was not found in {version_dir.name}.")

    desktop = get_desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut_path = desktop / "Roblox Player.lnk"
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shortcut = $shell.CreateShortcut('{escape_powershell(shortcut_path)}'); "
        f"$shortcut.TargetPath = '{escape_powershell(exe_path)}'; "
        f"$shortcut.WorkingDirectory = '{escape_powershell(version_dir)}'; "
        "$shortcut.Save()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    if completed.returncode != 0:
        raise VersionManagerError("Windows could not create the desktop shortcut.")
    return shortcut_path


def escape_powershell(path: Path) -> str:
    return str(path).replace("'", "''")


def launch_roblox(version_dir: Path) -> None:
    exe_path = version_dir / ROBLOX_EXE
    if not exe_path.exists():
        raise VersionManagerError(f"{ROBLOX_EXE} was not found in {version_dir.name}.")
    subprocess.Popen(
        [str(exe_path)],
        cwd=str(version_dir),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def format_folder_status(folder: Path | None) -> str:
    if folder is None:
        return "Current local version: not found"
    timestamp = datetime.fromtimestamp(folder.stat().st_mtime).strftime("%m/%d/%Y %I:%M %p")
    return f"Current local version: {folder.name} ({timestamp})"


class App(ctk.CTk if ctk is not None else object):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(APP_NAME)
        self.geometry("560x500")
        self.minsize(520, 470)
        self.resizable(False, False)

        self.versions_dir = default_versions_dir()
        self.cache_dir = default_cache_dir()
        self.log_dir = default_log_dir()
        self.logger = RunLogger(self.log_dir)
        self.versions: list[RobloxVersion] = []
        self.selected_label = ctk.StringVar(value="Loading versions...")
        self.launch_after_update = ctk.BooleanVar(value=False)
        self.status_text = ctk.StringVar(value="Ready")
        self.current_text = ctk.StringVar(value="Current local version: checking...")
        self.latest_text = ctk.StringVar(value="Latest Roblox version: checking...")
        self.log_path_text = ctk.StringVar(value=f"Logs: {self.logger.path.name}")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.build_ui()
        self.refresh_local_status()
        self.after(100, self.drain_events)
        self.refresh_versions()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        self.configure(fg_color="#101214")
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(self, text=APP_NAME, font=ctk.CTkFont(size=23, weight="bold"))
        header.grid(row=0, column=0, sticky="w", padx=24, pady=(22, 4))

        subtitle = ctk.CTkLabel(
            self,
            text="Pick a WindowsPlayer build and apply it to your local Roblox install.",
            text_color="#9da7b1",
            font=ctk.CTkFont(size=12),
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=24, pady=(0, 18))

        panel = ctk.CTkFrame(self, corner_radius=14, fg_color="#181c20")
        panel.grid(row=2, column=0, sticky="ew", padx=22, pady=0)
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(panel, text="Requested version", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=18, pady=(16, 6)
        )
        self.version_menu = ctk.CTkOptionMenu(
            panel,
            variable=self.selected_label,
            values=["Loading versions..."],
            height=36,
            corner_radius=10,
            dynamic_resizing=False,
        )
        self.version_menu.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))

        current = ctk.CTkLabel(panel, textvariable=self.current_text, anchor="w", text_color="#d4dae0")
        current.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 2))

        latest = ctk.CTkLabel(
            panel,
            textvariable=self.latest_text,
            anchor="w",
            text_color="#8b949e",
            font=ctk.CTkFont(size=11),
        )
        latest.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 14))

        self.progress = ctk.CTkProgressBar(panel, height=12, corner_radius=6)
        self.progress.grid(row=4, column=0, sticky="ew", padx=18, pady=(2, 12))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(panel, textvariable=self.status_text, anchor="w", text_color="#9da7b1")
        self.status_label.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 16))

        self.log_path_label = ctk.CTkLabel(
            self,
            textvariable=self.log_path_text,
            anchor="w",
            text_color="#8b949e",
            font=ctk.CTkFont(size=11),
        )
        self.log_path_label.grid(row=3, column=0, sticky="w", padx=24, pady=(12, 0))

        options = ctk.CTkFrame(self, fg_color="transparent")
        options.grid(row=4, column=0, sticky="ew", padx=22, pady=(14, 0))
        options.grid_columnconfigure(0, weight=1)

        self.launch_check = ctk.CTkCheckBox(
            options,
            text="Launch Roblox after update",
            variable=self.launch_after_update,
            corner_radius=6,
        )
        self.launch_check.grid(row=0, column=0, sticky="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=22, pady=(16, 22))
        buttons.grid_columnconfigure(0, weight=1)

        self.logs_button = ctk.CTkButton(
            buttons,
            text="Open Logs Folder",
            command=self.open_logs_folder_clicked,
            height=38,
            corner_radius=10,
            fg_color="#2b3138",
            hover_color="#39414a",
        )
        self.logs_button.grid(row=0, column=0, padx=(0, 10))

        self.shortcut_button = ctk.CTkButton(
            buttons,
            text="Update Desktop Shortcut",
            command=self.update_shortcut_clicked,
            height=38,
            corner_radius=10,
            fg_color="#2b3138",
            hover_color="#39414a",
        )
        self.shortcut_button.grid(row=0, column=1, padx=(0, 10))

        self.update_button = ctk.CTkButton(
            buttons,
            text="Apply Version",
            command=self.apply_clicked,
            height=38,
            corner_radius=10,
        )
        self.update_button.grid(row=0, column=2)

    def refresh_versions(self) -> None:
        self.set_busy(True, "Loading DeployHistory...")
        thread = threading.Thread(target=self.load_versions_worker, daemon=True)
        thread.start()

    def load_versions_worker(self) -> None:
        try:
            versions = fetch_versions()
            self.events.put(("versions", versions))
        except Exception as exc:
            self.events.put(("error", exc))

    def apply_clicked(self) -> None:
        version = self.selected_version()
        if version is None:
            messagebox.showwarning(APP_NAME, "Versions are still loading.")
            return
        launch_after_update = self.launch_after_update.get()
        self.set_busy(True, "Preparing update...")
        self.progress.set(0)
        thread = threading.Thread(
            target=self.install_worker,
            args=(version, launch_after_update),
            daemon=True,
        )
        thread.start()

    def install_worker(self, version: RobloxVersion, launch_after_update: bool) -> None:
        try:
            final_dir = install_version(
                version,
                self.versions_dir,
                self.cache_dir,
                log=self.queue_log,
                progress=lambda value: self.events.put(("progress", value)),
            )
            shortcut_path = create_desktop_shortcut(final_dir)
            if launch_after_update:
                launch_roblox(final_dir)
            self.events.put(("done", (final_dir, shortcut_path)))
        except Exception as exc:
            self.events.put(("error", exc))

    def update_shortcut_clicked(self) -> None:
        try:
            latest = find_latest_version_folder(self.versions_dir)
            if latest is None:
                raise VersionManagerError("No local Roblox version folder was found.")
            shortcut = create_desktop_shortcut(latest)
            self.status_text.set(f"Desktop shortcut updated: {shortcut.name}")
            self.append_log(f"Desktop shortcut updated: {shortcut}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def open_logs_folder_clicked(self) -> None:
        try:
            open_folder(self.log_dir)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the logs folder: {exc}")

    def selected_version(self) -> RobloxVersion | None:
        selected = self.selected_label.get()
        for version in self.versions:
            if version.label == selected:
                return version
        return self.versions[0] if self.versions else None

    def refresh_local_status(self) -> None:
        latest = find_latest_version_folder(self.versions_dir)
        self.current_text.set(format_folder_status(latest))

    def queue_log(self, message: str) -> None:
        self.events.put(("status", message))

    def append_log(self, message: str) -> None:
        self.logger.write(message)
        self.log_path_text.set(f"Logs: {self.logger.path.name}")
        self.status_text.set(message)

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        state = "disabled" if busy else "normal"
        self.logs_button.configure(state=state)
        self.update_button.configure(state=state)
        self.shortcut_button.configure(state=state)
        self.version_menu.configure(state=state)
        if status:
            self.status_text.set(status)

    def drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "versions":
                self.versions = payload
                labels = [version.label for version in self.versions]
                self.version_menu.configure(values=labels)
                self.selected_label.set(labels[0])
                latest = self.versions[0]
                self.latest_text.set(f"Latest Roblox version: {latest.folder_name} ({latest.file_version})")
                self.set_busy(False, "Ready")
            elif event == "status":
                self.append_log(str(payload))
            elif event == "progress":
                self.progress.set(float(payload))
            elif event == "done":
                final_dir, shortcut_path = payload
                self.refresh_local_status()
                self.append_log(f"Applied {final_dir.name}; shortcut updated.")
                self.set_busy(False)
            elif event == "error":
                self.set_busy(False, "Failed")
                self.append_log(f"Error: {payload}")
                messagebox.showerror(APP_NAME, str(payload))

        self.after(100, self.drain_events)

    def on_close(self) -> None:
        try:
            self.logger.write("Application closed.")
        finally:
            self.logger.close()
            self.destroy()


def main() -> None:
    try:
        if ctk is None:
            raise VersionManagerError(
                "customtkinter is not installed. Run: pip install -r requirements.txt"
            )
        if requests is None:
            raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
        app = App()
    except Exception as exc:
        messagebox.showerror(APP_NAME, str(exc))
        return
    app.mainloop()


if __name__ == "__main__":
    main()
