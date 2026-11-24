# D-Sync · External Device Auto-Sync Daemon

A resident Windows app. **Detect an external device (USB stick / card reader) → read the YAML config at the card root → copy files to the destination per the config → skip duplicates automatically**, with progress shown live on a local dashboard.

English | [中文](README.zh_cn.md)

---

## Features

| Capability | Details |
|---|---|
| Auto-detection | `WM_DEVICECHANGE` events plus periodic polling as a belt-and-braces approach, so inserts and removals are handled immediately |
| External-device filtering | Only external volumes such as USB sticks and card readers; fixed disks, optical drives and RAM disks are ignored |
| Config-driven | YAML defines source paths, destinations, exclude patterns and conflict policy |
| In-page editing | Add, edit and delete rules directly on the dashboard; saving writes back to the device YAML and takes effect immediately |
| Path picking | Both the source and destination support a "Browse…" button that opens the native OS folder dialog |
| Auto-sync toggle | The `enable` field controls whether inserting the card syncs automatically, switchable on the page |
| Manual trigger | "Sync now" always runs, whether or not `enable` is on |
| Smart dedup | Compares content hashes and skips duplicates, so nothing takes up disk twice |
| Live dashboard | A local web page with SSE progress push, per-file logs and history |
| Background operation | Lives in the system tray, takes no desktop space, and closing the page does not stop syncing |
| Safety boundary | The dashboard binds `127.0.0.1` only and is never exposed to the internet |

---

## Quick start

### 1. Prepare a config file (optional)

Put a `dsync.yml` at the device root:

```yaml
version: 1
enable: true               # master auto-sync switch: false means inserting the card does not sync; trigger it manually
destination_root: "D:/SD_Backup"
overwrite: false           # overwrite when the destination has a same-named file with different content
exclude: ["*.tmp", "Thumbs.db"]

rules:
  - source: "DCIM"           # source directory on the device
    destination: "Photos"    # destination (relative to destination_root)

  - source: "Documents"
    destination: "Documents"
    exclude: ["*.bak"]        # rule-level exclude
```

You can also put **no file on the card at all** and configure everything from the dashboard using "Create config file" (see below). The app ships **no local templates and no default config**; the YAML on the device is always the single source of truth.

### 2. Run

```bash
# from source
python -m dsync

# packaged EXE
dist\D-Sync.exe
```

After startup:

- a tray icon appears; right-click it to open the dashboard or quit
- the dashboard is at `http://127.0.0.1:8765/` by default
- logs go to `%LOCALAPPDATA%\D-Sync\logs\dsync.log`

---

## Dashboard layout

The page is split into five sections from top to bottom:

### ① Device status

**One row per device**, with no global status area any more. Four columns:

| Column | Content |
|---|---|
| Device | The volume label (falls back to the drive letter) |
| State | Detected / Syncing / Synced / Error |
| Progress | `x/y (z skipped) 99%` |
| Elapsed | How many seconds the last sync took |

### ② Select device

The dropdown **lists external devices only** (USB sticks, card readers, ...). It comes with "Refresh list", "Sync now" and "Cancel sync".

- "Sync now" is not subject to the `enable` flag; it always triggers a manual sync
- Selecting a device with no config file hides the config area and shows the create prompt instead

### ③ Sync configuration (shown when a config exists)

- The **auto-sync toggle** sits at the top of the config card and controls whether inserting this device starts a sync
- Global settings: destination root (with browsing), global excludes, overwrite policy
- Rule list: source / destination / exclude / enabled / overwrite, each with a "Browse…" button for picking paths
- A YAML preview on the right, copyable in one click
- "Validate" and "Save config"; saving writes to the device root and takes effect immediately

### ④ No config file yet (shown when there is none)

Just one hint and a "Create config file" button, which opens a blank editor to write rules from scratch.

### ⑤ Activity log / History

- Activity log: per-file copy/skip records with auto-scroll and clear
- History: the time, device, result, copied count, skipped count and elapsed time of every sync

---

## Config file format

The file name is looked up in order: `dsync.yml` → `dsync.yaml`.
Override it on the command line with `--config-name` (repeatable).

Three syntaxes are supported, from simplest to most explicit:

**1) Minimal form**

```yaml
source: DCIM
destination: D:/Photos/DCIM
```

**2) Rule list (recommended)**

```yaml
version: 1
destination_root: D:/SD_Backup
rules:
  - source: DCIM
    destination: Photos/DCIM
  - source: Documents/*.pdf
    destination: Docs
```

**3) Mapping form**

```yaml
destination_root: D:/SD_Backup
mappings:
  DCIM: Photos/DCIM
  Documents: Docs
```

Whatever the syntax, it is normalised into a `rules` list internally.

### Fields

#### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `version` | int | `1` | Config format version |
| `enable` | bool | `true` | Master auto-sync switch. With `false`, inserting the card does not sync automatically but a manual trigger still works |
| `destination_root` | str | `""` | Base directory for relative destinations in the rules |
| `overwrite` | bool | `false` | Whether to overwrite when the destination has a same-named file with different content |
| `exclude` | list | `[]` | Global exclude patterns (glob), applied to every rule |
| `rules` | list | `[]` | The rule list |
| `mappings` | dict | — | Shorthand for `rules` |

#### Rule fields

| Field | Type | Default | Description |
|---|---|---|---|
| `source` | str | required | Source path relative to the device root; may contain `*` / `**` wildcards |
| `destination` | str | required | Destination path. An absolute path is used as-is; a relative one resolves against `destination_root` |
| `exclude` | list | `[]` | Rule-level exclude patterns (glob), relative to the source directory |
| `enabled` | bool | `true` | Whether the rule is active |
| `overwrite` | bool | inherit global | Overwrite policy, overriding the global setting for this rule |

---

## Deduplication logic

For every file to copy:

1. Destination missing → copy it directly
2. Destination present with the **same size** → compute SHA-256 on both sides
   - hashes match → **skip** (treated as a duplicate)
   - hashes differ → follow the `overwrite` policy
3. Destination present with a different size → follow the `overwrite` policy

The skipped count is reflected live in the device row's "Progress" column.

---

## Command-line options

```bash
python -m dsync [options]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Dashboard bind address |
| `--port` | `8765` | Dashboard port; moves to the next free port when taken |
| `--config-name` | `dsync.yml` | Config file name on the device; may be repeated |
| `--include-fixed` | off | Also consider fixed disks (for card readers the OS reports as fixed) |
| `--poll-interval` | `3.0` | Polling interval in seconds |
| `--open-browser` | off | Open the browser automatically on startup |
| `--no-tray` | off | Do not show the system tray icon |
| `--log-level` | `INFO` | Log level |

---

## Project layout

```
D-Sync/
├── dsync/
│   ├── __init__.py
│   ├── __main__.py          # python -m dsync entry point
│   ├── app.py               # app controller: state, devices, config, sync orchestration
│   ├── cli.py               # command-line parsing
│   ├── config.py            # YAML parsing, validation, edit-model conversion
│   ├── devices.py           # Win32 device events + polling detection
│   ├── events.py            # event bus (the SSE data source)
│   ├── filedialog.py        # native path picker
│   ├── logging_setup.py     # logging and data directory
│   ├── syncer.py            # sync engine: rule expansion, excludes, copying, dedup
│   ├── tray.py              # system tray
│   └── web/
│       ├── server.py        # REST API + SSE
│       └── static/          # dashboard front end (plain HTML/CSS/JS, i18n.js for strings)
├── scripts/                 # packaging and verification scripts
├── tests/                   # pytest suites
├── assets/                  # icons
├── run_dsync.py             # PyInstaller entry point
└── dist/D-Sync.exe          # packaged artifact
```

---

## Data and log locations

| Content | Path |
|---|---|
| Logs | `%LOCALAPPDATA%\D-Sync\logs\dsync.log` (keeps 5 rotating files) |
| Dashboard port | `http://127.0.0.1:8765/` |

> During tests the `SDSYNC_DATA_DIR` environment variable redirects the data directory
> to a temp path, so `%LOCALAPPDATA%\D-Sync` is never touched.

---

## Language

The dashboard is bilingual: English by default, Simplified Chinese available from the
language selector in the top bar. The choice is stored in `localStorage` under `dsync.lang`.
All UI strings live in `dsync/web/static/i18n.js`.

---

## FAQ

**Nothing happens when I insert a card?**
1. Check the config file name — by default `dsync.yml` / `dsync.yaml` is looked up at the device root
2. Check that the volume is recognised as an **external device** (USB stick / card reader). Some card readers mount the card as a fixed disk; in that case start with `--include-fixed`
3. Look at the dashboard's "Activity log" or the messages in `%LOCALAPPDATA%\D-Sync\logs\dsync.log`

**It says "the destination cannot be inside the device"?**
That guard prevents recursive copying (copying files back onto the card they came from).
Point `destination_root` at a disk outside the device.

**Does "Sync now" still work after `enable: false`?**
Yes. `enable` only controls **auto-sync on insert**; pressing "Sync now" always runs.
