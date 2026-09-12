# Roblox Version Manager

A Windows GUI for syncing a selected Roblox `WindowsPlayer` build to the newest local Roblox version folder. It uses WEAO's live product catalog to choose the requested build for a product.

The app reads Roblox deployment history from:

```text
https://setup-rbxcdn.github.io/DeployHistory.txt
```

It keeps only unique `WindowsPlayer` versions, shows recent builds in a dropdown, downloads the selected build from RDD, copies the files over the newest local Roblox version folder, and renames that folder to the selected `version-...` hash.

The product catalog is loaded from WEAO's public `/api/status/exploits` endpoint with the required `WEAO-3PService` user-agent. Only visible Windows products with a valid Roblox version hash are shown.

## Setup

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Run the app:

   ```powershell
   python app.py
   ```

## Behavior

- Reads recent `WindowsPlayer` builds from DeployHistory.
- Ignores duplicate hashes.
- Shows only the latest five unique versions in the dropdown.
- Downloads Roblox package manifests and zip files from the official Roblox CDN, following the same manifest flow RDD uses.
- Loads WEAO's Windows product catalog with feature, type, build, and link metadata.
- Searches products by name or product version.
- Syncs directly to the Roblox build reported by a selected WEAO product.
- Provides direct links to a product site and community when WEAO publishes them.
- Keeps manual Roblox build selection available as a fallback.
- Loads the manual Roblox build list immediately and refreshes WEAO data in the background.
- Caches the last WEAO catalog so startup does not wait for the network.
- Lets you change the Roblox Versions directory from **Settings**.
- Supports favorites, product filters, product sorting by type, sync history, and retained local versions.
- Can create a Windows Startup shortcut for background auto-sync.
- Can watch for `RobloxPlayerBeta.exe` launches, compare the selected product build, close Roblox, and resync when needed.
- Can run as a notification-area tray app with quick favorite-product switching.
- Can hourly cache favorite product builds for faster hot-swapping.
- Supports a custom application icon in packaged builds.
- Uses a generated `rvm.ico` icon for the window, taskbar, and File Explorer executable icon.

Rebuild the executable after changing the source or icon.

- Uses `%LOCALAPPDATA%\Roblox\Versions`.
- Deletes old direct `version-*` folders, keeping the newest local folder.
- Copies downloaded files into the newest folder with overwrite behavior.
- Leaves files that are not overwritten in place, preserving local settings.
- Renames the active folder to the selected `version-...` hash.
- Can create or refresh a desktop shortcut to `RobloxPlayerBeta.exe`.
- Can launch Roblox after the update.

## WEAO product catalog

RVM treats every product in the catalog as selectable and lets you choose the Roblox build it reports. RVM does not guarantee that any third-party product is safe or functional. Only use products you trust.

The API is rate limited, so use the refresh buttons only when needed. If WEAO cannot be reached, RVM keeps using the last cached catalog when one is available and reports the endpoint and connection problem in the error message.

## Auto-sync

Enable **Auto-sync current selected product on Roblox launch** in **Settings**. RVM then starts a lightweight background watcher through the Windows Startup folder. When `RobloxPlayerBeta.exe` appears, RVM refreshes the selected product, compares the local active version folder with the product's reported `rbxversion`, and prompts before closing Roblox and syncing if they differ.

Automatic sync is opt-in and disabled by default. If you enable it, keep the app's selected product and Roblox directory configured correctly.

## Packaging

Build a Windows executable with:

```powershell
.\build_exe.ps1
```

The finished app will be created at `dist\RobloxVersionManager.exe`.

If you prefer a double-clickable launcher, run `build_exe.bat` instead.

## Updating The App

Yes, when you change the source code, you should rebuild the executable again if you want the `.exe` to include the latest changes. The source files update the app logic, but the compiled `dist\RobloxVersionManager.exe` only changes after you run the build again.

## Logs

Each run writes a timestamped log file to `logs\` next to the script or executable. If a package download is not a zip, the raw response is also saved under `logs\failed_downloads\` for inspection.

## Safety Notes

The cleanup step only removes direct child folders inside `%LOCALAPPDATA%\Roblox\Versions` whose names start with `version-`. It refuses to operate on folders that do not look like the Roblox Versions directory.
