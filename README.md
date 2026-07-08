# Roblox Version Manager

A small Windows GUI for applying a selected Roblox `WindowsPlayer` build to the newest local Roblox version folder.

The app reads Roblox deployment history from:

```text
https://setup-rbxcdn.github.io/DeployHistory.txt
```

It keeps only unique `WindowsPlayer` versions, shows recent builds in a dropdown, downloads the selected build from RDD, copies the files over the newest local Roblox version folder, and renames that folder to the selected `version-...` hash.

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

- Uses `%LOCALAPPDATA%\Roblox\Versions`.
- Deletes old direct `version-*` folders, keeping the newest local folder.
- Copies downloaded files into the newest folder with overwrite behavior.
- Leaves files that are not overwritten in place, preserving local settings.
- Renames the active folder to the selected `version-...` hash.
- Can create or refresh a desktop shortcut to `RobloxPlayerBeta.exe`.
- Can launch Roblox after the update.

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
