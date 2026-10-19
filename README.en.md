<div align="center">

# 🐢 WhySlow

**Explains why your computer is slow and helps fix it** — in plain language, without ad-ridden "optimizers".

[🇷🇺 Русский](README.ru.md) · 🇬🇧 English · [⬆ Home](README.md)

</div>

One utility in the spirit of CPU-Z / GPU-Z: hardware info, load, startup items, junk files, processes.
What sets it apart is a **shared knowledge base on GitHub**: WhySlow checks every startup item and every
process against it and tells you whether it is safe to disable, better left alone, or worth removing.

![Overview](docs/overview.png)

## Contents

* [What it does](#what-it-does)
  * [Tabs](#tabs)
  * [Problems it finds](#problems-it-finds)
  * [Safety](#safety)
  * [Platform support](#platform-support)
* [Install and run](#install-and-run)
  * [Download](#download)
  * [Windows](#windows)
  * [macOS](#macos)
  * [Linux](#linux)
  * [Text mode](#text-mode)
  * [Language](#language)
* [Updates](#updates)
  * [Publishing a new version](#publishing-a-new-version)
* [Temperature sensors](#temperature-sensors)
* [Knowledge base](#knowledge-base)
  * [Entry format](#entry-format)
  * [When a program is not in the base](#when-a-program-is-not-in-the-base)
  * [Suggesting an entry](#suggesting-an-entry)
* [How it is built](#how-it-is-built)

## What it does

### Tabs

| Tab | What it does |
|---|---|
| **Overview** | Live CPU, memory, disk and network tiles with charts, what is loading the system right now, and a single **Check** button that lists problems with explanations and a **Fix** button |
| **System** | CPU with every core separately, clock speeds, memory, integrated GPU load, disks, network, temperatures — last-minute charts with a scale and hover tooltips |
| **Startup** | Everything that starts with the system, plus the knowledge base verdict. Toggle items on or off right in the list, or remove them |
| **Processes** | Programs grouped by name (a browser is dozens of processes), with the knowledge base verdict |
| **Cleanup** | Temporary files, caches, recycle bin. Only removes your own files older than one day |
| **Journal** | Everything WhySlow changed. Any startup or proxy change can be rolled back with one click |

### Problems it finds

Each check answers its own question and offers a fix where possible:

* **No internet after a program was closed.** Some programs turn on a system proxy while running and leave it
  on if they crash. Nothing is listening on that proxy anymore, so the browser and other apps cannot go online.
  WhySlow detects this and turns the proxy off with one click (and can roll it back).
* **High CPU load.** Shows which program is causing it and what the knowledge base says about it.
* **Not enough memory.** Top processes by memory and how full the page file is.
* **Low disk space.** Especially on the system drive, plus how much cleanup can free.
* **Overheating.** Component temperatures above normal.
* **Clutter in startup.** What can be disabled, what is better removed, and for items not in the base, what the internet says about them.
* **No reboot for a long time.** A reminder that a restart often helps on its own.

### Safety

* A check **changes nothing**. Every action happens only after you confirm it.
* Startup items are disabled the same way the system's own tools do it:
  * **Windows** — via the `StartupApproved` flag (like Task Manager); Task Scheduler tasks are disabled
    as in Task Scheduler itself; services are set to Manual (Windows will start them if needed);
  * **Linux** — a copy of the `.desktop` file with `Hidden=true` in `~/.config/autostart` (system files are untouched);
  * **macOS** — removed from Login Items; user agents are moved to `~/Library/LaunchAgents/WhySlow-disabled`;
    agents and daemons in `/Library` are disabled with `launchctl disable` (macOS asks for a password for daemons).
* "Remove from startup" keeps a backup of the entry, which you can restore from the Journal.
* If the knowledge base says "Keep", the program asks you once more.

![Startup](docs/startup.png)

### Platform support

| | Windows | Linux | macOS |
|---|---|---|---|
| CPU, memory, disks, processes | ✅ | ✅ | ✅ |
| Graphics | ✅ load (+NVIDIA: temp) | ✅ lspci, NVIDIA and AMD load | ✅ integrated GPU load |
| Temperatures | via LibreHardwareMonitor (CPU, motherboard, disks) + NVIDIA | ✅ | ✅ Apple Silicon: CPU, storage, battery, no password |
| Startup | Run and RunOnce registry keys, Startup folder, Task Scheduler, services | XDG autostart + systemd | Login Items, LaunchAgents, LaunchDaemons |
| Stale proxy | ✅ + fix | ✅ GNOME + fix, environment variables | — |
| Cleanup | Temp, browser cache, dumps, updates, recycle bin | cache, thumbnails, /tmp, trash | cache, logs, trash |

## Install and run

You need **Python 3.10 or newer**. The launch scripts install everything else.

### Download

* **Archive:** on the [Releases](https://github.com/moggiler0x/WhySlow/releases) page download `Source code (zip)`
  for the latest version and unpack it anywhere. Or use the green **Code → Download ZIP** button on the main page.
* **With git:**
  ```bash
  git clone https://github.com/moggiler0x/WhySlow.git
  cd WhySlow
  ```

Both ways can be updated from inside the program.

### Windows

1. Install Python from [python.org](https://www.python.org/downloads/) and tick **Add python.exe to PATH** during setup.
2. Double-click `run.bat` in the program folder. On first launch it installs the required libraries.
3. To change startup items "for all users" and see all temperatures, click
   **Restart as administrator** at the bottom of the window.

For CPU, motherboard and disk temperatures, see [Temperature sensors](#temperature-sensors).

### macOS

1. Python: [python.org](https://www.python.org/downloads/) or `brew install python`.
2. Open **Terminal**, go to the program folder and run:
   ```bash
   cd ~/Downloads/WhySlow   # your folder
   ./run.sh
   ```
   If macOS says "Permission denied", run `chmod +x run.sh` once.
3. If the program says it cannot verify GitHub's certificate (happens with Python from python.org),
   run `pip3 install certifi` or open `Install Certificates.command` from the Python folder in Applications.

On Apple Silicon temperatures are read without a password or extra software.

### Linux

```bash
./run.sh
# or manually
pip install -r requirements.txt
python3 -m whyslow
```

The window needs PySide6 (installed from `requirements.txt`). Temperatures come from the system automatically.

### Text mode

No window, right in the terminal:

```bash
python3 -m whyslow --cli       # check and report
python3 -m whyslow --version   # version
python3 -m whyslow --update    # update (asks for confirmation)
python3 -m whyslow --cli --lang en   # language for this run: en or ru
```

### Language

The program speaks English and Russian. By default it follows the system language; switch it with the
language list at the top of the window (the program restarts in the chosen language and remembers it).

## Updates

WhySlow finds new versions in this repository by itself. The **Updates** button at the bottom of the window
shows what is new and installs the version after you confirm. When a new version is out,
the button lights up: "Update to …".

* **Check on startup** is on by default: the program only tells you about a new version.
* **Install automatically** is off by default. When on, the new version installs itself
  at launch and you only need to click **Restart**.
* If a new version fails to start, the old one comes back automatically. A successful update
  can also be rolled back from the Journal tab.

How an update is installed depends on how you got the program:

* **git clone** — through git, onto the new version's tag. If you edited the program files, the update
  refuses so it does not overwrite your changes. `git pull` still works.
* **ZIP archive** — the new version's archive is downloaded, current files are saved to the data folder
  (`updates/`) and replaced.

### Publishing a new version

1. Bump `__version__` in [`whyslow/__init__.py`](whyslow/__init__.py) and commit it to `main`.
2. Create a tag with the same version and push it:
   ```bash
   git tag v0.3.0
   git push origin v0.3.0
   ```
3. GitHub Actions ([`release.yml`](.github/workflows/release.yml)) checks that the tag matches the version in code
   and creates a release with the list of changes. The program shows the release text in the Updates window;
   you can edit it on GitHub.

Without a terminal: on GitHub go to **Releases → Draft a new release**, type `v0.3.0` as the tag
(branch `main`), click **Generate release notes** and **Publish release**.

Installed copies see the new version as soon as the tag exists, even before the release is created.

## Temperature sensors

* **macOS (Apple Silicon)** and **Linux** — temperatures are read automatically, nothing to install.
* **NVIDIA graphics cards** — temperature comes from the driver on every system.
* **Windows** does not expose CPU, motherboard and disk temperatures by itself. WhySlow gets them from
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases) in one of two ways:
  1. **Easiest:** run LibreHardwareMonitor as administrator and keep it open.
     WhySlow reads the sensors via WMI, no setup needed.
  2. **Without the LHM window:** put `LibreHardwareMonitorLib.dll` and `HidSharp.dll` from the LHM archive
     into `%LOCALAPPDATA%\WhySlow\lhm` (or point the `WHYSLOW_LHM_DIR` variable at a folder),
     run `pip install pythonnet` and start WhySlow as administrator.
     If LHM is installed in `Program Files\LibreHardwareMonitor`, the DLLs are found there automatically.

If there are no sensors, the System tab and the check explain what is missing.

## Knowledge base

File: [`whyslow/data/known.json`](whyslow/data/known.json). On launch the program downloads the latest version
from the `main` branch here and caches it. The built-in copy works offline.

### Entry format

```json
{
  "id": "discord",
  "title": "Discord",
  "title_en": "Discord",
  "category": "Мессенджер",
  "category_en": "Messenger",
  "verdict": "safe",
  "match": ["discord"],
  "note": "Мессенджер. Запустите вручную, когда понадобится.",
  "note_en": "Messenger. Start it manually when you need it."
}
```

* `title`, `category`, `note` are in Russian; `title_en`, `category_en`, `note_en` are the English versions.
  If an English field is missing, the program shows the Russian text.

* `verdict`: `safe` — can be disabled, `optional` — up to you, `keep` — leave alone, `bad` — better removed.
* `match`: regular expressions (case-insensitive), matched against the name and the launch command.
  For Windows services the short service name is appended to the end of the line, so it is easy to match as `\bwsearch$` (in JSON: `\\bwsearch$`).
  Entries are checked top to bottom; the first match wins.
* After editing, bump `version` at the top of the file, otherwise installed copies will not pick up the change.

### When a program is not in the base

WhySlow finds out what it is so you don't have to search:

1. it reads details from the file itself: description, product name and publisher
   (on Windows from the `.exe` properties, on macOS from the app's `Info.plist`);
2. it looks up a Wikipedia article, Russian first, then English. No keys or sign-up needed.

The result is shown in the details on the Startup and Processes tabs, marked
"from the internet, unverified" with a link to the source. A "safe to disable" verdict is never
based on this data: only the knowledge base itself helps decide what to disable.
Answers are cached in `online.json` next to the base cache (hits for a month, misses for three days).

### Suggesting an entry

* In the program, select an item on the Startup or Processes tab and click **Suggest to the base…** —
  a ready-made issue opens with the found description already filled in.
* Or send a pull request that edits [`known.json`](whyslow/data/known.json).

## How it is built

```
whyslow/
  gui.py         interface (PySide6)
  cli.py         text mode
  checks.py      diagnostics → list of findings
  knowledge.py   knowledge base: loading, updating from GitHub, matching
  online.py      describing unknown programs: file details and Wikipedia
  startup.py     startup items on Windows / Linux / macOS
  processes.py   processes grouped by program
  junk.py        finding and removing junk
  network.py     system proxy check
  sysinfo.py     hardware info, integrated GPU load
  lhm.py         Windows temperatures via LibreHardwareMonitor
  macsensors.py  Mac temperatures and GPU load without admin rights
  journal.py     change journal for rollback
  updater.py     updating the program from GitHub
  settings.py    settings (language, update checks and auto-install)
  i18n.py        interface language: Russian and English
  data/known.json
```

Program data (base cache, journal, settings, backups, previous versions): `%LOCALAPPDATA%\WhySlow` on Windows,
`~/.local/share/whyslow` on Linux and macOS.

### Roadmap

* Single-file `.exe` build (PyInstaller)
