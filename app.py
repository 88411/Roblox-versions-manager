from __future__ import annotations

import json
import csv
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from tkinter import filedialog
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

try:
    import pystray
    from PIL import Image
except ModuleNotFoundError:
    pystray = None
    Image = None

APP_NAME = "Roblox Version Manager"
RBXCDN_HOST = "https://setup-aws.rbxcdn.com"
DEPLOY_HISTORY_URL = "https://setup-rbxcdn.github.io/DeployHistory.txt"
WEAO_API_BASE_URLS = (
    "https://weao.xyz",
    "https://weao.gg",
    "https://whatexpsare.online",
    "https://whatexploitsare.online",
)
WEAO_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "WEAO-3PService",
}
ROBLOX_EXE = "RobloxPlayerBeta.exe"
VERSION_PREFIX = "version-"
MAX_DROPDOWN_ITEMS = 5
DEFAULT_CHANNEL = "LIVE"
WEAO_CACHE_MAX_AGE_SECONDS = 86400
DEFAULT_RETAINED_VERSIONS = 5
HISTORY_LIMIT = 100
SETTINGS_FILENAME = "settings.json"
HISTORY_FILENAME = "sync_history.json"
BACKGROUND_MODE = "--background" in sys.argv
AUTO_SYNC_POLL_SECONDS = 2
WEAO_REQUEST_TIMEOUT_SECONDS = 6
ICON_FILENAME = "rvm.ico"
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


DEFAULT_SETTINGS: dict[str, Any] = {
    "versions_dir": "",
    "auto_sync": False,
    "start_minimized": False,
    "start_background_sync": False,
    "background_tray": False,
    "relaunch_after_auto_sync": False,
    "preserve_versions": True,
    "retained_versions": DEFAULT_RETAINED_VERSIONS,
    "favorites": [],
    "saved_profiles": [],
    "selected_product_id": "",
    "sort_mode": "Type",
    "product_filter": "All products",
    "type_filter": "All",
    "auto_cache_favorites": False,
}


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
        if self.released_at:
            return f"{self.folder_name} ({self.released_at})"
        return self.folder_name


@dataclass(frozen=True)
class WEAOProduct:
    product_id: str
    title: str
    version: str
    rbx_version: str
    updated_at: str
    hidden: bool
    platform: str
    extype: str
    website_url: str
    discord_url: str
    free: bool
    decompiler: bool
    multi_inject: bool
    sunc_percentage: int | float | None
    unc_percentage: int | float | None
    element_certified: bool
    description: str
    index: int

    @property
    def version_hash(self) -> str:
        return normalize_version_hash(self.rbx_version)

    @property
    def label(self) -> str:
        return f"{self.title}  /  {self.version}"

    @property
    def build_label(self) -> str:
        return f"version-{self.version_hash}"

def product_sort_key(product: WEAOProduct, sort_mode: str) -> tuple[Any, ...]:
    if sort_mode == "Type":
        return (product.extype.lower(), product.title.lower())
    if sort_mode == "sUNC":
        return (-(product.sunc_percentage or -1), product.title.lower())
    if sort_mode == "Updated":
        return (product.updated_at, product.title.lower())
    return (product.index, product.title.lower())


def product_type_label(product: WEAOProduct) -> str:
    raw_type = product.extype.casefold()
    if "executor" in raw_type:
        return "Executer"
    return "External"


def normalize_version_hash(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith(VERSION_PREFIX):
        normalized = normalized[len(VERSION_PREFIX):]
    if not re.fullmatch(r"[0-9a-fA-F]+", normalized):
        raise VersionManagerError(f"Invalid Roblox version hash: {value}")
    return normalized.lower()


def runtime_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def resource_path(filename: str) -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    return Path(bundle_dir) / filename if bundle_dir else runtime_dir() / filename


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
        **hidden_subprocess_kwargs(),
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
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "RobloxVersionManager" / "logs"
    return runtime_dir() / "logs"


def default_app_data_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", tempfile.gettempdir())
    path = Path(local_app_data) / "RobloxVersionManager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return default_app_data_dir() / SETTINGS_FILENAME


def history_path() -> Path:
    return default_app_data_dir() / HISTORY_FILENAME


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update(saved)
    except (OSError, ValueError):
        pass
    return normalize_settings(settings)


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temp_path.replace(path)


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(DEFAULT_SETTINGS)
    normalized.update(settings)
    try:
        normalized["retained_versions"] = max(1, min(20, int(normalized.get("retained_versions", DEFAULT_RETAINED_VERSIONS))))
    except (TypeError, ValueError):
        normalized["retained_versions"] = DEFAULT_RETAINED_VERSIONS
    normalized["favorites"] = [str(value) for value in normalized.get("favorites", []) if value is not None]
    normalized["auto_sync"] = bool(normalized.get("auto_sync"))
    normalized["preserve_versions"] = bool(normalized.get("preserve_versions", True))
    normalized["auto_cache_favorites"] = bool(normalized.get("auto_cache_favorites"))
    normalized["background_tray"] = bool(
        normalized.get("background_tray") or normalized["auto_cache_favorites"]
    )
    normalized.pop("safe_mode", None)
    normalized.pop("backup_before_sync", None)
    return normalized


def load_sync_history() -> list[dict[str, Any]]:
    try:
        saved = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return saved if isinstance(saved, list) else []


def save_sync_history(history: list[dict[str, Any]]) -> None:
    path = history_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(history[:HISTORY_LIMIT], indent=2), encoding="utf-8")
    temp_path.replace(path)


def product_cache_path() -> Path:
    return default_app_data_dir() / "weao-products.json"


def load_cached_products() -> list[WEAOProduct]:
    path = product_cache_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        product
        for index, item in enumerate(payload, start=1)
        if isinstance(item, dict)
        for product in [parse_weao_product(item, index)]
        if product is not None and not product.hidden
    ]


def save_product_cache(payload: list[dict[str, Any]]) -> None:
    path = product_cache_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    temp_path.replace(path)


def roblox_user_state_paths(versions_dir: Path, active_dir: Path) -> list[tuple[Path, Path]]:
    """Return Roblox user-state files and directories that must survive a client update."""
    root = versions_dir.parent
    paths: list[tuple[Path, Path]] = []
    for candidate in root.glob("GlobalBasicSettings*.xml"):
        if candidate.is_file():
            paths.append((candidate, Path("local_root") / candidate.name))
    for relative in (Path("LocalStorage"), Path("ClientSettings")):
        candidate = root / relative
        if candidate.exists():
            paths.append((candidate, Path("local_root") / relative))
    appdata_root = os.getenv("APPDATA")
    if appdata_root:
        appdata_roblox = Path(appdata_root) / "Roblox"
        for relative in (Path("LocalStorage"), Path("ClientSettings")):
            candidate = appdata_roblox / relative
            if candidate.exists():
                paths.append((candidate, Path("appdata") / relative))
    for relative in (Path("ClientSettings"), Path("AppSettings.xml"), Path("LocalStorage")):
        candidate = active_dir / relative
        if candidate.exists():
            paths.append((candidate, Path("active") / relative))
    return paths


def snapshot_roblox_user_state(
    versions_dir: Path,
    active_dir: Path,
    snapshot_dir: Path,
    log: Callable[[str], None],
) -> list[tuple[Path, Path, str, Path]]:
    snapshots: list[tuple[Path, Path, str, Path]] = []
    for source, relative in roblox_user_state_paths(versions_dir, active_dir):
        destination = snapshot_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        snapshots.append((source, destination, str(relative.parts[0]), Path(*relative.parts[1:])))
    if snapshots:
        log(f"Preserved {len(snapshots)} Roblox user-state item(s) during sync.")
    return snapshots


def restore_roblox_user_state(
    snapshots: list[tuple[Path, Path, str, Path]],
    versions_dir: Path,
    final_dir: Path,
    log: Callable[[str], None],
) -> None:
    for _original, snapshot, location, relative in snapshots:
        if location == "local_root":
            destination = versions_dir.parent / relative
        elif location == "appdata":
            appdata_root = os.getenv("APPDATA")
            if not appdata_root:
                continue
            destination = Path(appdata_root) / "Roblox" / relative
        else:
            destination = final_dir / relative
        if destination.exists() and destination.is_dir() and snapshot.is_file():
            shutil.rmtree(destination)
        elif destination.exists() and destination.is_file() and snapshot.is_dir():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(snapshot, destination)
        else:
            shutil.copy2(snapshot, destination)
    if snapshots:
        log("Restored Roblox user-state after sync.")


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
        normalized_hash = normalize_version_hash(version_hash)
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


def parse_weao_product(payload: dict[str, Any], index: int) -> WEAOProduct | None:
    if str(payload.get("platform") or "Windows").lower() != "windows":
        return None
    rbx_version = str(payload.get("rbxversion", "")).strip()
    try:
        normalize_version_hash(rbx_version)
    except VersionManagerError:
        return None
    slug = payload.get("slug") if isinstance(payload.get("slug"), dict) else {}
    return WEAOProduct(
        product_id=str(payload.get("_id") or payload.get("trackerId") or index),
        title=str(payload.get("title") or "Unnamed product"),
        version=str(payload.get("version") or "Unknown version"),
        rbx_version=rbx_version,
        updated_at=str(payload.get("updatedDate") or ""),
        hidden=bool(payload.get("hidden", False)),
        platform=str(payload.get("platform") or "Windows"),
        extype=str(payload.get("extype") or ""),
        website_url=str(payload.get("websitelink") or ""),
        discord_url=str(payload.get("discordlink") or ""),
        free=bool(payload.get("free", False)),
        decompiler=bool(payload.get("decompiler", False)),
        multi_inject=bool(payload.get("multiInject", False)),
        sunc_percentage=payload.get("suncPercentage") if isinstance(payload.get("suncPercentage"), (int, float)) else None,
        unc_percentage=payload.get("uncPercentage") if isinstance(payload.get("uncPercentage"), (int, float)) else None,
        element_certified=bool(payload.get("elementCertified", False)),
        description=str(slug.get("fullDescription") or ""),
        index=int(payload.get("index") or index),
    )


def fetch_weao_products() -> list[WEAOProduct]:
    if requests is None:
        raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
    last_error = "unknown error"
    errors: list[str] = []
    for base_url in WEAO_API_BASE_URLS:
        try:
            response = requests.get(
                f"{base_url}/api/status/exploits",
                headers=WEAO_HEADERS,
                timeout=WEAO_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                errors.append(f"{base_url}: {last_error}")
                continue
            payload = response.json()
            if not isinstance(payload, list):
                last_error = "unexpected response format"
                errors.append(f"{base_url}: {last_error}")
                continue
            products = [
                product
                for index, item in enumerate(payload, start=1)
                if isinstance(item, dict)
                for product in [parse_weao_product(item, index)]
                if product is not None and not product.hidden
            ]
            products.sort(key=lambda product: (product.index, product.title.lower()))
            if not products:
                raise VersionManagerError("WEAO returned no visible Windows products with valid Roblox builds.")
            save_product_cache(payload)
            return products
        except (requests.RequestException, ValueError, VersionManagerError, ssl.SSLError, socket.error) as exc:
            last_error = str(exc)
            errors.append(f"{base_url}: {last_error}")
    detail = "; ".join(errors[-3:]) if errors else last_error
    raise VersionManagerError(
        "Could not load WEAO products. Check your internet connection, VPN/proxy, or antivirus HTTPS inspection. "
        f"Tried {len(WEAO_API_BASE_URLS)} WEAO endpoints. ({detail})"
    )


def fetch_weao_products_with_cache() -> tuple[list[WEAOProduct], bool]:
    cached = load_cached_products()
    if cached:
        return cached, True
    return fetch_weao_products(), False


def fetch_weao_current_windows() -> str | None:
    if requests is None:
        return None
    for base_url in WEAO_API_BASE_URLS:
        try:
            response = requests.get(
                f"{base_url}/api/versions/current",
                headers=WEAO_HEADERS,
                timeout=WEAO_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
            value = payload.get("Windows") if isinstance(payload, dict) else None
            if value:
                return normalize_version_hash(str(value))
        except (requests.RequestException, ValueError, VersionManagerError, ssl.SSLError, socket.error):
            continue
    return None


def refresh_product_for_auto_sync(product: WEAOProduct) -> WEAOProduct:
    products = fetch_weao_products()
    for candidate in products:
        if candidate.product_id == product.product_id or candidate.title.casefold() == product.title.casefold():
            return candidate
    return product


def fetch_versions() -> list[RobloxVersion]:
    if requests is None:
        raise VersionManagerError("requests is not installed. Run: pip install -r requirements.txt")
    response = requests.get(DEPLOY_HISTORY_URL, timeout=30)
    if response.status_code >= 400:
        raise VersionManagerError(f"DeployHistory request failed with HTTP {response.status_code}.")
    return parse_deploy_history(response.text)


def version_from_hash(version_hash: str, file_version: str = "") -> RobloxVersion:
    return RobloxVersion(
        hash=normalize_version_hash(version_hash),
        released_at="",
        file_version=file_version,
    )


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


def local_version_hash(versions_dir: Path) -> str | None:
    latest = find_latest_version_folder(versions_dir)
    if latest is None:
        return None
    try:
        return normalize_version_hash(latest.name)
    except VersionManagerError:
        return None


def ensure_safe_versions_dir(versions_dir: Path) -> None:
    resolved = versions_dir.resolve()
    if len(resolved.parts) < 3 or resolved == Path(resolved.anchor):
        raise VersionManagerError(
            "Choose a specific Roblox Versions folder, not a drive or filesystem root."
        )
    resolved.mkdir(parents=True, exist_ok=True)


def safe_remove_old_versions(
    versions_dir: Path,
    keep_dir: Path,
    log: Callable[[str], None],
    preserve: bool = True,
    retained_versions: int = DEFAULT_RETAINED_VERSIONS,
) -> None:
    if preserve:
        candidates = sorted(
            [
                child
                for child in versions_dir.iterdir()
                if child.is_dir() and child.name.startswith(VERSION_PREFIX)
            ],
            key=lambda child: child.stat().st_mtime,
            reverse=True,
        )
        keep_names = {child.resolve() for child in candidates[: max(1, retained_versions)]}
    else:
        keep_names = {keep_dir.resolve()}
    keep_dir = keep_dir.resolve()
    removed = 0
    for child in versions_dir.iterdir():
        if not child.is_dir() or not child.name.startswith(VERSION_PREFIX):
            continue
        if child.resolve() in keep_names:
            continue
        if child.resolve().parent != versions_dir.resolve():
            continue
        shutil.rmtree(child)
        removed += 1
        log(f"Removed old version folder: {child.name}")
    if removed == 0:
        log("No old version folders needed cleanup.")


def startup_directory() -> Path:
    app_data = os.getenv("APPDATA")
    if not app_data:
        raise VersionManagerError("APPDATA is not available on this Windows account.")
    return Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_shortcut_path() -> Path:
    return startup_directory() / "Roblox Version Manager.lnk"


def configure_startup_shortcut(enabled: bool) -> None:
    shortcut_path = startup_shortcut_path()
    if not enabled:
        if shortcut_path.exists():
            shortcut_path.unlink()
        return
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        target_path = Path(sys.executable)
        arguments = "--background"
        working_directory = runtime_dir()
    else:
        target_path = Path(sys.executable)
        arguments = f'"{Path(__file__).resolve()}" --background'
        working_directory = Path(__file__).resolve().parent
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shortcut = $shell.CreateShortcut('{escape_powershell(shortcut_path)}'); "
        f"$shortcut.TargetPath = '{escape_powershell(target_path)}'; "
        f"$shortcut.Arguments = '{escape_powershell_text(arguments)}'; "
        f"$shortcut.WorkingDirectory = '{escape_powershell(working_directory)}'; "
        "$shortcut.WindowStyle = 7; $shortcut.Save()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    if completed.returncode != 0:
        raise VersionManagerError("Windows could not configure the background startup shortcut.")


def escape_powershell_text(value: str) -> str:
    return value.replace("'", "''")


def is_roblox_player_running() -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {ROBLOX_EXE}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    return ROBLOX_EXE.lower() in completed.stdout.lower()


def is_roblox_running() -> bool:
    return bool(list_running_roblox_processes())


def open_app_background() -> None:
    if ctk is None:
        return
    app = App()
    app.mainloop()


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
    preserve_versions: bool = True,
    retained_versions: int = DEFAULT_RETAINED_VERSIONS,
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
    state_dir = Path(tempfile.mkdtemp(prefix="rvm_user_state_"))
    try:
        snapshots = snapshot_roblox_user_state(versions_dir, active_dir, state_dir, log)
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
                raise VersionManagerError(f"Cannot replace existing path: {target_dir}")

        final_dir = rename_active_folder(active_dir, version.folder_name)
        restore_roblox_user_state(snapshots, versions_dir, final_dir, log)
        safe_remove_old_versions(
            versions_dir,
            final_dir,
            log,
            preserve=preserve_versions,
            retained_versions=retained_versions,
        )
        progress(1.0)
        return final_dir
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


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
        self.geometry("1080x760")
        self.minsize(860, 620)
        self.resizable(True, True)
        icon_path = resource_path(ICON_FILENAME)
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.settings = load_settings()
        configured_versions_dir = str(self.settings.get("versions_dir") or "").strip()
        try:
            self.versions_dir = Path(configured_versions_dir) if configured_versions_dir else default_versions_dir()
        except VersionManagerError:
            self.versions_dir = Path.home() / "AppData" / "Local" / "Roblox" / "Versions"
        self.cache_dir = default_cache_dir()
        self.log_dir = default_log_dir()
        self.logger = RunLogger(self.log_dir)
        self.versions: list[RobloxVersion] = []
        self.products: list[WEAOProduct] = load_cached_products()
        saved_product_id = str(self.settings.get("selected_product_id") or "")
        self.selected_product_id: str | None = saved_product_id or None
        self.selected_label = ctk.StringVar(value="Loading versions...")
        self.product_query = ctk.StringVar()
        self.launch_after_update = ctk.BooleanVar(value=False)
        self.status_text = ctk.StringVar(value="Ready")
        self.current_text = ctk.StringVar(value="Current local version: checking...")
        self.latest_text = ctk.StringVar(value="Latest tracked build: checking...")
        self.product_count_text = ctk.StringVar(value="Loading WEAO products..." if not self.products else "")
        self.product_title_text = ctk.StringVar(value="Select a product")
        self.product_version_text = ctk.StringVar(value="Product profile")
        self.product_build_text = ctk.StringVar(value="Choose a product to see its target Roblox build.")
        self.product_meta_text = ctk.StringVar(value="")
        self.product_features_text = ctk.StringVar(value="")
        self.weao_current_text = ctk.StringVar(value="WEAO current build: checking...")
        self.log_path_text = ctk.StringVar(value=f"Logs: {self.logger.path.name}")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.loading_products = bool(not self.products)
        self.loading_versions = True
        self.busy = False
        self.monitor_stop = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        self.auto_sync_in_progress = False
        self._last_auto_sync_hash: str | None = None
        self.product_refresh_started = False
        self.favorite_cache_thread: threading.Thread | None = None
        self.favorite_cache_stop = threading.Event()
        self.tray_icon: Any = None
        self.tray_thread: threading.Thread | None = None
        self.settings_window: Any = None
        self.settings_save_button: Any = None
        self.settings_feedback: Any = None

        self.build_ui()
        self.refresh_local_status()
        self.after(100, self.drain_events)
        if self.products:
            self.select_product(self.selected_product() or self.products[0])
        else:
            self.product_count_text.set("WEAO catalog loading in background")
        self.refresh_versions()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.settings.get("start_minimized") or BACKGROUND_MODE:
            self.after(250, self.withdraw)
        if self.settings.get("background_tray"):
            self.after(250, self.withdraw)
        if self.settings.get("auto_sync"):
            self.start_auto_sync_monitor()
        if self.settings.get("auto_cache_favorites"):
            self.start_favorite_cache_monitor()
        if self.settings.get("background_tray") or BACKGROUND_MODE:
            self.start_tray_icon()
        elif self.settings.get("auto_cache_favorites"):
            self.start_tray_icon()
        if self.settings.get("auto_sync") and BACKGROUND_MODE:
            self.status_text.set("Background auto-sync active")

    def build_ui(self) -> None:
        self.configure(fg_color="#0d1117")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 2))
        header_row.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            header_row,
            text="RVM  /  Roblox Version Manager",
            font=ctk.CTkFont(size=25, weight="bold"),
            text_color="#f4f7fb",
        )
        header.grid(row=0, column=0, sticky="w")

        live_badge = ctk.CTkLabel(
            header_row,
            text="WEAO LIVE",
            text_color="#0d1117",
            fg_color="#3bea57",
            corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            padx=10,
            pady=4,
        )
        live_badge.grid(row=0, column=1, sticky="e", padx=(12, 0))

        settings_button = ctk.CTkButton(
            header_row,
            text="Settings",
            command=self.open_settings,
            width=82,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color="#2b3745",
            hover_color="#202b37",
        )
        settings_button.grid(row=0, column=2, sticky="e", padx=(10, 0))

        subtitle = ctk.CTkLabel(
            self,
            text="Choose a product, review its build details, and launch or sync the matching Roblox version.",
            text_color="#8d98a6",
            font=ctk.CTkFont(size=12),
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=30, pady=(0, 20))

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", padx=22)
        content.grid_columnconfigure(0, weight=4, minsize=360)
        content.grid_columnconfigure(1, weight=6, minsize=420)
        content.grid_rowconfigure(0, weight=1)

        products_panel = ctk.CTkFrame(content, corner_radius=16, fg_color="#151b23", border_width=1, border_color="#222c38")
        products_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        products_panel.grid_columnconfigure(0, weight=1)
        products_panel.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            products_panel,
            text="PRODUCT CATALOG",
            text_color="#3bea57",
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 3))

        product_heading = ctk.CTkLabel(
            products_panel,
            textvariable=self.product_count_text,
            anchor="w",
            text_color="#eef3f8",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        product_heading.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))

        search_row = ctk.CTkFrame(products_panel, fg_color="transparent")
        search_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))
        search_row.grid_columnconfigure(0, weight=1)
        self.product_search = ctk.CTkEntry(
            search_row,
            textvariable=self.product_query,
            placeholder_text="Search products...",
            height=35,
            corner_radius=9,
            border_color="#2b3745",
        )
        self.product_search.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.refresh_products_button = ctk.CTkButton(
            search_row,
            text="Refresh",
            width=76,
            height=35,
            corner_radius=9,
            fg_color="#27313d",
            hover_color="#344252",
            command=self.refresh_products_clicked,
        )
        self.refresh_products_button.grid(row=0, column=1)
        self.product_query.trace_add("write", lambda *_: self.rebuild_product_cards())

        filter_row = ctk.CTkFrame(products_panel, fg_color="transparent")
        filter_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        filter_row.grid_columnconfigure(0, weight=1)
        filter_row.grid_columnconfigure(1, weight=1)
        self.product_filter = ctk.CTkOptionMenu(
            filter_row,
        values=["All products", "Favorites only"],
            command=lambda value: self.filter_changed(value),
            height=30,
            corner_radius=8,
            dynamic_resizing=False,
        )
        filter_value = str(self.settings.get("product_filter") or "All products")
        if filter_value not in {"All products", "Favorites only"}:
            filter_value = "All products"
        self.product_filter.set(filter_value)
        self.product_filter.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.sort_menu = ctk.CTkOptionMenu(
            filter_row,
            values=["All", "Executer", "External"],
            command=lambda value: self.type_filter_changed(value),
            height=30,
            corner_radius=8,
            dynamic_resizing=False,
        )
        type_filter_value = str(self.settings.get("type_filter") or "All")
        if type_filter_value not in {"All", "Executer", "External"}:
            type_filter_value = "All"
        self.sort_menu.set(type_filter_value)
        self.sort_menu.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self.products_frame = ctk.CTkScrollableFrame(
            products_panel,
            fg_color="#10161d",
            corner_radius=11,
            scrollbar_button_color="#2a3745",
            scrollbar_button_hover_color="#3a4b5d",
        )
        products_panel.grid_rowconfigure(4, weight=1)
        self.products_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 13))
        self.products_frame.grid_columnconfigure(0, weight=1)

        detail_panel = ctk.CTkFrame(content, corner_radius=16, fg_color="#151b23", border_width=1, border_color="#222c38")
        detail_panel.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        detail_panel.grid_columnconfigure(0, weight=1)
        detail_panel.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(
            detail_panel,
            text="SELECTED PRODUCT",
            text_color="#3bea57",
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 4))
        title_row = ctk.CTkFrame(detail_panel, fg_color="transparent")
        title_row.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 3))
        title_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            title_row,
            textvariable=self.product_title_text,
            anchor="w",
            text_color="#f4f7fb",
            font=ctk.CTkFont(size=23, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=(4, 8))
        self.website_button = ctk.CTkButton(
            title_row,
            text="Site",
            command=lambda: self.open_product_link("website"),
            width=48,
            height=23,
            corner_radius=7,
            fg_color="#27313d",
            hover_color="#344252",
            font=ctk.CTkFont(size=10),
        )
        self.website_button.grid(row=0, column=1, padx=(0, 4))
        self.discord_button = ctk.CTkButton(
            title_row,
            text="Discord",
            command=lambda: self.open_product_link("discord"),
            width=66,
            height=23,
            corner_radius=7,
            fg_color="#27313d",
            hover_color="#344252",
            font=ctk.CTkFont(size=10),
        )
        self.discord_button.grid(row=0, column=2, padx=4)
        self.favorite_button = ctk.CTkButton(
            title_row,
            text="☆",
            command=self.toggle_favorite,
            width=30,
            height=25,
            corner_radius=7,
            fg_color="#27313d",
            hover_color="#344252",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.favorite_button.grid(row=0, column=3, padx=(4, 0))
        ctk.CTkLabel(
            detail_panel,
            textvariable=self.product_version_text,
            anchor="w",
            text_color="#aab6c3",
            font=ctk.CTkFont(size=13),
        ).grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 12))

        build_card = ctk.CTkFrame(detail_panel, corner_radius=12, fg_color="#10161d")
        build_card.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 12))
        build_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            build_card,
            text="TARGET ROBLOX BUILD",
            anchor="w",
            text_color="#748293",
            font=ctk.CTkFont(size=10, weight="bold"),
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(13, 2))
        ctk.CTkLabel(
            build_card,
            textvariable=self.product_build_text,
            anchor="w",
            text_color="#e9eff5",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))
        ctk.CTkLabel(
            build_card,
            textvariable=self.product_meta_text,
            anchor="w",
            text_color="#748293",
            font=ctk.CTkFont(size=10),
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 11))

        description_frame = ctk.CTkFrame(detail_panel, fg_color="transparent")
        description_frame.grid(row=5, column=0, sticky="nsew", padx=20, pady=(0, 8))
        description_frame.grid_columnconfigure(0, weight=1)
        description_frame.grid_rowconfigure(1, weight=1)
        self.product_features_label = ctk.CTkLabel(
            description_frame,
            textvariable=self.product_features_text,
            anchor="w",
            wraplength=520,
            text_color="#c0cad5",
            font=ctk.CTkFont(size=11),
        )
        self.product_features_label.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 9))
        self.product_description = ctk.CTkTextbox(
            description_frame,
            height=150,
            corner_radius=10,
            fg_color="#10161d",
            border_width=1,
            border_color="#222c38",
            text_color="#98a5b3",
            wrap="word",
        )
        self.product_description.grid(row=1, column=0, sticky="nsew")
        self.product_description.configure(state="disabled")

        action_row = ctk.CTkFrame(detail_panel, fg_color="transparent")
        action_row.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 10))
        action_row.grid_columnconfigure(0, weight=1)
        action_row.grid_columnconfigure(1, weight=1)
        self.sync_product_button = ctk.CTkButton(
            action_row,
            text="Sync selected product",
            command=self.sync_product_clicked,
            height=43,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.sync_product_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.launch_product_button = ctk.CTkButton(
            action_row,
            text="Launch selected",
            command=self.launch_selected_product_clicked,
            height=43,
            corner_radius=10,
            fg_color="#27313d",
            hover_color="#344252",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.launch_product_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        manual_panel = ctk.CTkFrame(self, corner_radius=13, fg_color="#151b23", border_width=1, border_color="#222c38")
        manual_panel.grid(row=3, column=0, sticky="ew", padx=22, pady=(14, 0))
        manual_panel.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(manual_panel, text="Manual build", text_color="#c8d1dc", anchor="w").grid(
            row=0, column=0, padx=(16, 8), pady=12
        )
        self.version_menu = ctk.CTkOptionMenu(
            manual_panel,
            variable=self.selected_label,
            values=["Loading versions..."],
            height=34,
            corner_radius=9,
            dynamic_resizing=False,
        )
        self.version_menu.grid(row=0, column=1, sticky="ew", padx=8, pady=12)
        self.manual_apply_button = ctk.CTkButton(
            manual_panel,
            text="Apply manual",
            command=self.apply_clicked,
            width=126,
            height=34,
            corner_radius=9,
            fg_color="#27313d",
            hover_color="#344252",
        )
        self.manual_apply_button.grid(row=0, column=2, padx=(8, 16), pady=12)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=22, pady=(10, 18))
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_columnconfigure(1, weight=1)
        footer.grid_columnconfigure(2, weight=1)
        footer.grid_columnconfigure(3, weight=1)

        current = ctk.CTkLabel(footer, textvariable=self.current_text, anchor="w", text_color="#aeb9c5")
        current.grid(row=0, column=0, sticky="w")
        latest = ctk.CTkLabel(
            footer,
            textvariable=self.latest_text,
            anchor="w",
            text_color="#778493",
            font=ctk.CTkFont(size=11),
        )
        latest.grid(row=1, column=0, sticky="w", pady=(2, 0))
        ctk.CTkLabel(
            footer,
            textvariable=self.weao_current_text,
            anchor="w",
            text_color="#667382",
            font=ctk.CTkFont(size=10),
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))

        self.launch_check = ctk.CTkCheckBox(
            footer,
            text="Launch after sync",
            variable=self.launch_after_update,
            corner_radius=6,
        )
        self.launch_check.grid(row=0, column=1, rowspan=2, sticky="w", padx=14)

        self.progress = ctk.CTkProgressBar(footer, height=10, corner_radius=5)
        self.progress.grid(row=0, column=2, columnspan=2, sticky="ew", padx=(10, 0), pady=(0, 5))
        self.progress.set(0)
        self.status_label = ctk.CTkLabel(footer, textvariable=self.status_text, anchor="w", text_color="#8d98a6")
        self.status_label.grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.log_path_label = ctk.CTkLabel(
            footer,
            textvariable=self.log_path_text,
            anchor="e",
            text_color="#667382",
            font=ctk.CTkFont(size=10),
        )
        self.log_path_label.grid(row=1, column=3, sticky="e")

        utility_row = ctk.CTkFrame(self, fg_color="transparent")
        utility_row.grid(row=5, column=0, sticky="ew", padx=22, pady=(0, 18))
        utility_row.grid_columnconfigure(0, weight=1)
        utility_row.grid_columnconfigure(1, weight=1)
        utility_row.grid_columnconfigure(2, weight=1)
        self.logs_button = ctk.CTkButton(
            utility_row,
            text="Open logs",
            command=self.open_logs_folder_clicked,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color="#2b3745",
            hover_color="#202b37",
        )
        self.logs_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.shortcut_button = ctk.CTkButton(
            utility_row,
            text="Update desktop shortcut",
            command=self.update_shortcut_clicked,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color="#2b3745",
            hover_color="#202b37",
        )
        self.shortcut_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.refresh_versions_button = ctk.CTkButton(
            utility_row,
            text="Refresh Roblox builds",
            command=self.refresh_versions_clicked,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color="#2b3745",
            hover_color="#202b37",
        )
        self.refresh_versions_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        self.history_button = ctk.CTkButton(
            utility_row,
            text="Sync history",
            command=self.open_history,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color="#2b3745",
            hover_color="#202b37",
        )
        self.history_button.grid(row=0, column=3, sticky="ew", padx=(6, 0))
        utility_row.grid_columnconfigure(3, weight=1)

        self.rebuild_product_cards()
        self.update_control_state()

    def refresh_products(self, force: bool = True) -> None:
        if force:
            self.product_refresh_started = False
        if self.product_refresh_started:
            return
        self.product_refresh_started = True
        self.loading_products = True
        self.update_control_state()
        self.status_text.set("Refreshing product data in background...")
        thread = threading.Thread(target=self.load_products_worker, args=(force,), daemon=True)
        thread.start()

    def refresh_versions(self) -> None:
        self.loading_versions = True
        self.update_control_state()
        self.status_text.set("Loading Roblox build history...")
        thread = threading.Thread(target=self.load_versions_worker, daemon=True)
        thread.start()

    def refresh_products_clicked(self) -> None:
        if not self.busy:
            self.refresh_products(force=True)

    def refresh_versions_clicked(self) -> None:
        if not self.busy:
            self.refresh_versions()

    def load_products_worker(self, use_cache: bool = True) -> None:
        try:
            if use_cache and self.products:
                self.events.put(("products", self.products))
            products = fetch_weao_products()
            self.events.put(("products", products))
            self.events.put(("status", "WEAO product catalog updated."))
            current_version = fetch_weao_current_windows()
            self.events.put(("weao_current", current_version))
        except Exception as exc:
            self.events.put(("error", ("WEAO product catalog", exc)))

    def load_versions_worker(self) -> None:
        try:
            self.events.put(("versions", fetch_versions()))
        except Exception as exc:
            self.events.put(("error", ("Roblox build history", exc)))

    def selected_product(self) -> WEAOProduct | None:
        for product in self.products:
            if product.product_id == self.selected_product_id:
                return product
        return self.products[0] if self.products else None

    def select_product(self, product: WEAOProduct) -> None:
        self.selected_product_id = product.product_id
        self.settings["selected_product_id"] = product.product_id
        save_settings(self.settings)
        self.product_title_text.set(product.title)
        self.product_version_text.set(f"{product_type_label(product)}  /  version {product.version}  /  {product.platform}")
        self.product_build_text.set(f"{product.build_label}  |  Roblox {product.rbx_version}")
        self.product_meta_text.set(f"Last updated  {product.updated_at}")
        features = []
        if product.sunc_percentage is not None:
            features.append(f"sUNC {product.sunc_percentage}%")
        if product.unc_percentage is not None:
            features.append(f"UNC {product.unc_percentage}%")
        if product.decompiler:
            features.append("Decompiler")
        if product.multi_inject:
            features.append("Multi-instance")
        if product.element_certified:
            features.append("Element certified")
        type_label = product_type_label(product)
        feature_text = "  /  ".join(features) if features else "No feature data published by WEAO."
        self.product_features_text.set(f"FEATURES\n{feature_text}")
        self.set_description(product.description)
        favorites = {str(value) for value in self.settings.get("favorites", [])}
        self.favorite_button.configure(
            text="★" if product.product_id in favorites else "☆",
            text_color="#ffd166" if product.product_id in favorites else "#c5d0db",
        )
        self.website_button.configure(state="normal" if product.website_url else "disabled")
        self.discord_button.configure(state="normal" if product.discord_url else "disabled")
        self.rebuild_product_cards()

    def rebuild_product_cards(self) -> None:
        if not hasattr(self, "products_frame"):
            return
        for child in self.products_frame.winfo_children():
            child.destroy()
        query = self.product_query.get().strip().lower()
        product_filter = self.product_filter.get() if hasattr(self, "product_filter") else "All products"
        type_filter = self.sort_menu.get() if hasattr(self, "sort_menu") else "All"
        favorites = {str(value) for value in self.settings.get("favorites", [])}
        visible_products = [
            product
            for product in self.products
            if (not query or query in product.title.lower() or query in product.version.lower())
            and (product_filter != "Favorites only" or product.product_id in favorites)
            and (type_filter == "All" or product_type_label(product) == type_filter)
        ]
        visible_products.sort(key=lambda product: product_sort_key(product, "Type"))
        if self.loading_products and not self.products:
            self.product_count_text.set("Loading WEAO products...")
        else:
            self.product_count_text.set(f"{len(visible_products)} Windows products")
        if not visible_products:
            ctk.CTkLabel(
                self.products_frame,
                text="No products match your search.",
                text_color="#7e8b99",
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=12, pady=16)
            return
        for index, product in enumerate(visible_products):
            selected = product.product_id == self.selected_product_id
            card = ctk.CTkFrame(
                self.products_frame,
                height=74,
                corner_radius=10,
                fg_color="#263b2b" if selected else "#18212b",
                border_width=1,
            border_color="#3bea57" if selected else "#202c38",
            )
            card.grid(row=index, column=0, sticky="ew", padx=3, pady=(0, 7))
            card.grid_propagate(False)
            card.grid_columnconfigure(0, weight=0)
            card.grid_columnconfigure(1, weight=1)
            card.grid_columnconfigure(2, weight=0)
            card.bind("<Button-1>", lambda _event, item=product: self.select_product(item))
            favorite_button = ctk.CTkButton(
                card,
                text="★" if product.product_id in favorites else "☆",
                command=lambda item=product: self.toggle_product_favorite(item),
                width=30,
                height=30,
                corner_radius=8,
                fg_color="transparent",
                hover_color="#263442",
                text_color="#ffd166" if product.product_id in favorites else "#778493",
                font=ctk.CTkFont(size=17, weight="bold"),
            )
            favorite_button.grid(row=0, column=0, rowspan=2, padx=(5, 2), pady=5)
            title = ctk.CTkLabel(
                card,
                text=product.title,
                anchor="w",
                text_color="#edf3f7",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            title.grid(row=0, column=1, sticky="ew", padx=(7, 4), pady=(8, 0))
            title.bind("<Button-1>", lambda _event, item=product: self.select_product(item))
            type_label = product_type_label(product)
            subtitle = ctk.CTkLabel(
                card,
                text=f"version {product.version}  /  {product.build_label}",
                anchor="w",
                text_color="#8f9baa",
                font=ctk.CTkFont(size=10),
            )
            subtitle.grid(row=1, column=1, sticky="ew", padx=(7, 4), pady=(0, 8))
            subtitle.bind("<Button-1>", lambda _event, item=product: self.select_product(item))
            status = ctk.CTkLabel(
                card,
                text=type_label,
                text_color="#8f9baa",
                width=84,
                anchor="e",
                font=ctk.CTkFont(size=9, weight="bold"),
            )
            status.grid(row=0, column=2, rowspan=2, padx=(4, 12))
            status.bind("<Button-1>", lambda _event, item=product: self.select_product(item))

    def set_description(self, description: str) -> None:
        self.product_description.configure(state="normal")
        self.product_description.delete("1.0", "end")
        self.product_description.insert("1.0", description.strip() or "No description currently available. Check back soon!")
        self.product_description.configure(state="disabled")

    def open_product_link(self, link_type: str) -> None:
        product = self.selected_product()
        if product is None:
            return
        url = product.website_url if link_type == "website" else product.discord_url
        if url:
            webbrowser.open(url)

    def filter_changed(self, value: str) -> None:
        self.settings["product_filter"] = value
        save_settings(self.settings)
        self.rebuild_product_cards()

    def sort_changed(self, value: str) -> None:
        self.settings["sort_mode"] = value
        save_settings(self.settings)
        self.rebuild_product_cards()

    def type_filter_changed(self, value: str) -> None:
        self.settings["type_filter"] = value
        save_settings(self.settings)
        self.rebuild_product_cards()

    def toggle_favorite(self) -> None:
        product = self.selected_product()
        if product is None:
            return
        self.toggle_product_favorite(product)

    def toggle_product_favorite(self, product: WEAOProduct) -> None:
        favorites = {str(value) for value in self.settings.get("favorites", [])}
        if product.product_id in favorites:
            favorites.remove(product.product_id)
        else:
            favorites.add(product.product_id)
        self.settings["favorites"] = sorted(favorites)
        save_settings(self.settings)
        self.refresh_tray_menu()
        if product.product_id == self.selected_product_id:
            self.favorite_button.configure(
                text="★" if product.product_id in favorites else "☆",
                text_color="#ffd166" if product.product_id in favorites else "#c5d0db",
            )
        self.rebuild_product_cards()

    def sync_product_clicked(self) -> None:
        product = self.selected_product()
        if product is None:
            messagebox.showwarning(APP_NAME, "Choose a WEAO product first.")
            return
        self.start_install(
            version_from_hash(product.rbx_version),
            f"{product.title} {product.version}",
        )

    def launch_selected_product_clicked(self) -> None:
        product = self.selected_product()
        if product is None:
            messagebox.showwarning(APP_NAME, "Choose a product first.")
            return
        target_version = version_from_hash(product.rbx_version)
        if local_version_hash(self.versions_dir) == target_version.hash:
            local_dir = find_latest_version_folder(self.versions_dir)
            if local_dir is not None and (local_dir / ROBLOX_EXE).exists():
                try:
                    launch_roblox(local_dir)
                    self.status_text.set(f"Launched {product.title} from cached {local_dir.name}")
                    return
                except VersionManagerError:
                    pass
        self.start_install(
            target_version,
            f"launch {product.title} {product.version}",
            launch_after_update=True,
        )

    def launch_favorite_from_tray(self, product: WEAOProduct) -> None:
        self.selected_product_id = product.product_id
        self.settings["selected_product_id"] = product.product_id
        save_settings(self.settings)
        self.select_product(product)
        self.launch_selected_product_clicked()

    def apply_clicked(self) -> None:
        version = self.selected_version()
        if version is None:
            messagebox.showwarning(APP_NAME, "Roblox builds are still loading.")
            return
        self.start_install(version, "manual Roblox build")

    def start_install(
        self,
        version: RobloxVersion,
        source_label: str,
        launch_after_update: bool | None = None,
    ) -> None:
        if self.busy:
            return
        if launch_after_update is None:
            launch_after_update = self.launch_after_update.get()
        self.set_busy(True, f"Preparing {source_label}...")
        self.progress.set(0)
        thread = threading.Thread(
            target=self.install_worker,
            args=(version, launch_after_update, source_label),
            daemon=True,
        )
        thread.start()

    def install_worker(self, version: RobloxVersion, launch_after_update: bool, source_label: str) -> None:
        try:
            final_dir = install_version(
                version,
                self.versions_dir,
                self.cache_dir,
                log=self.queue_log,
                progress=lambda value: self.events.put(("progress", value)),
                preserve_versions=bool(self.settings.get("preserve_versions", True)),
                retained_versions=int(self.settings.get("retained_versions", DEFAULT_RETAINED_VERSIONS) or DEFAULT_RETAINED_VERSIONS),
            )
            shortcut_path = create_desktop_shortcut(final_dir)
            if launch_after_update:
                launch_roblox(final_dir)
            self.events.put(("done", (final_dir, shortcut_path, source_label, version.hash)))
        except Exception as exc:
            self.events.put(("error", ("Version sync", exc)))

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

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        window = ctk.CTkToplevel(self)
        self.settings_window = window
        window.title("Settings")
        window.geometry("590x540")
        window.minsize(500, 460)
        window.transient(self)
        window.attributes("-topmost", True)
        window.grab_set()
        window.lift()
        window.focus_force()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(window, text="SETTINGS", text_color="#3bea57", anchor="w", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=24, pady=(22, 10)
        )
        body = ctk.CTkScrollableFrame(window, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16)
        body.grid_columnconfigure(0, weight=1)

        folder_card = ctk.CTkFrame(body, corner_radius=12, fg_color="#151b23")
        folder_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 10))
        folder_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(folder_card, text="Roblox install location", anchor="w", text_color="#eef3f8", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=16, pady=(14, 2)
        )
        folder_text = ctk.StringVar(value=str(self.versions_dir))
        ctk.CTkLabel(folder_card, textvariable=folder_text, anchor="w", text_color="#83909e", wraplength=420).grid(
            row=1, column=0, sticky="ew", padx=16, pady=(0, 10)
        )
        ctk.CTkButton(folder_card, text="Change Roblox version folder", command=self.choose_versions_directory, height=34, corner_radius=9).grid(
            row=2, column=0, sticky="ew", padx=16, pady=(0, 15)
        )

        auto_var = ctk.BooleanVar(value=bool(self.settings.get("auto_sync", False)))
        cache_var = ctk.BooleanVar(value=bool(self.settings.get("auto_cache_favorites", False)))
        tray_var = ctk.BooleanVar(value=bool(self.settings.get("background_tray", False)))
        preserve_var = ctk.BooleanVar(value=bool(self.settings.get("preserve_versions", True)))
        start_minimized_var = ctk.BooleanVar(value=bool(self.settings.get("start_minimized", False)))
        relaunch_var = ctk.BooleanVar(value=bool(self.settings.get("relaunch_after_auto_sync", False)))
        controls = [
            ("Auto-sync current selected product on Roblox launch", auto_var, "When RobloxPlayerBeta.exe starts, compare it with the selected product and resync if needed."),
            ("Auto-cache favorite products", cache_var, "Keep favorite product builds downloaded in the background for faster hot-swapping. Requires background mode."),
            ("Run in background tray", tray_var, "Keep RVM available from the Windows notification area with quick favorite-product controls."),
            ("Keep previous Roblox versions", preserve_var, "Retain old version folders so you can switch back without downloading again."),
            ("Start minimized", start_minimized_var, "Launch the manager hidden. Use the tray/startup shortcut to keep background sync running."),
            ("Relaunch Roblox after auto-sync", relaunch_var, "Start Roblox again after an automatic version correction finishes."),
        ]
        for index, (label, variable, description) in enumerate(controls, start=1):
            card = ctk.CTkFrame(body, corner_radius=11, fg_color="#151b23")
            card.grid(row=index, column=0, sticky="ew", padx=4, pady=5)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkCheckBox(card, text=label, variable=variable, corner_radius=6, font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=0, column=0, sticky="w", padx=14, pady=(12, 2)
            )
            ctk.CTkLabel(card, text=description, anchor="w", wraplength=460, text_color="#8693a1", font=ctk.CTkFont(size=10)).grid(
                row=1, column=0, sticky="ew", padx=14, pady=(0, 12)
            )

        retained_row = ctk.CTkFrame(body, corner_radius=11, fg_color="#151b23")
        retained_row.grid(row=7, column=0, sticky="ew", padx=4, pady=5)
        retained_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(retained_row, text="Retained local versions", anchor="w", text_color="#eef3f8").grid(row=0, column=0, padx=14, pady=12)
        retained_entry = ctk.CTkEntry(retained_row, width=70, height=30)
        retained_entry.insert(0, str(self.settings.get("retained_versions", DEFAULT_RETAINED_VERSIONS)))
        retained_entry.grid(row=0, column=1, padx=14, pady=12)

        def save_and_close() -> None:
            try:
                retained = max(1, min(20, int(retained_entry.get().strip())))
            except ValueError:
                messagebox.showerror(APP_NAME, "Retained versions must be a number from 1 to 20.", parent=window)
                return
            if cache_var.get():
                tray_var.set(True)
            self.settings.update(
                {
                    "auto_sync": auto_var.get(),
                    "auto_cache_favorites": cache_var.get(),
                    "background_tray": tray_var.get(),
                    "preserve_versions": preserve_var.get(),
                    "start_minimized": start_minimized_var.get(),
                    "relaunch_after_auto_sync": relaunch_var.get(),
                    "retained_versions": retained,
                }
            )
            save_settings(self.settings)
            background_enabled = auto_var.get() or cache_var.get() or tray_var.get()
            try:
                configure_startup_shortcut(background_enabled)
            except Exception as exc:
                messagebox.showwarning(APP_NAME, str(exc), parent=window)
            if auto_var.get():
                self.start_auto_sync_monitor()
            else:
                self.stop_auto_sync_monitor()
            if cache_var.get():
                self.start_favorite_cache_monitor()
            else:
                self.stop_favorite_cache_monitor()
            if tray_var.get():
                self.start_tray_icon()
            else:
                self.stop_tray_icon()
            self.rebuild_product_cards()
            self.settings_feedback.configure(text="Settings saved", text_color="#3bea57")
            self.settings_save_button.configure(text="Saved")
            window.after(900, lambda: self.settings_feedback.configure(text=""))
            window.after(1200, lambda: self.settings_save_button.configure(text="Save settings"))

        self.settings_feedback = ctk.CTkLabel(window, text="", text_color="#7f8d9d", anchor="e", font=ctk.CTkFont(size=10))
        self.settings_feedback.grid(row=2, column=0, sticky="e", padx=24, pady=(10, 0))
        self.settings_save_button = ctk.CTkButton(window, text="Save settings", command=save_and_close, height=40, corner_radius=10)
        self.settings_save_button.grid(
            row=3, column=0, sticky="ew", padx=24, pady=(6, 22)
        )

        def close_settings() -> None:
            try:
                window.grab_release()
            except Exception:
                pass
            self.settings_window = None
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_settings)

    def start_auto_sync_monitor(self) -> None:
        self.settings["auto_sync"] = True
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self.monitor_stop.clear()
        self.monitor_thread = threading.Thread(target=self.auto_sync_worker, daemon=True)
        self.monitor_thread.start()

    def stop_auto_sync_monitor(self) -> None:
        self.settings["auto_sync"] = False
        self.monitor_stop.set()

    def start_favorite_cache_monitor(self) -> None:
        self.settings["auto_cache_favorites"] = True
        self.settings["background_tray"] = True
        if self.favorite_cache_thread and self.favorite_cache_thread.is_alive():
            return
        self.favorite_cache_stop.clear()
        self.favorite_cache_thread = threading.Thread(target=self.favorite_cache_worker, daemon=True)
        self.favorite_cache_thread.start()

    def start_tray_icon(self) -> None:
        if pystray is None or Image is None or self.tray_icon is not None:
            return
        icon_path = resource_path(ICON_FILENAME)
        if icon_path.exists():
            icon_image = Image.open(icon_path).convert("RGBA")
        else:
            icon_image = Image.new("RGBA", (64, 64), (59, 234, 87, 255))

        menu = self.build_tray_menu()
        self.tray_icon = pystray.Icon("roblox-version-manager", icon_image, APP_NAME, menu)

        try:
            self.tray_icon.run_detached(setup=lambda icon: setattr(icon, "visible", True))
            self.status_text.set("System tray active")
        except Exception as exc:
            self.tray_icon = None
            self.events.put(("status", f"System tray unavailable: {exc}"))

    def build_tray_menu(self) -> Any:
        if pystray is None:
            return None

        def show_window(_icon: Any, _item: Any) -> None:
            self.after(0, self.show_main_window)

        def hide_window(_icon: Any, _item: Any) -> None:
            self.after(0, self.withdraw)

        def exit_app(_icon: Any, _item: Any) -> None:
            self.after(0, self.on_close)

        def favorite_action(product: WEAOProduct) -> Callable[[Any, Any], None]:
            def launch(_icon: Any, _item: Any) -> None:
                self.after(0, lambda: self.launch_favorite_from_tray(product))

            return launch

        favorites = {str(value) for value in self.settings.get("favorites", [])}
        product_items = [
            pystray.MenuItem(product.title, favorite_action(product))
            for product in self.products
            if product.product_id in favorites
        ]
        favorite_menu = pystray.Menu(*product_items) if product_items else pystray.Menu(
            pystray.MenuItem("No favorites", None, enabled=False)
        )
        return pystray.Menu(
            pystray.MenuItem("Show RVM", show_window),
            pystray.MenuItem("Hide RVM", hide_window),
            pystray.MenuItem("Launch favorite", favorite_menu),
            pystray.MenuItem("Exit", exit_app),
        )

    def show_main_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def stop_tray_icon(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
            self.status_text.set("System tray stopped")

    def refresh_tray_menu(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.menu = self.build_tray_menu()

    def stop_favorite_cache_monitor(self) -> None:
        self.settings["auto_cache_favorites"] = False
        self.favorite_cache_stop.set()

    def favorite_cache_worker(self) -> None:
        try:
            self.cache_favorite_products()
        except Exception as exc:
            self.events.put(("status", f"Favorite cache update failed: {exc}"))
        while not self.favorite_cache_stop.wait(3600):
            try:
                products = fetch_weao_products()
                self.events.put(("products", products))
                self.cache_favorite_products(products)
            except Exception as exc:
                self.events.put(("status", f"Favorite cache update failed: {exc}"))

    def cache_favorite_products(self, products: list[WEAOProduct] | None = None) -> None:
        favorites = {str(value) for value in self.settings.get("favorites", [])}
        catalog = products if products is not None else self.products
        favorite_products = [product for product in catalog if product.product_id in favorites]
        if not favorite_products:
            return
        self.events.put(("status", f"Caching {len(favorite_products)} favorite product build(s)..."))
        for product in favorite_products:
            version = version_from_hash(product.rbx_version)
            with tempfile.TemporaryDirectory(prefix="rvm_cache_stage_") as stage_name:
                stage_windows_player_deployment(
                    version,
                    self.cache_dir,
                    Path(stage_name),
                    lambda message: None,
                    lambda value: None,
                )
        self.events.put(("status", "Favorite product cache is up to date."))

    def auto_sync_worker(self) -> None:
        was_running = False
        while not self.monitor_stop.wait(AUTO_SYNC_POLL_SECONDS):
            running = is_roblox_player_running()
            if running and not was_running and not self.auto_sync_in_progress:
                self.events.put(("roblox_started", None))
            was_running = running

    def auto_sync_started(self) -> None:
        if self.busy or self.auto_sync_in_progress:
            return
        product = self.selected_product()
        if product is None or not self.settings.get("auto_sync"):
            return
        thread = threading.Thread(target=self.auto_sync_check_worker, args=(product,), daemon=True)
        thread.start()

    def auto_sync_check_worker(self, product: WEAOProduct) -> None:
        try:
            current_product = refresh_product_for_auto_sync(product)
        except Exception:
            current_product = product
        self.events.put(("auto_sync_checked", current_product))

    def auto_sync_checked(self, product: WEAOProduct) -> None:
        if self.busy or self.auto_sync_in_progress or not self.settings.get("auto_sync"):
            return
        self.select_product(product)
        local_hash = local_version_hash(self.versions_dir)
        target_hash = product.version_hash
        if local_hash == target_hash:
            return
        if self._last_auto_sync_hash == target_hash:
            return
        self._last_auto_sync_hash = target_hash
        self.auto_sync_in_progress = True
        self.events.put(("auto_sync_prompt", (product, local_hash, target_hash)))

    def auto_sync_prompt(self, payload: tuple[WEAOProduct, str | None, str]) -> None:
        product, local_hash, target_hash = payload
        self.deiconify()
        messagebox.showwarning(
            APP_NAME,
            f"Roblox version is not synced with {product.title}.\n\n"
            f"Current: {local_hash or 'unknown'}\n"
            f"Required: {target_hash}\n\n"
            "Closing Roblox and resyncing now.\n\n"
            "You can turn this off in Settings.",
        )
        close_running_roblox_processes(self.queue_log, wait_seconds=15)
        self.start_install(version_from_hash(product.rbx_version), f"auto-sync {product.title} {product.version}")

    def record_sync(self, product_label: str, version: RobloxVersion, final_dir: Path) -> None:
        history = load_sync_history()
        history.insert(
            0,
            {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "product": product_label,
                "version": version.folder_name,
                "folder": str(final_dir),
            },
        )
        save_sync_history(history)

    def open_logs_folder_clicked(self) -> None:
        try:
            open_folder(self.log_dir)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the logs folder: {exc}")

    def choose_versions_directory(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose Roblox Versions folder",
            initialdir=str(self.versions_dir if self.versions_dir.exists() else default_versions_dir()),
        )
        if not selected:
            return
        path = Path(selected)
        try:
            ensure_safe_versions_dir(path)
        except VersionManagerError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.versions_dir = path
        self.settings["versions_dir"] = str(path)
        save_settings(self.settings)
        self.refresh_local_status()
        self.latest_text.set(f"Roblox folder: {path}")
        self.status_text.set("Roblox folder changed")
        self.append_log(f"Roblox Versions directory changed: {path}")

    def open_history(self) -> None:
        history = load_sync_history()
        window = ctk.CTkToplevel(self)
        window.title("Sync history")
        window.geometry("700x430")
        window.minsize(560, 300)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(window, text="SYNC HISTORY", text_color="#3bea57", anchor="w", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=20, pady=(18, 7)
        )
        text = ctk.CTkTextbox(window, corner_radius=10, fg_color="#10161d", text_color="#c5d0db", wrap="word")
        text.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))
        lines = [
            f"{item.get('timestamp', '')}\n{item.get('product', 'Manual build')}\n{item.get('version', '')}\n{item.get('folder', '')}\n"
            for item in history
        ]
        text.insert("1.0", "\n".join(lines) if lines else "No syncs recorded yet.")
        text.configure(state="disabled")

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

    def update_control_state(self) -> None:
        disabled = self.busy
        state = "disabled" if disabled else "normal"
        for widget in (
            self.logs_button,
            self.shortcut_button,
            self.refresh_versions_button,
            self.refresh_products_button,
            self.sync_product_button,
            self.launch_product_button,
            self.manual_apply_button,
            self.version_menu,
        ):
            widget.configure(state=state)
        self.product_search.configure(state="disabled" if self.busy else "normal")

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        if status:
            self.status_text.set(status)
        self.update_control_state()

    def drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "products":
                if self.products and payload == self.products:
                    continue
                self.products = payload
                self.refresh_tray_menu()
                self.loading_products = False
                self.product_refresh_started = False
                self.rebuild_product_cards()
                if self.products:
                    self.select_product(self.selected_product() or self.products[0])
                self.update_control_state()
                if not self.loading_versions and not self.busy:
                    self.status_text.set("Ready to sync")
            elif event == "weao_current":
                if payload:
                    self.weao_current_text.set(f"WEAO current build: version-{payload}")
                else:
                    self.weao_current_text.set("WEAO current build: unavailable")
            elif event == "versions":
                self.versions = payload
                self.loading_versions = False
                labels = [version.label for version in self.versions]
                self.version_menu.configure(values=labels)
                if labels:
                    self.selected_label.set(labels[0])
                    latest = self.versions[0]
                    self.latest_text.set(f"Latest tracked build: {latest.folder_name} ({latest.file_version})")
                self.update_control_state()
                if not self.loading_products and not self.busy:
                    self.status_text.set("Ready to sync")
            elif event == "status":
                self.append_log(str(payload))
            elif event == "progress":
                self.progress.set(float(payload))
            elif event == "done":
                final_dir, shortcut_path, source_label, version_hash = payload
                self.refresh_local_status()
                self.record_sync(source_label, version_from_hash(version_hash), final_dir)
                self.append_log(f"Synced {source_label} to {final_dir.name}; shortcut updated.")
                self.progress.set(1)
                self.auto_sync_in_progress = False
                self._last_auto_sync_hash = None
                self.set_busy(False, "Sync complete")
                if self.settings.get("relaunch_after_auto_sync") and source_label.startswith("auto-sync"):
                    launch_roblox(final_dir)
            elif event == "error":
                source, error = payload
                if source == "WEAO product catalog":
                    self.loading_products = False
                    self.product_refresh_started = False
                    self.product_count_text.set("WEAO unavailable")
                elif source == "Roblox build history":
                    self.loading_versions = False
                    self.refresh_products(force=False)
                self.update_control_state()
                self.set_busy(False, f"{source} failed")
                self.auto_sync_in_progress = False
                self._last_auto_sync_hash = None
                self.append_log(f"{source} error: {error}")
                messagebox.showerror(APP_NAME, f"{source}: {error}")
            elif event == "roblox_started":
                self.auto_sync_started()
            elif event == "auto_sync_prompt":
                self.auto_sync_prompt(payload)
            elif event == "auto_sync_checked":
                self.auto_sync_checked(payload)

        self.after(100, self.drain_events)

    def on_close(self) -> None:
        try:
            self.monitor_stop.set()
            self.favorite_cache_stop.set()
            self.stop_tray_icon()
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
