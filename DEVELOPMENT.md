# Development Notes

Technical documentation covering how this save editor was developed, including the reverse-engineering process used to understand the save file format.

## How the Save Format Was Reverse-Engineered

### Step 1: Archive Extraction

The game's script archive (`revenger.bin`) uses the **ESC-ARC2** format:
- XOR encryption key: `0x65AC9365`
- Files are LZW-compressed with magic bytes `acp\0` (`0x61637000`)
- Contains 93 script files that control all game logic

The archive was extracted and decompressed to obtain the game's script source code.

### Step 2: Identifying Save/Load Logic

The decompressed scripts revealed two key files:
- **`global.h`** — Defines the `SAVEDATA` struct (700,024 bytes) containing all game state
- **`global.c`** — Contains the save and load functions

The save function (line ~1747 of `global.c`) computes a checksum:
```c
crc = crc32(&save_data, sizeof(SAVEDATA));
```

The load function (line ~1787) verifies it:
```c
if (crc != crc32(&save_data, sizeof(SAVEDATA))) {
    // reject save
}
```

### Step 3: Identifying the CRC Algorithm

The engine's `crc32()` function is a built-in trap declared in `acpx.h`:
```c
#trap uint crc32(pData, nSize);
```

This maps to a native function in the game executable. Ghidra decompilation revealed two CRC functions:
- `FUN_0052d9d0` — Standard reflected CRC-32 (polynomial `0xEDB88320`)
- `FUN_0052ddd0` — **Big-endian/unreflected CRC-32** (polynomial `0x04C11DB7`)

The `crc32` trap uses the second function. Testing confirmed the algorithm is **CRC-32/POSIX** (aka CRC-32/CKSUM):
- Polynomial: `0x04C11DB7` (MSB-first, unreflected)
- Initial value: `0xFFFFFFFF`
- Final XOR: `0xFFFFFFFF`
- **Not** the standard CRC-32 used by zlib/gzip

This was verified against all 18 existing save files — 100% match.

### Step 4: Locating Stats

Stats were found by:
1. Making in-game changes (leveling up, using items)
2. Saving before and after
3. Binary-diffing the save files
4. Cross-referencing with the `SAVEDATA` struct definition in `global.h`

Stats are stored as `uint16` little-endian values in the upper 16 bits of `uint32` fields, at offsets starting from `0x09D582`.

### Step 5: Thumbnail Format

The save file thumbnail was identified from `global.h`:
- `FILE_PREVIEW_W = 154`, `FILE_PREVIEW_H = 86`
- Format: RGB555 (16-bit color, little-endian, bottom-up bitmap)
- Located immediately after the 4-byte CRC at offset `0x0AAE7C`

### Step 6: System File (CG Gallery)

The `revenger.sys` file (141,516 bytes) stores global game state including the CG gallery:
- CG unlock bitmask: bytes `0x068` through `0x1A7`
- No checksum protection — direct bit manipulation works
- Setting all bytes to `0xFF` unlocks the full gallery

## Key Design Decisions

### Why CRC-32/POSIX and not standard CRC-32?

The ESCUDE engine implements both reflected (standard) and unreflected (big-endian) CRC-32 variants. The save system specifically uses the unreflected variant. Standard `zlib.crc32()` will produce wrong checksums — the editor includes a pure-Python implementation of CRC-32/POSIX to ensure correctness.

### Why PPM P6 for thumbnails?

The thumbnail is decoded from RGB555 to RGB888 for display. Initial attempts used PPM P3 (text format), which proved unreliable in tkinter on Windows. Switching to PPM P6 (binary format) resolved all display issues.

### Dynamic window sizing

The editor detects screen resolution at startup and sizes the window as a percentage of the display (~47% width, ~75% height). This ensures the UI is usable on both 1080p and 4K displays without manual resizing.

## Tools Used

- **GARbro** — Extraction of game archives (`revenger.bin`, `data.bin`)
- **Ghidra** — Decompilation of `revenger.exe` (32-bit x86)
- **Python 3** — Editor implementation (tkinter GUI)
- **PyInstaller** — Standalone executable packaging
- **Claude Code** — AI-assisted development and reverse-engineering
