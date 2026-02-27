#!/usr/bin/env python3
"""
Yamizome Revenger - Save File Editor
=====================================
A GUI tool for viewing and editing save files for Yamizome Revenger (ESCUDE).

Save files are unencrypted little-endian binary. Stats are stored as uint16
values at fixed offsets within each save_XXX.dat file.

The system file (revenger.sys) contains CG/event gallery unlock bitmasks.
"""

import os
import sys
import struct
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from pathlib import Path

# ─── Constants ───────────────────────────────────────────────────────────────

DEFAULT_SAVE_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "ESCUDE", "YamizomeRevenger", "save"
)

SAVE_FILE_SIZE = 726516
SYS_FILE_NAME = "revenger.sys"
SYS_FILE_SIZE = 141516
BACKUP_SUFFIX = ".bak"

# CRC-32/POSIX checksum: computed over the SAVEDATA region, stored as uint32 LE
SAVEDATA_SIZE = 700024   # sizeof(SAVEDATA) in the ESCUDE engine
CRC_OFFSET = SAVEDATA_SIZE  # 0xAAE78 - 4-byte CRC immediately after SAVEDATA

# Stat field definitions: (offset, name, description, min_val, max_val)
# Values are uint16 LE at the given offset
STAT_FIELDS = [
    (0x09D58A, "HP",               "Current Health Points",       0, 65535),
    (0x09D58E, "Max HP",           "Maximum Health Points",       0, 65535),
    (0x09D582, "Power",            "Attack / Power stat",         0, 65535),
    (0x09D586, "Expansion Points", "Expansion Points (EP)",       0, 65535),
    (0x09D592, "Strength Gain",    "Health gain per rest",         0, 65535),
    (0x09D596, "Potion Gain",      "Potion effectiveness gain",   0, 65535),
    (0x09D59A, "Skill Gain",       "Skill learning speed",        0, 65535),
    (0x09D59E, "Stat D",           "Unknown stat D (45-319 range)",0, 65535),
    (0x09D5A2, "Stat E",           "Unknown stat E (416-552 range)",0, 65535),
]

# Dialogue text location
DIALOGUE_OFFSET = 0x1F349
DIALOGUE_MAX_LEN = 200

# Script position (uint32 LE) - represents position in game script
SCRIPT_POS_OFFSET = 0x1F340

# Thumbnail (RGB555, 154x86 pixels, little-endian uint16 per pixel)
THUMB_OFFSET = CRC_OFFSET + 4  # 0xAAE7C - immediately after the 4-byte CRC
THUMB_WIDTH = 154
THUMB_HEIGHT = 86
THUMB_SIZE = THUMB_WIDTH * THUMB_HEIGHT * 2

# CG gallery bitmask in revenger.sys
CG_BITMASK_START = 0x068
CG_BITMASK_END = 0x1A7  # inclusive


# ─── CRC-32/POSIX Checksum ──────────────────────────────────────────────────
# The ESCUDE engine validates saves with CRC-32/POSIX (aka CRC-32/CKSUM):
#   Polynomial: 0x04C11DB7 (MSB-first / unreflected)
#   Initial value: 0xFFFFFFFF
#   Final XOR: 0xFFFFFFFF
# Computed over the first SAVEDATA_SIZE bytes, stored as uint32 LE at CRC_OFFSET.

_CRC32_TABLE = []
for _i in range(256):
    _crc = _i << 24
    for _ in range(8):
        if _crc & 0x80000000:
            _crc = ((_crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            _crc = (_crc << 1) & 0xFFFFFFFF
    _CRC32_TABLE.append(_crc)


def compute_crc32(data):
    """Compute CRC-32/POSIX over data bytes (matching ESCUDE engine crc32 trap)."""
    crc = 0xFFFFFFFF
    table = _CRC32_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[(crc >> 24) ^ byte]
    return crc ^ 0xFFFFFFFF


# ─── Helper Functions ────────────────────────────────────────────────────────

def read_uint16(data, offset):
    """Read a little-endian uint16 from binary data."""
    return struct.unpack_from('<H', data, offset)[0]


def write_uint16(data, offset, value):
    """Write a little-endian uint16 into a bytearray."""
    struct.pack_into('<H', data, offset, value)


def read_uint32(data, offset):
    """Read a little-endian uint32 from binary data."""
    return struct.unpack_from('<I', data, offset)[0]


def read_dialogue(data):
    """Extract the dialogue text string from save data."""
    end = data.find(b'\x00', DIALOGUE_OFFSET)
    if end == -1 or end > DIALOGUE_OFFSET + DIALOGUE_MAX_LEN:
        end = DIALOGUE_OFFSET + DIALOGUE_MAX_LEN
    try:
        return data[DIALOGUE_OFFSET:end].decode('utf-8', errors='replace')
    except Exception:
        return "(unreadable)"


def decode_thumbnail_to_photoimage(data):
    """Decode RGB555 thumbnail data to a tk.PhotoImage (binary PPM P6 format)."""
    try:
        raw = data[THUMB_OFFSET:THUMB_OFFSET + THUMB_SIZE]
        if len(raw) < THUMB_SIZE:
            return None

        # Build binary PPM (P6) - much more reliable than text P3 in tkinter
        header = f"P6\n{THUMB_WIDTH} {THUMB_HEIGHT}\n255\n".encode('ascii')
        pixels = bytearray(THUMB_WIDTH * THUMB_HEIGHT * 3)
        for i in range(0, THUMB_SIZE, 2):
            pixel = struct.unpack_from('<H', raw, i)[0]
            j = (i // 2) * 3
            pixels[j]     = ((pixel >> 10) & 0x1F) << 3
            pixels[j + 1] = ((pixel >> 5) & 0x1F) << 3
            pixels[j + 2] = (pixel & 0x1F) << 3

        return header + bytes(pixels)
    except Exception:
        return None


def count_unlocked_cgs(sys_data):
    """Count how many CG bits are set in revenger.sys."""
    total_bits = 0
    set_bits = 0
    for i in range(CG_BITMASK_START, CG_BITMASK_END + 1):
        byte = sys_data[i]
        for bit in range(8):
            total_bits += 1
            if byte & (1 << bit):
                set_bits += 1
    return set_bits, total_bits


# ─── Save File Data Model ───────────────────────────────────────────────────

class SaveSlot:
    """Represents a single save file."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.slot_number = self._extract_slot_number()
        self.modified_time = os.path.getmtime(filepath)
        self.data = None
        self.original_data = None
        self.stats = {}
        self.dialogue = ""
        self.script_pos = 0

    def _extract_slot_number(self):
        name = os.path.splitext(self.filename)[0]
        try:
            return int(name.split('_')[1])
        except (IndexError, ValueError):
            return 0

    def load(self):
        """Load and parse the save file."""
        with open(self.filepath, 'rb') as f:
            self.data = bytearray(f.read())
        self.original_data = bytes(self.data)

        if len(self.data) != SAVE_FILE_SIZE:
            raise ValueError(
                f"Unexpected file size: {len(self.data)} (expected {SAVE_FILE_SIZE})"
            )

        # Read all stat fields
        for offset, name, desc, min_v, max_v in STAT_FIELDS:
            self.stats[name] = read_uint16(self.data, offset)

        self.dialogue = read_dialogue(self.data)
        self.script_pos = read_uint32(self.data, SCRIPT_POS_OFFSET)

    def get_stat(self, name):
        return self.stats.get(name, 0)

    def set_stat(self, name, value):
        self.stats[name] = value
        for offset, field_name, desc, min_v, max_v in STAT_FIELDS:
            if field_name == name:
                write_uint16(self.data, offset, value)
                break

    def has_changes(self):
        return self.data != self.original_data

    def save(self, backup=True):
        """Write modified data back to the save file, recomputing CRC."""
        if backup:
            backup_path = self.filepath + BACKUP_SUFFIX
            if not os.path.exists(backup_path):
                shutil.copy2(self.filepath, backup_path)

        # Recompute CRC-32/POSIX over the SAVEDATA region
        crc = compute_crc32(self.data[:SAVEDATA_SIZE])
        struct.pack_into('<I', self.data, CRC_OFFSET, crc)

        with open(self.filepath, 'wb') as f:
            f.write(self.data)
        self.original_data = bytes(self.data)

    def get_modified_str(self):
        dt = datetime.fromtimestamp(self.modified_time)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


# ─── Main GUI Application ───────────────────────────────────────────────────

class SaveEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Yamizome Revenger - Save Editor")

        # Dynamic window sizing based on screen resolution
        # ~47% screen width, ~75% screen height → smaller on 1080p, larger on 4K
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        win_w = max(900, int(screen_w * 0.47))
        win_h = max(750, int(screen_h * 0.75))
        # Center the window on screen
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.minsize(850, 650)

        self.save_dir = self._find_save_directory()
        self.save_slots = []
        self.current_slot = None
        self.stat_vars = {}   # name -> tk.StringVar
        self.stat_entries = {} # name -> Entry widget
        self.sys_data = None

        self._build_ui()

        if os.path.isdir(self.save_dir):
            self._load_directory()
        else:
            self._prompt_for_directory()

    # ── Save Directory Detection ────────────────────────────────────────

    @staticmethod
    def _find_save_directory():
        """Try to locate the Yamizome Revenger save directory automatically.

        Checks the standard Documents/ESCUDE/YamizomeRevenger/save path.
        Returns the path if found, or DEFAULT_SAVE_DIR as a fallback.
        """
        # Primary: standard Windows Documents location
        if os.path.isdir(DEFAULT_SAVE_DIR):
            return DEFAULT_SAVE_DIR

        # Fallback: check alternate Documents paths (OneDrive, custom, etc.)
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Documents", "ESCUDE", "YamizomeRevenger", "save"),
            os.path.join(home, "OneDrive", "Documents", "ESCUDE", "YamizomeRevenger", "save"),
            os.path.join(home, "My Documents", "ESCUDE", "YamizomeRevenger", "save"),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return path

        return DEFAULT_SAVE_DIR  # Will trigger the browse prompt

    def _prompt_for_directory(self):
        """Show a message that save directory was not found, prompt user to browse."""
        self.status_var.set("Save directory not found - please browse to locate it")
        self.dir_var.set("(not found) " + self.save_dir)

        # Show a dialog after the window appears
        self.root.after(300, self._show_directory_prompt)

    def _show_directory_prompt(self):
        """Display a dialog asking the user to locate their save directory."""
        result = messagebox.askokcancel(
            "Save Directory Not Found",
            "The default save directory was not found:\n\n"
            f"  {DEFAULT_SAVE_DIR}\n\n"
            "This usually means the game stores saves in a different location.\n"
            "Expected folder structure: Documents/ESCUDE/YamizomeRevenger/save\n\n"
            "Click OK to browse for your save folder, or Cancel to set it later\n"
            "using the 'Browse Folder...' button."
        )
        if result:
            self._browse_folder()

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        self.root.configure(bg='#1e1e2e')

        style = ttk.Style()
        style.theme_use('clam')

        # Dark theme colors
        bg = '#1e1e2e'
        fg = '#cdd6f4'
        accent = '#89b4fa'
        surface = '#313244'
        surface2 = '#45475a'
        red = '#f38ba8'
        green = '#a6e3a1'
        yellow = '#f9e2af'

        style.configure('.', background=bg, foreground=fg, fieldbackground=surface)
        style.configure('TFrame', background=bg)
        style.configure('TLabel', background=bg, foreground=fg, font=('Segoe UI', 10))
        style.configure('Header.TLabel', background=bg, foreground=accent,
                        font=('Segoe UI', 14, 'bold'))
        style.configure('SubHeader.TLabel', background=bg, foreground=yellow,
                        font=('Segoe UI', 11, 'bold'))
        style.configure('Stat.TLabel', background=bg, foreground=fg,
                        font=('Segoe UI', 10))
        style.configure('TButton', background=surface2, foreground=fg,
                        font=('Segoe UI', 10), padding=6)
        style.map('TButton',
                  background=[('active', accent), ('pressed', accent)],
                  foreground=[('active', bg), ('pressed', bg)])
        style.configure('Save.TButton', background='#a6e3a1', foreground='#1e1e2e',
                        font=('Segoe UI', 11, 'bold'), padding=8)
        style.map('Save.TButton',
                  background=[('active', '#94e2d5'), ('pressed', '#94e2d5')])
        style.configure('Danger.TButton', background=red, foreground='#1e1e2e',
                        font=('Segoe UI', 10, 'bold'), padding=6)
        style.configure('TLabelframe', background=bg, foreground=accent)
        style.configure('TLabelframe.Label', background=bg, foreground=accent,
                        font=('Segoe UI', 10, 'bold'))

        # Treeview styling
        style.configure('Treeview', background=surface, foreground=fg,
                        fieldbackground=surface, rowheight=28,
                        font=('Segoe UI', 9))
        style.configure('Treeview.Heading', background=surface2, foreground=accent,
                        font=('Segoe UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', accent)],
                  foreground=[('selected', bg)])

        # ── Top bar ──
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        ttk.Label(top_frame, text="Yamizome Revenger - Save Editor",
                  style='Header.TLabel').pack(side=tk.LEFT)

        btn_frame = ttk.Frame(top_frame)
        btn_frame.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="Browse Folder...",
                   command=self._browse_folder).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Refresh",
                   command=self._load_directory).pack(side=tk.LEFT, padx=4)

        # ── Directory label ──
        self.dir_var = tk.StringVar(value=self.save_dir)
        dir_label = ttk.Label(self.root, textvariable=self.dir_var,
                              font=('Segoe UI', 8), foreground='#6c7086')
        dir_label.pack(fill=tk.X, padx=12, pady=(0, 5))

        # ── Main content pane ──
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # LEFT: Save list
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)

        ttk.Label(left_frame, text="Save Files",
                  style='SubHeader.TLabel').pack(anchor=tk.W, padx=5, pady=(0, 5))

        tree_frame = ttk.Frame(left_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ('slot', 'date', 'hp', 'power', 'ep')
        self.tree = ttk.Treeview(tree_frame, columns=columns, show='headings',
                                 selectmode='browse')
        self.tree.heading('slot', text='Slot')
        self.tree.heading('date', text='Last Modified')
        self.tree.heading('hp', text='HP')
        self.tree.heading('power', text='Power')
        self.tree.heading('ep', text='EP')

        self.tree.column('slot', width=50, minwidth=40, anchor=tk.CENTER)
        self.tree.column('date', width=150, minwidth=120)
        self.tree.column('hp', width=60, minwidth=50, anchor=tk.CENTER)
        self.tree.column('power', width=60, minwidth=50, anchor=tk.CENTER)
        self.tree.column('ep', width=50, minwidth=40, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind('<<TreeviewSelect>>', self._on_select)

        # CG Unlock button under save list
        sys_frame = ttk.LabelFrame(left_frame, text="System File (revenger.sys)")
        sys_frame.pack(fill=tk.X, padx=5, pady=(10, 5))

        self.cg_label_var = tk.StringVar(value="CG Gallery: --")
        ttk.Label(sys_frame, textvariable=self.cg_label_var).pack(
            anchor=tk.W, padx=10, pady=2)

        ttk.Button(sys_frame, text="Unlock All CGs / Events",
                   command=self._unlock_all_cgs).pack(padx=10, pady=(2, 8))

        # Maintenance
        maint_frame = ttk.LabelFrame(left_frame, text="Maintenance")
        maint_frame.pack(fill=tk.X, padx=5, pady=(5, 5))

        self.backup_label_var = tk.StringVar(value="Backups: --")
        ttk.Label(maint_frame, textvariable=self.backup_label_var).pack(
            anchor=tk.W, padx=10, pady=2)

        ttk.Button(maint_frame, text="Delete All Backups",
                   style='Danger.TButton',
                   command=self._delete_all_backups).pack(padx=10, pady=(2, 8))

        # RIGHT: Editor panel
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=1)

        # Action buttons - fixed at top of right panel (always visible)
        btn_bar = ttk.Frame(right_frame)
        btn_bar.pack(fill=tk.X, padx=5, pady=(5, 0))

        ttk.Button(btn_bar, text="Apply Changes", style='Save.TButton',
                   command=self._apply_changes).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_bar, text="Revert", command=self._revert).pack(
            side=tk.LEFT, padx=5)
        ttk.Button(btn_bar, text="Max All Stats",
                   command=self._max_all_stats).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_bar, text="Compare Two Saves...",
                   command=self._open_compare_dialog).pack(side=tk.RIGHT, padx=5)

        ttk.Separator(right_frame, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=5, pady=5)

        # Scrollable editor area
        self._editor_canvas = tk.Canvas(right_frame, bg='#1e1e2e',
                                        highlightthickness=0)
        editor_scroll = ttk.Scrollbar(right_frame, orient=tk.VERTICAL,
                                      command=self._editor_canvas.yview)
        self.editor_frame = ttk.Frame(self._editor_canvas)

        self.editor_frame.bind(
            '<Configure>',
            lambda e: self._editor_canvas.configure(
                scrollregion=self._editor_canvas.bbox('all'))
        )
        self._editor_canvas.create_window((0, 0), window=self.editor_frame,
                                          anchor=tk.NW)
        self._editor_canvas.configure(yscrollcommand=editor_scroll.set)

        self._editor_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        editor_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Mousewheel scrolling
        def _on_mousewheel(event):
            self._editor_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), "units")
        self._editor_canvas.bind_all('<MouseWheel>', _on_mousewheel)

        self._build_editor_panel()

        # Rescale thumbnail when canvas resizes
        self._thumb_zoom = 0
        self._editor_canvas.bind('<Configure>', self._on_canvas_resize)

        # ── Bottom status bar ──
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=10, pady=(5, 10))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_frame, textvariable=self.status_var,
                  font=('Segoe UI', 9), foreground='#6c7086').pack(side=tk.LEFT)

    def _build_editor_panel(self):
        """Build the stat editing fields in the right panel."""
        frame = self.editor_frame

        # Header
        ttk.Label(frame, text="Edit Save Data",
                  style='SubHeader.TLabel').grid(
            row=0, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(5, 2))

        # Dialogue preview
        ttk.Label(frame, text="Scene Text:", style='Stat.TLabel').grid(
            row=1, column=0, sticky=tk.W, padx=10, pady=2)
        self.dialogue_var = tk.StringVar(value="(no save selected)")
        self.dialogue_label = ttk.Label(frame, textvariable=self.dialogue_var,
                                        wraplength=380, foreground='#94e2d5',
                                        font=('Segoe UI', 9, 'italic'))
        self.dialogue_label.grid(row=1, column=1, columnspan=2,
                                 sticky=tk.W, padx=5, pady=2)

        # Script position (read-only display)
        ttk.Label(frame, text="Script Position:", style='Stat.TLabel').grid(
            row=2, column=0, sticky=tk.W, padx=10, pady=2)
        self.script_pos_var = tk.StringVar(value="--")
        ttk.Label(frame, textvariable=self.script_pos_var,
                  foreground='#6c7086').grid(
            row=2, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=3, column=0, columnspan=3, sticky=tk.EW, padx=10, pady=8)

        # Stat fields
        ttk.Label(frame, text="Character Stats",
                  style='SubHeader.TLabel').grid(
            row=4, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 5))

        row = 5
        for offset, name, desc, min_v, max_v in STAT_FIELDS:
            ttk.Label(frame, text=f"{name}:", style='Stat.TLabel').grid(
                row=row, column=0, sticky=tk.W, padx=(15, 5), pady=3)

            var = tk.StringVar(value="0")
            self.stat_vars[name] = var

            entry = tk.Entry(frame, textvariable=var, width=10,
                            font=('Consolas', 11),
                            bg='#313244', fg='#cdd6f4',
                            insertbackground='#cdd6f4',
                            relief=tk.FLAT, highlightthickness=1,
                            highlightcolor='#89b4fa',
                            highlightbackground='#45475a')
            entry.grid(row=row, column=1, sticky=tk.W, padx=5, pady=3)
            self.stat_entries[name] = entry

            ttk.Label(frame, text=desc, foreground='#6c7086',
                      font=('Segoe UI', 8)).grid(
                row=row, column=2, sticky=tk.W, padx=5, pady=3)

            row += 1

        # Thumbnail preview
        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, padx=10, pady=8)
        row += 1

        ttk.Label(frame, text="Thumbnail Preview",
                  style='SubHeader.TLabel').grid(
            row=row, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 5))
        row += 1

        self.thumb_label = tk.Label(frame, bg='#313244', relief=tk.SUNKEN, bd=1)
        self.thumb_label.grid(row=row, column=0, columnspan=3,
                              padx=15, pady=5, sticky=tk.W)

    # ── Directory & File Loading ─────────────────────────────────────────

    def _browse_folder(self):
        path = filedialog.askdirectory(initialdir=self.save_dir,
                                       title="Select Save File Directory")
        if path:
            self.save_dir = path
            self.dir_var.set(path)
            self._load_directory()

    def _load_directory(self):
        """Scan the save directory and load all save files."""
        self.save_slots.clear()
        self.tree.delete(*self.tree.get_children())
        self.current_slot = None

        if not os.path.isdir(self.save_dir):
            self.status_var.set(f"Directory not found: {self.save_dir}")
            self.dir_var.set("(not found) " + self.save_dir)
            return

        self.dir_var.set(self.save_dir)

        # Load save files
        for fname in sorted(os.listdir(self.save_dir)):
            if fname.startswith('save_') and fname.endswith('.dat'):
                fpath = os.path.join(self.save_dir, fname)
                if os.path.getsize(fpath) == SAVE_FILE_SIZE:
                    try:
                        slot = SaveSlot(fpath)
                        slot.load()
                        self.save_slots.append(slot)
                    except Exception as e:
                        print(f"Warning: Failed to load {fname}: {e}")

        # Populate treeview
        for slot in self.save_slots:
            self.tree.insert('', tk.END, iid=str(slot.slot_number),
                             values=(
                                 f"{slot.slot_number:03d}",
                                 slot.get_modified_str(),
                                 slot.get_stat('HP'),
                                 slot.get_stat('Power'),
                                 slot.get_stat('Expansion Points'),
                             ))

        # Load system file
        sys_path = os.path.join(self.save_dir, SYS_FILE_NAME)
        if os.path.isfile(sys_path):
            with open(sys_path, 'rb') as f:
                self.sys_data = bytearray(f.read())
            unlocked, total = count_unlocked_cgs(self.sys_data)
            self.cg_label_var.set(f"CG Gallery: {unlocked}/{total} unlocked")
        else:
            self.sys_data = None
            self.cg_label_var.set("CG Gallery: revenger.sys not found")

        # Count backup files
        self._update_backup_count()

        if self.save_slots:
            self.status_var.set(
                f"Loaded {len(self.save_slots)} save files from {self.save_dir}")
        else:
            self.status_var.set(
                "No save files found in this directory. "
                "Use 'Browse Folder...' to select the correct location.")

    # ── Save Selection ───────────────────────────────────────────────────

    def _on_select(self, event):
        """Handle save slot selection in the treeview."""
        sel = self.tree.selection()
        if not sel:
            return

        slot_num = int(sel[0])
        slot = next((s for s in self.save_slots if s.slot_number == slot_num), None)
        if not slot:
            return

        # Check for unsaved changes
        if self.current_slot and self.current_slot.has_changes():
            if messagebox.askyesno("Unsaved Changes",
                                   f"Save {self.current_slot.slot_number:03d} has "
                                   f"unsaved changes. Discard?"):
                self.current_slot.data = bytearray(self.current_slot.original_data)
                self.current_slot.load()
            else:
                # Re-select current
                self.tree.selection_set(str(self.current_slot.slot_number))
                return

        self.current_slot = slot
        self._populate_editor()

    def _populate_editor(self):
        """Fill the editor fields with data from the current save slot."""
        if not self.current_slot:
            return

        slot = self.current_slot

        # Dialogue
        self.dialogue_var.set(slot.dialogue if slot.dialogue else "(empty)")

        # Script position
        self.script_pos_var.set(f"0x{slot.script_pos:08X} ({slot.script_pos:,})")

        # Stats
        for name, var in self.stat_vars.items():
            var.set(str(slot.get_stat(name)))

        # Thumbnail
        self._load_thumbnail(slot.data)

        self.status_var.set(f"Editing Save {slot.slot_number:03d}")

    def _load_thumbnail(self, data):
        """Decode and display the save file thumbnail."""
        try:
            ppm_data = decode_thumbnail_to_photoimage(data)
            if ppm_data:
                self._thumb_image_base = tk.PhotoImage(data=ppm_data)
                self._thumb_zoom = 0  # Force recalculation
                self._rescale_thumbnail()
            else:
                self._thumb_image_base = None
                self.thumb_label.configure(image='', text="No thumbnail",
                                           fg='#6c7086')
        except Exception as e:
            self._thumb_image_base = None
            self.thumb_label.configure(image='', text=f"Error: {e}",
                                       fg='#f38ba8')

    def _on_canvas_resize(self, event):
        """Rescale thumbnail when the editor panel is resized."""
        # Also update scroll region
        self._editor_canvas.configure(
            scrollregion=self._editor_canvas.bbox('all'))
        self._rescale_thumbnail()

    def _rescale_thumbnail(self):
        """Pick the best integer zoom for the current canvas width."""
        if not getattr(self, '_thumb_image_base', None):
            return
        available = self._editor_canvas.winfo_width() - 40  # padding
        if available < THUMB_WIDTH:
            return
        zoom = max(1, available // THUMB_WIDTH)
        if zoom == self._thumb_zoom:
            return
        self._thumb_zoom = zoom
        self._thumb_image_scaled = self._thumb_image_base.zoom(zoom, zoom)
        self.thumb_label.configure(image=self._thumb_image_scaled)

    # ── Editing Actions ──────────────────────────────────────────────────

    def _apply_changes(self):
        """Validate and write changes to the save file."""
        if not self.current_slot:
            messagebox.showwarning("No Save Selected", "Please select a save file first.")
            return

        slot = self.current_slot

        # Validate and apply each stat
        for offset, name, desc, min_v, max_v in STAT_FIELDS:
            try:
                val = int(self.stat_vars[name].get())
            except ValueError:
                messagebox.showerror("Invalid Value",
                                     f"{name}: must be an integer.")
                self.stat_entries[name].focus_set()
                return

            if val < min_v or val > max_v:
                messagebox.showerror("Out of Range",
                                     f"{name}: must be between {min_v} and {max_v}.")
                self.stat_entries[name].focus_set()
                return

            slot.set_stat(name, val)

        # Save with backup
        try:
            slot.save(backup=True)
            self.status_var.set(
                f"Saved changes to Save {slot.slot_number:03d} "
                f"(backup: {slot.filename}{BACKUP_SUFFIX})"
            )
            # Update treeview row
            self.tree.item(str(slot.slot_number), values=(
                f"{slot.slot_number:03d}",
                slot.get_modified_str(),
                slot.get_stat('HP'),
                slot.get_stat('Power'),
                slot.get_stat('Expansion Points'),
            ))
            messagebox.showinfo("Success",
                                f"Save {slot.slot_number:03d} updated successfully!\n"
                                f"Backup saved as {slot.filename}{BACKUP_SUFFIX}")
        except Exception as e:
            messagebox.showerror("Save Failed", f"Could not write file:\n{e}")

    def _revert(self):
        """Revert the current save to its original data."""
        if not self.current_slot:
            return

        self.current_slot.data = bytearray(self.current_slot.original_data)
        self.current_slot.load()
        self._populate_editor()
        self.status_var.set(f"Reverted Save {self.current_slot.slot_number:03d}")

    def _max_all_stats(self):
        """Set all stat fields to high but reasonable values."""
        if not self.current_slot:
            messagebox.showwarning("No Save Selected", "Please select a save file first.")
            return

        # Set reasonable max values
        presets = {
            'HP': 9999,
            'Max HP': 9999,
            'Power': 9999,
            'Expansion Points': 999,
            'Strength Gain': 999,
            'Potion Gain': 999,
            'Skill Gain': 999,
            'Stat D': 999,
            'Stat E': 999,
        }
        for name, val in presets.items():
            if name in self.stat_vars:
                self.stat_vars[name].set(str(val))

        self.status_var.set("Stats set to max values - click 'Apply Changes' to save")

    # ── CG Unlock ────────────────────────────────────────────────────────

    def _unlock_all_cgs(self):
        """Set all CG gallery bits to 1 in revenger.sys."""
        if self.sys_data is None:
            messagebox.showerror("Error", "revenger.sys not found in save directory.")
            return

        if not messagebox.askyesno("Unlock All CGs",
                                    "This will unlock all CG/event gallery entries.\n"
                                    "A backup will be created. Continue?"):
            return

        sys_path = os.path.join(self.save_dir, SYS_FILE_NAME)
        backup_path = sys_path + BACKUP_SUFFIX

        # Backup
        if not os.path.exists(backup_path):
            shutil.copy2(sys_path, backup_path)

        # Set all bits in the CG bitmask range
        for i in range(CG_BITMASK_START, CG_BITMASK_END + 1):
            self.sys_data[i] = 0xFF

        with open(sys_path, 'wb') as f:
            f.write(self.sys_data)

        unlocked, total = count_unlocked_cgs(self.sys_data)
        self.cg_label_var.set(f"CG Gallery: {unlocked}/{total} unlocked")
        self.status_var.set("All CGs unlocked! Backup saved as revenger.sys.bak")
        messagebox.showinfo("Done", "All CG/event gallery entries unlocked!")

    # ── Backup Management ─────────────────────────────────────────────────

    def _get_backup_files(self):
        """Return a list of all .bak files in the save directory."""
        if not os.path.isdir(self.save_dir):
            return []
        return [
            os.path.join(self.save_dir, f)
            for f in os.listdir(self.save_dir)
            if f.endswith(BACKUP_SUFFIX)
        ]

    def _update_backup_count(self):
        """Update the backup count label."""
        backups = self._get_backup_files()
        self.backup_label_var.set(f"Backups: {len(backups)} file(s)")

    def _delete_all_backups(self):
        """Delete all .bak files in the save directory."""
        backups = self._get_backup_files()
        if not backups:
            messagebox.showinfo("No Backups", "There are no backup files to delete.")
            return

        if not messagebox.askyesno(
                "Delete All Backups",
                f"This will permanently delete {len(backups)} backup file(s):\n\n"
                + "\n".join(os.path.basename(f) for f in backups)
                + "\n\nThis cannot be undone. Continue?"):
            return

        deleted = 0
        for path in backups:
            try:
                os.remove(path)
                deleted += 1
            except Exception as e:
                print(f"Warning: Failed to delete {path}: {e}")

        self._update_backup_count()
        self.status_var.set(f"Deleted {deleted} backup file(s)")
        messagebox.showinfo("Done", f"Deleted {deleted} backup file(s).")

    # ── Compare Tool ─────────────────────────────────────────────────────

    def _open_compare_dialog(self):
        """Open a dialog to compare two save files byte-by-byte."""
        if len(self.save_slots) < 2:
            messagebox.showwarning("Not Enough Saves",
                                   "Need at least 2 save files to compare.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Compare Saves")
        dialog.geometry("750x550")
        dialog.configure(bg='#1e1e2e')
        dialog.transient(self.root)

        top = ttk.Frame(dialog)
        top.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(top, text="Save A:").pack(side=tk.LEFT, padx=5)
        slot_names = [f"Save {s.slot_number:03d}" for s in self.save_slots]
        var_a = tk.StringVar(value=slot_names[0])
        combo_a = ttk.Combobox(top, textvariable=var_a, values=slot_names,
                               state='readonly', width=12)
        combo_a.pack(side=tk.LEFT, padx=5)

        ttk.Label(top, text="Save B:").pack(side=tk.LEFT, padx=(20, 5))
        var_b = tk.StringVar(value=slot_names[1] if len(slot_names) > 1 else slot_names[0])
        combo_b = ttk.Combobox(top, textvariable=var_b, values=slot_names,
                               state='readonly', width=12)
        combo_b.pack(side=tk.LEFT, padx=5)

        # Filter options
        filter_frame = ttk.Frame(dialog)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)

        show_stats_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_frame, text="Show only stat region (0x09D540-0x09D640)",
                        variable=show_stats_only).pack(side=tk.LEFT, padx=5)

        # Results area
        result_text = tk.Text(dialog, bg='#313244', fg='#cdd6f4',
                              font=('Consolas', 9), wrap=tk.NONE,
                              insertbackground='#cdd6f4')
        result_scroll_y = ttk.Scrollbar(dialog, orient=tk.VERTICAL,
                                        command=result_text.yview)
        result_scroll_x = ttk.Scrollbar(dialog, orient=tk.HORIZONTAL,
                                        command=result_text.xview)
        result_text.configure(yscrollcommand=result_scroll_y.set,
                              xscrollcommand=result_scroll_x.set)
        result_scroll_y.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=(0, 10))
        result_scroll_x.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        result_text.pack(fill=tk.BOTH, expand=True, padx=(10, 0), pady=(0, 10))

        def run_compare():
            idx_a = slot_names.index(var_a.get())
            idx_b = slot_names.index(var_b.get())
            sa = self.save_slots[idx_a]
            sb = self.save_slots[idx_b]

            result_text.delete('1.0', tk.END)
            result_text.insert(tk.END,
                f"Comparing Save {sa.slot_number:03d} vs Save {sb.slot_number:03d}\n")
            result_text.insert(tk.END, "=" * 70 + "\n\n")

            start = 0x09D540 if show_stats_only.get() else 0
            end = 0x09D640 if show_stats_only.get() else SAVE_FILE_SIZE

            diffs = 0
            result_text.insert(tk.END,
                f"{'Offset':<12} {'Save A (hex)':<16} {'Save B (hex)':<16} "
                f"{'A (uint16)':<12} {'B (uint16)':<12} {'Field'}\n")
            result_text.insert(tk.END, "-" * 80 + "\n")

            for off in range(start, end - 1, 2):
                va = struct.unpack_from('<H', sa.data, off)[0]
                vb = struct.unpack_from('<H', sb.data, off)[0]
                if va != vb:
                    # Check if this is a known stat field
                    field_name = ""
                    for foff, fname, fdesc, _, _ in STAT_FIELDS:
                        if off == foff:
                            field_name = fname
                            break

                    result_text.insert(tk.END,
                        f"0x{off:06X}    "
                        f"{sa.data[off:off+2].hex():<16} "
                        f"{sb.data[off:off+2].hex():<16} "
                        f"{va:<12} {vb:<12} {field_name}\n")
                    diffs += 1

                    if diffs >= 2000 and not show_stats_only.get():
                        result_text.insert(tk.END,
                            f"\n... truncated at 2000 differences. "
                            f"Use stat region filter for focused view.\n")
                        break

            result_text.insert(tk.END, f"\nTotal differences: {diffs}\n")

        ttk.Button(top, text="Compare", command=run_compare).pack(
            side=tk.LEFT, padx=20)


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    # Enable DPI awareness for sharp rendering on high-DPI displays
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()

    # Set window icon (optional, won't crash if missing)
    try:
        root.iconbitmap(default='')
    except Exception:
        pass

    app = SaveEditorApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
