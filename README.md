# Yamizome Revenger - Save Editor

A GUI save file editor for **Yamizome Revenger** (ESCUDE engine). Edit character stats, unlock the CG/event gallery, compare saves, and more.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)
![License](https://img.shields.io/badge/License-GPL%20v3-blue)

## Features

- **Edit character stats** — HP, Max HP, Power, EP, Strength Gain, Potion Gain, Skill Gain, and more
- **Max All Stats** — One-click preset to max out all stats
- **CG/Event gallery unlock** — Unlock all gallery entries in `revenger.sys`
- **Thumbnail preview** — View the in-game screenshot embedded in each save file
- **Save comparison** — Byte-level diff between any two save files
- **Automatic backups** — Creates `.bak` files before any modification
- **Backup management** — View count and bulk-delete backups
- **CRC-32/POSIX checksum** — Recomputes the engine checksum so the game accepts edited saves
- **Adaptive UI** — Dark theme, DPI-aware, scales to your screen resolution

## Download

### Option 1: Standalone Executable (no Python needed)

Download `Yamizome Save Editor.exe` from the [Releases](https://github.com/chocovanis/Yamizome-Revenger-Save-Editor/releases) page. Just run it — no installation required.

### Option 2: Run from Source

Requires **Python 3.8+** with tkinter (included in standard Python installs on Windows).

```bash
git clone https://github.com/chocovanis/Yamizome-Revenger-Save-Editor.git
cd Yamizome-Revenger-Save-Editor
python yamizome_save_editor.py
```

## Setup

1. **Run the editor** (exe or Python script)
2. The editor automatically looks for saves in the default location:
   ```
   Documents\ESCUDE\YamizomeRevenger\save
   ```
3. If your saves are elsewhere (e.g. OneDrive Documents), a dialog will prompt you to browse to the correct folder
4. Select a save slot from the left panel, edit stats on the right, and click **Apply Changes**

## Save File Format

Each `save_XXX.dat` file is **726,516 bytes**:

| Region | Offset | Size | Description |
|--------|--------|------|-------------|
| SAVEDATA | `0x000000` | 700,024 bytes | Game state (stats, script position, backlog, etc.) |
| Checksum | `0x0AAE78` | 4 bytes | CRC-32/POSIX, little-endian uint32 |
| Thumbnail | `0x0AAE7C` | 26,488 bytes | 154x86 RGB555 bitmap (bottom-up) |

### Checksum Details

The game validates saves using **CRC-32/POSIX** (aka CRC-32/CKSUM):
- Polynomial: `0x04C11DB7` (MSB-first / unreflected)
- Initial value: `0xFFFFFFFF`
- Final XOR: `0xFFFFFFFF`
- Computed over the first 700,024 bytes (SAVEDATA region)

Without the correct checksum, the game rejects modified saves. This editor recomputes it automatically.

### Character Stat Offsets

All stats are `uint16` little-endian:

| Offset | Stat |
|--------|------|
| `0x09D58A` | HP |
| `0x09D58E` | Max HP |
| `0x09D582` | Power |
| `0x09D586` | Expansion Points (EP) |
| `0x09D592` | Strength Gain |
| `0x09D596` | Potion Gain |
| `0x09D59A` | Skill Gain |
| `0x09D59E` | Stat D (unknown) |
| `0x09D5A2` | Stat E (unknown) |

## Building the Executable

To build the standalone `.exe` yourself:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "Yamizome Save Editor" yamizome_save_editor.py
```

The output will be in the `dist/` folder.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
