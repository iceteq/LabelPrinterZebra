import argparse
import io
import json
import queue
import re
import socket
import sys
import threading
import tkinter as tk
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFilter, ImageTk

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _CONFIG_DIR / "printer_config.json"
_EXAMPLE_NAME = "printer_config.example.json"
_BARCODE_ESCAPES_PATH = _CONFIG_DIR / "barcode_escapes.json"
_BARCODE_ESCAPES_EXAMPLE = _CONFIG_DIR / "barcode_escapes.example.json"
_SETTINGS_PATH = _CONFIG_DIR / "label_settings.json"
_ERROR_LOG_PATH = _CONFIG_DIR / "label_printer_error.log"
_DEFAULT_PRINTER_PORT = 9100


def _log_exception(where: str, exc: BaseException | None = None) -> None:
    """Persist UI/thread failures so a vanishing window is diagnosable."""
    try:
        with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {date.today().isoformat()} {where} ---\n")
            if exc is not None:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            else:
                fh.write("".join(traceback.format_exception(*sys.exc_info())))
    except OSError:
        pass


@dataclass(frozen=True)
class PrinterEntry:
    name: str
    host: str
    port: int = _DEFAULT_PRINTER_PORT


@dataclass
class PrinterStore:
    printers: list[PrinterEntry]
    selected: str = ""

    def names(self) -> list[str]:
        return [p.name for p in self.printers]

    def get(self, name: str) -> PrinterEntry | None:
        for printer in self.printers:
            if printer.name == name:
                return printer
        return None

    def selected_printer(self) -> PrinterEntry | None:
        chosen = self.get(self.selected)
        if chosen is not None:
            return chosen
        return self.printers[0] if self.printers else None


def _empty_printer_store() -> PrinterStore:
    return PrinterStore(printers=[], selected="")


def _normalize_printer_store(data: object) -> PrinterStore:
    """Accept new multi-printer config or legacy {host, port}."""
    if not isinstance(data, dict):
        return _empty_printer_store()

    if "printers" not in data and "host" in data:
        name = str(data.get("name") or "Printer").strip() or "Printer"
        port = int(data.get("port", _DEFAULT_PRINTER_PORT))
        entry = PrinterEntry(name=name, host=str(data["host"]).strip(), port=port)
        return PrinterStore(printers=[entry], selected=entry.name)

    printers: list[PrinterEntry] = []
    seen: set[str] = set()
    raw_list = data.get("printers", [])
    if not isinstance(raw_list, list):
        raw_list = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        if not name or not host or name in seen:
            continue
        try:
            port = int(item.get("port", _DEFAULT_PRINTER_PORT))
        except (TypeError, ValueError):
            port = _DEFAULT_PRINTER_PORT
        if not (1 <= port <= 65535):
            port = _DEFAULT_PRINTER_PORT
        seen.add(name)
        printers.append(PrinterEntry(name=name, host=host, port=port))

    selected = str(data.get("selected", "")).strip()
    names = {p.name for p in printers}
    if selected not in names:
        selected = printers[0].name if printers else ""
    return PrinterStore(printers=printers, selected=selected)


def _load_printer_store() -> PrinterStore:
    if not _CONFIG_PATH.is_file():
        return _empty_printer_store()
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return _normalize_printer_store(json.load(f))


def _save_printer_store(store: PrinterStore) -> None:
    payload = {
        "selected": store.selected,
        "printers": [
            {"name": p.name, "host": p.host, "port": int(p.port)} for p in store.printers
        ],
    }
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def _load_printer_config() -> tuple[str, int]:
    store = _load_printer_store()
    printer = store.selected_printer()
    if printer is None:
        raise FileNotFoundError(
            "No printer configured. Click Manage… to add a printer (name, IP, port)."
        )
    return printer.host, printer.port


def _validate_printer_fields(
    name: str, host: str, port_text: str, *, existing: set[str], renaming_from: str = ""
) -> tuple[str, str, int]:
    name = name.strip()
    host = host.strip()
    if not name:
        raise ValueError("Enter a printer name.")
    if not host:
        raise ValueError("Enter a printer IP or hostname.")
    try:
        port = int(str(port_text).strip())
    except ValueError as exc:
        raise ValueError("Port must be a number (usually 9100).") from exc
    if not (1 <= port <= 65535):
        raise ValueError("Port must be between 1 and 65535.")
    conflict = existing - ({renaming_from} if renaming_from else set())
    if name in conflict:
        raise ValueError(f'A printer named "{name}" already exists.')
    return name, host, port


def _zpl_field(text: str) -> str:
    return text.replace("^", "^^").replace("~", "~~")


def _load_barcode_escapes() -> dict[str, str]:
    if _BARCODE_ESCAPES_PATH.is_file():
        path = _BARCODE_ESCAPES_PATH
    elif _BARCODE_ESCAPES_EXAMPLE.is_file():
        path = _BARCODE_ESCAPES_EXAMPLE
    else:
        return {"enter": "\r"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    keywords = data.get("keywords", data)
    if not isinstance(keywords, dict):
        raise ValueError(f"{path.name} must contain a 'keywords' object.")
    return {str(name).lower(): str(char) for name, char in keywords.items()}


def _decode_barcode_input(text: str, escapes: dict[str, str] | None = None) -> str:
    escapes = escapes if escapes is not None else _load_barcode_escapes()
    result = text
    for name, char in escapes.items():
        pattern = r"\{" + re.escape(name) + r"\}"
        result = re.sub(pattern, char, result, flags=re.IGNORECASE)
    return result


def _zpl_fh_payload(data: str) -> str:
    parts: list[str] = []
    for ch in data:
        if ch == "_":
            parts.append("__")
        elif ch == "^":
            parts.append("^^")
        elif ch == "~":
            parts.append("~~")
        elif ord(ch) < 32 or ord(ch) > 126:
            parts.append(f"_{ord(ch):02X}")
        else:
            parts.append(ch)
    return "".join(parts)


_LABELARY_DPMM = 8
_LABELARY_WIDTH_IN = 4
_LABELARY_HEIGHT_IN = 2
_TEXT_ONLY_MAX_LINES = 8
_AUTHOR = "Anton"
_STAMP_FONT_H = 16
_STAMP_FONT_W = 12
# ^FB line gap (dots). Must match block_h math used for vertical centering.
_FB_LINE_GAP = 0
# Zebra ^A0 cells have more empty ascent than descent; nudge middle-aligned
# text down so the ink looks centered (scales with font size).
_OPTICAL_CENTER_BIAS = 0.18
# Slightly optimistic wrap width so we don't invent a blank second line when
# our char-width model is a touch wider than the printer's real glyphs.
_WRAP_WIDTH_SLACK = 1.08
# Relative advance widths as fractions of font height (calibrated so
# ABCDEFGHIJKLMNOPQRSTUVWXY ≈ fills the printable width).
_CHAR_WIDTH_SPACE = 0.28
_CHAR_WIDTH_NARROW = 0.35
_CHAR_WIDTH_LOWER = 0.48
_CHAR_WIDTH_UPPER = 0.57
_CHAR_WIDTH_WIDE = 0.85
_NARROW_CHARS = set("iltrfjI.:;,!|'\"`")
_WIDE_CHARS = set("mw@%")

_H_ALIGN = {"left": "L", "center": "C", "right": "R"}
_SIZE_STEPS = ("small", "medium", "large", "xl", "xxl")
_SIZE_LABELS = {
    "small": "Small",
    "medium": "Medium",
    "large": "Large",
    "xl": "XL",
    "xxl": "XXL",
}
# Compact labels for steppers beside input fields.
_SIZE_COMPACT = {
    "small": "S",
    "medium": "M",
    "large": "L",
    "xl": "XL",
    "xxl": "XXL",
}
# Type sizes — fit-to-box shrinks when content needs it.
_NOTE_SIZE = {
    "small": (52, 46),
    "medium": (72, 64),
    "large": (96, 84),
    "xl": (124, 108),
    "xxl": (156, 136),
}
_TITLE_SIZE = {
    "small": (36, 32),
    "medium": (48, 42),
    "large": (64, 56),
    "xl": (80, 70),
    "xxl": (104, 90),
}
_CAPTION_SIZE = {
    "small": (22, 18),
    "medium": (28, 22),
    "large": (40, 32),
    "xl": (56, 46),
    "xxl": (72, 58),
}
_DEFAULT_TYPE_SIZE = "medium"
# Note placement presets: (horizontal, vertical).
_NOTE_ALIGN_STEPS = ("left", "right", "top", "bottom", "center")
_NOTE_ALIGN_LABELS = {
    "left": "Left",
    "right": "Right",
    "top": "Top",
    "bottom": "Bottom",
    "center": "Center",
}
_NOTE_ALIGN = {
    "left": ("left", "middle"),
    "right": ("right", "middle"),
    "top": ("center", "top"),
    "bottom": ("center", "bottom"),
    "center": ("center", "middle"),
}
_DEFAULT_NOTE_ALIGN = "center"


def _label_dots(width_in: float, height_in: float, dpmm: int) -> tuple[int, int]:
    dots_per_in = dpmm * 25.4
    return round(width_in * dots_per_in), round(height_in * dots_per_in)


_LABEL_WIDTH, _LABEL_HEIGHT = _label_dots(
    _LABELARY_WIDTH_IN, _LABELARY_HEIGHT_IN, _LABELARY_DPMM
)


@dataclass(frozen=True)
class LookRecipe:
    """Coupled visual recipe — user picks intent; engine picks pixels."""

    look_id: str
    title: str
    blurb: str
    align: str
    valign: str  # top | middle
    stack: str  # vertical | side
    title_h: int
    title_w: int
    bar_h: int
    module_pref: int
    caption_h: int
    caption_w: int
    # Target fraction of printable width for short barcodes (visual balance).
    bar_fill: float


# Tuned for 4×2: clear hierarchy, tight barcode↔caption grouping, calm margins.
# Type sizes are intentionally bold; fit-to-box shrinks when needed.
_LOOKS: dict[str, LookRecipe] = {
    "quiet": LookRecipe(
        look_id="quiet",
        title="Quiet",
        blurb="Default — calm type, reliable bars",
        align="left",
        valign="top",
        stack="vertical",
        title_h=48,
        title_w=42,
        bar_h=140,
        module_pref=3,
        caption_h=28,
        caption_w=22,
        bar_fill=0.68,
    ),
    "read_id": LookRecipe(
        look_id="read_id",
        title="Read ID",
        blurb="Large caption for act / part numbers",
        align="left",
        valign="top",
        stack="vertical",
        title_h=36,
        title_w=32,
        bar_h=120,
        module_pref=3,
        caption_h=56,
        caption_w=46,
        bar_fill=0.55,
    ),
    "label": LookRecipe(
        look_id="label",
        title="Label",
        blurb="Centered mark — title leads",
        align="center",
        valign="middle",
        stack="vertical",
        title_h=80,
        title_w=70,
        bar_h=145,
        module_pref=3,
        caption_h=26,
        caption_w=20,
        bar_fill=0.58,
    ),
    "side": LookRecipe(
        look_id="side",
        title="Side title",
        blurb="Title beside the barcode",
        align="left",
        valign="middle",
        stack="side",
        title_h=52,
        title_w=46,
        bar_h=145,
        module_pref=3,
        caption_h=34,
        caption_w=28,
        bar_fill=0.55,
    ),
}

_LOOK_ORDER = ("quiet", "read_id", "label", "side")
_DEFAULT_LOOK = "quiet"
_NOTE_LOOK = "note"
_NOTE_LOOK_TITLE = "Note"


def _normalize_look(value: str) -> str:
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "readid": "read_id",
        "read": "read_id",
        "side_title": "side",
        "sidetitle": "side",
        "default": "quiet",
        "scan": "quiet",  # removed look; map old configs/CLI to Quiet
        "note": _NOTE_LOOK,
    }
    key = aliases.get(key, key)
    if key == _NOTE_LOOK:
        return _NOTE_LOOK
    if key not in _LOOKS:
        raise ValueError(f"look must be one of {list(_LOOK_ORDER) + [_NOTE_LOOK]}")
    return key


def _normalize_size(value: str) -> str:
    key = str(value).strip().lower()
    if key not in _SIZE_STEPS:
        raise ValueError(f"size must be one of {list(_SIZE_STEPS)}")
    return key


def _normalize_note_align(value: str) -> str:
    key = str(value).strip().lower()
    aliases = {
        "middle": "center",
        "c": "center",
        "l": "left",
        "r": "right",
        "t": "top",
        "b": "bottom",
    }
    key = aliases.get(key, key)
    if key not in _NOTE_ALIGN:
        raise ValueError(f"note align must be one of {list(_NOTE_ALIGN_STEPS)}")
    return key


def _bump_size(current: str, delta: int) -> str:
    idx = _SIZE_STEPS.index(_normalize_size(current))
    return _SIZE_STEPS[max(0, min(len(_SIZE_STEPS) - 1, idx + delta))]


@dataclass(frozen=True)
class LabelLayout:
    look: str = _DEFAULT_LOOK
    note_size: str = _DEFAULT_TYPE_SIZE
    note_align: str = _DEFAULT_NOTE_ALIGN
    title_size: str = _DEFAULT_TYPE_SIZE
    caption_size: str = _DEFAULT_TYPE_SIZE
    show_caption: bool = True  # human-readable text under barcode (Side title)
    show_footer: bool = True  # author · date stamp

    def __post_init__(self) -> None:
        object.__setattr__(self, "look", _normalize_look(self.look))
        object.__setattr__(self, "note_size", _normalize_size(self.note_size))
        object.__setattr__(self, "note_align", _normalize_note_align(self.note_align))
        object.__setattr__(self, "title_size", _normalize_size(self.title_size))
        object.__setattr__(self, "caption_size", _normalize_size(self.caption_size))
        object.__setattr__(self, "show_caption", bool(self.show_caption))
        object.__setattr__(self, "show_footer", bool(self.show_footer))

    @property
    def recipe(self) -> LookRecipe:
        # Note is UI/mode only; barcode recipes fall back to Quiet if needed.
        if self.look == _NOTE_LOOK:
            return _LOOKS[_DEFAULT_LOOK]
        return _LOOKS[self.look]

    @property
    def title_dims(self) -> tuple[int, int]:
        return _TITLE_SIZE[self.title_size]

    @property
    def caption_dims(self) -> tuple[int, int]:
        return _CAPTION_SIZE[self.caption_size]


def _load_label_settings() -> LabelLayout:
    """Last-used layout (never includes copies — those always start at 1)."""
    if not _SETTINGS_PATH.is_file():
        return LabelLayout()
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LabelLayout()
    if not isinstance(data, dict):
        return LabelLayout()
    try:
        look = data.get("look", _DEFAULT_LOOK)
        # Never restore Note as the barcode look preference.
        if _normalize_look(str(look)) == _NOTE_LOOK:
            look = _DEFAULT_LOOK
        return LabelLayout(
            look=look,
            note_size=data.get("note_size", _DEFAULT_TYPE_SIZE),
            note_align=data.get("note_align", _DEFAULT_NOTE_ALIGN),
            title_size=data.get("title_size", _DEFAULT_TYPE_SIZE),
            caption_size=data.get("caption_size", _DEFAULT_TYPE_SIZE),
            show_caption=data.get("show_caption", True),
            show_footer=data.get("show_footer", True),
        )
    except (TypeError, ValueError):
        return LabelLayout()


def _save_label_settings(layout: LabelLayout) -> None:
    look = layout.look if layout.look != _NOTE_LOOK else _DEFAULT_LOOK
    payload = {
        "look": look,
        "note_size": layout.note_size,
        "note_align": layout.note_align,
        "title_size": layout.title_size,
        "caption_size": layout.caption_size,
        "show_caption": bool(layout.show_caption),
        "show_footer": bool(layout.show_footer),
    }
    try:
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def _char_width_dots(ch: str, font_h: int) -> float:
    if ch == " " or ch == "\t":
        return font_h * _CHAR_WIDTH_SPACE
    if ch in _NARROW_CHARS:
        return font_h * _CHAR_WIDTH_NARROW
    if ch in _WIDE_CHARS:
        return font_h * _CHAR_WIDTH_WIDE
    if ch.isupper() or ch.isdigit():
        return font_h * _CHAR_WIDTH_UPPER
    return font_h * _CHAR_WIDTH_LOWER


def _text_width_dots(text: str, font_h: int) -> float:
    return sum(_char_width_dots(ch, font_h) for ch in text)


_MIN_TITLE_H = 22
_MIN_CAPTION_H = 16
_MIN_NOTE_H = 28
_MIN_BAR_H = 48


def _truncate_to_width(text: str, font_h: int, block_w: float) -> str:
    if _text_width_dots(text, font_h) <= block_w:
        return text
    ellipsis = "..."
    ellipsis_w = _text_width_dots(ellipsis, font_h)
    budget = block_w - ellipsis_w
    if budget <= 0:
        return ellipsis
    kept: list[str] = []
    used = 0.0
    for ch in text:
        w = _char_width_dots(ch, font_h)
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    return "".join(kept) + ellipsis


@dataclass(frozen=True)
class FittedText:
    text: str
    font_h: int
    font_w: int
    shrunk: bool
    truncated: bool


@dataclass(frozen=True)
class BuildResult:
    zpl: str
    warnings: tuple[str, ...] = ()


def _scale_font_w(prefer_h: int, prefer_w: int, new_h: int) -> int:
    if prefer_h <= 0:
        return max(8, new_h)
    return max(8, int(round(prefer_w * (new_h / prefer_h))))


def _fit_text_to_width(
    text: str,
    prefer_h: int,
    prefer_w: int,
    block_w: float,
    min_h: int,
) -> FittedText:
    """Shrink font to fit one line; truncate with ellipsis only at the minimum size."""
    text = text or ""
    if not text:
        return FittedText("", prefer_h, prefer_w, False, False)
    h = prefer_h
    w = prefer_w
    while h > min_h and _text_width_dots(text, h) > block_w:
        h -= 2
        w = _scale_font_w(prefer_h, prefer_w, h)
    shrunk = h < prefer_h
    if _text_width_dots(text, h) <= block_w:
        return FittedText(text, h, w, shrunk, False)
    clipped = _truncate_to_width(text, h, block_w)
    return FittedText(clipped, h, w, shrunk, clipped != text)


def _estimate_wrapped_lines(
    text: str, font_h: int, block_w: float, *, slack: float = 1.0
) -> int:
    """Estimate wrapped line count. slack>1 makes wrap slightly optimistic."""
    words = text.split()
    if not words:
        return 1
    width = max(1.0, float(block_w) * max(1.0, slack))
    lines = 1
    used = 0.0
    space = _char_width_dots(" ", font_h)
    for word in words:
        ww = _text_width_dots(word, font_h)
        if ww > width:
            # Very long token: rough char-wrap estimate.
            lines += max(1, int(ww // width))
            used = ww % width
            continue
        if used == 0:
            used = ww
        elif used + space + ww <= width:
            used += space + ww
        else:
            lines += 1
            used = ww
    return max(1, lines)


def _text_block_height(font_h: int, lines: int, *, line_gap: int = _FB_LINE_GAP) -> int:
    lines = max(1, int(lines))
    return lines * font_h + max(0, lines - 1) * line_gap


def _optical_center_nudge(font_h: int, v_align: str = "middle") -> int:
    """Shift middle-aligned ^A0 text down so glyphs look centered in the cell."""
    if v_align != "middle":
        return 0
    return max(0, int(round(font_h * _OPTICAL_CENTER_BIAS)))


def _fit_note_text(
    text: str,
    prefer_h: int,
    prefer_w: int,
    block_w: float,
    max_lines: int,
    min_h: int = _MIN_NOTE_H,
) -> FittedText:
    """Shrink note body until it wraps within max_lines; last resort keeps min size."""
    text = " ".join(text.split())
    if not text:
        return FittedText("", prefer_h, prefer_w, False, False)
    h = prefer_h
    w = prefer_w
    while h > min_h and _estimate_wrapped_lines(text, h, block_w) > max_lines:
        h -= 2
        w = _scale_font_w(prefer_h, prefer_w, h)
    shrunk = h < prefer_h
    # At minimum size, still allow wrapping; FB will clip overflow lines.
    return FittedText(text, h, w, shrunk, False)


def _zpl_header() -> str:
    return (
        "^XA"
        "^CI28"
        f"^PW{_LABEL_WIDTH}"
        f"^LL{_LABEL_HEIGHT}"
    )


def _caption_display(serial: str) -> str:
    return " ".join(serial.split())


def _estimate_barcode_width(payload: str, module: int) -> int:
    return (len(payload) + 3) * 11 * module


def _fit_module(payload: str, prefer: int, max_width: int, fill: float) -> int:
    """Pick module width: keep preferred size when possible, widen short codes for balance."""
    prefer = max(1, prefer)
    max_fit = 1
    for m in range(6, 0, -1):
        if _estimate_barcode_width(payload, m) <= max_width:
            max_fit = m
            break
    target = int(max_width * fill)
    module = min(prefer, max_fit)
    while module < max_fit and _estimate_barcode_width(payload, module) < target:
        module += 1
    return module


def _barcode_warnings(module: int, prefer: int) -> list[str]:
    if module <= 1:
        return ["Barcode is very dense — may be hard to scan"]
    if module < prefer:
        return ["Barcode narrowed to fit the label"]
    return []


def _text_zpl(
    x: int,
    y: int,
    text: str,
    font_h: int,
    font_w: int,
    h_align: str,
    block_w: int,
    *,
    max_lines: int = 1,
    line_gap: int = _FB_LINE_GAP,
) -> str:
    zpl_align = _H_ALIGN[h_align]
    gap = max(0, int(line_gap))
    return (
        f"^FO{x},{y}^FB{block_w},{max_lines},{gap},{zpl_align},0"
        f"^A0N,{font_h},{font_w}^FD{_zpl_field(text)}^FS"
    )


def _barcode_zpl(x: int, y: int, payload: str, height: int, module: int) -> str:
    encoded = _zpl_fh_payload(payload)
    return (
        f"^FO{x},{y}^BY{module},3,{height}"
        f"^FH^BCN,{height},N,N,N^FD{encoded}^FS"
    )


def _stamp_reserve(*, show_footer: bool = True) -> int:
    return (_STAMP_FONT_H + 14) if show_footer else 0


def _signature_zpl(margin: int) -> str:
    stamp_y = _LABEL_HEIGHT - margin - _STAMP_FONT_H
    text = f"{_AUTHOR} · {date.today():%Y-%m-%d}"
    block_w = _LABEL_WIDTH - 2 * margin
    return (
        f"^FO{margin},{stamp_y}"
        f"^FB{block_w},1,0,R,0"
        f"^A0N,{_STAMP_FONT_H},{_STAMP_FONT_W}"
        f"^FD{_zpl_field(text)}^FS"
    )


def _adaptive_margin(dense: bool) -> int:
    # Slightly tighter when the stack is tall — keeps the composition calm.
    return 40 if dense else 48


def _content_bottom(margin: int, *, show_footer: bool = True) -> int:
    return _LABEL_HEIGHT - margin - _stamp_reserve(show_footer=show_footer)


def _build_note_zpl(title: str, layout: LabelLayout, *, copies: int) -> BuildResult:
    prefer_h, prefer_w = _NOTE_SIZE[layout.note_size]
    h_align, v_align = _NOTE_ALIGN[layout.note_align]
    margin = _adaptive_margin(prefer_h >= 72)
    bottom = _content_bottom(margin, show_footer=layout.show_footer)
    block_w = _LABEL_WIDTH - 2 * margin
    available = max(prefer_h, bottom - margin)
    # Provisional line budget at preferred size, then refit after shrink.
    line_pitch = prefer_h + _FB_LINE_GAP
    max_lines = max(1, min(_TEXT_ONLY_MAX_LINES, available // max(1, line_pitch)))
    fitted = _fit_note_text(title, prefer_h, prefer_w, float(block_w), max_lines)
    line_pitch = fitted.font_h + _FB_LINE_GAP
    max_lines = max(1, min(_TEXT_ONLY_MAX_LINES, available // max(1, line_pitch)))
    # If shrink unlocked more lines, try once more at the fitted size.
    fitted = _fit_note_text(
        title, prefer_h, prefer_w, float(block_w), max_lines
    )
    # Optimistic wrap avoids reserving a blank line the printer never draws.
    est_lines = min(
        max_lines,
        _estimate_wrapped_lines(
            fitted.text,
            fitted.font_h,
            float(block_w),
            slack=_WRAP_WIDTH_SLACK,
        ),
    )
    # ^FB max_lines must match the height we centered — not a larger budget.
    paint_lines = est_lines
    block_h = _text_block_height(fitted.font_h, paint_lines)
    content_h = max(0, bottom - margin)
    if v_align == "top":
        y = margin
    elif v_align == "bottom":
        y = max(margin, bottom - block_h)
    else:
        y = margin + max(0, (content_h - block_h) // 2)
        y += _optical_center_nudge(fitted.font_h, v_align)
        y = min(y, max(margin, bottom - block_h))
    warnings: list[str] = []
    if fitted.shrunk:
        warnings.append("Note text shrunk to fit")
    zpl = (
        _zpl_header()
        + _text_zpl(
            margin,
            y,
            fitted.text,
            fitted.font_h,
            fitted.font_w,
            h_align,
            block_w,
            max_lines=paint_lines,
            line_gap=_FB_LINE_GAP,
        )
        + (_signature_zpl(margin) if layout.show_footer else "")
        + f"^PQ{copies}"
        + "^XZ"
    )
    return BuildResult(zpl, tuple(warnings))


def _build_side_zpl(
    title: str,
    serial: str,
    recipe: LookRecipe,
    layout: LabelLayout,
    *,
    copies: int,
) -> BuildResult:
    payload = _decode_barcode_input(serial)
    margin = 40
    gap = 10
    warnings: list[str] = []
    raw_title = " ".join(title.split()) if title.strip() else ""
    title_pref_h, title_pref_w = layout.title_dims
    title_h, title_w = title_pref_h, title_pref_w
    show_caption = layout.show_caption
    show_footer = layout.show_footer
    title_col = 100
    fitted_title = FittedText("", title_h, title_w, False, False)
    if raw_title:
        # Iterate: column hugs text, font shrinks to column, column remeasures.
        for _ in range(4):
            title_col = max(
                72,
                min(160, int(_text_width_dots(raw_title, title_h) + 12)),
            )
            fitted_title = _fit_text_to_width(
                raw_title, title_pref_h, title_pref_w, float(title_col), _MIN_TITLE_H
            )
            title_h, title_w = fitted_title.font_h, fitted_title.font_w
            new_col = max(
                72,
                min(160, int(_text_width_dots(fitted_title.text, title_h) + 12)),
            )
            if abs(new_col - title_col) <= 2:
                title_col = new_col
                break
            title_col = new_col
        if fitted_title.shrunk:
            warnings.append("Title shrunk to fit")
        if fitted_title.truncated:
            warnings.append("Title shortened to fit")

    h_align, v_align = _NOTE_ALIGN[layout.note_align]
    # Title beside barcode: Left keeps title|bar, Right mirrors to bar|title.
    title_on_left = h_align != "right"
    title_span = (title_col + gap) if raw_title else 0
    bar_max_w = _LABEL_WIDTH - 2 * margin - title_span
    quiet = 12
    module = _fit_module(
        payload, recipe.module_pref, max(40, bar_max_w - quiet), recipe.bar_fill
    )
    warnings.extend(_barcode_warnings(module, recipe.module_pref))
    bar_h = recipe.bar_h
    caption_pref_h, caption_pref_w = layout.caption_dims
    gap_bc = max(5, int(caption_pref_h * 0.12)) if show_caption else 0
    caption_h = caption_pref_h if show_caption else 0
    bottom = _content_bottom(margin, show_footer=show_footer)
    block_h = bar_h + gap_bc + caption_h
    if margin + block_h > bottom:
        bar_h = max(_MIN_BAR_H, bar_h - (margin + block_h - bottom))
    bar_w = _estimate_barcode_width(payload, module)
    fitted_caption = FittedText("", caption_pref_h, caption_pref_w, False, False)
    if show_caption:
        fitted_caption = _fit_text_to_width(
            _caption_display(serial),
            caption_pref_h,
            caption_pref_w,
            float(bar_max_w),
            _MIN_CAPTION_H,
        )
        if fitted_caption.shrunk:
            warnings.append("Caption shrunk to fit")
        if fitted_caption.truncated:
            warnings.append("Caption shortened to fit")
        gap_bc = max(5, int(fitted_caption.font_h * 0.12))
        caption_h = fitted_caption.font_h
    block_h = bar_h + gap_bc + caption_h
    content_h = max(0, bottom - margin)
    if v_align == "top":
        y0 = margin
    elif v_align == "bottom":
        y0 = max(margin, bottom - block_h)
    else:
        y0 = margin + max(0, (content_h - block_h) // 2)
    # Center title against the barcode cell, then optical-nudge glyph ink down.
    title_y = y0 + max(0, (bar_h - fitted_title.font_h) // 2)
    if fitted_title.text:
        title_y += _optical_center_nudge(fitted_title.font_h, "middle")
        title_y = min(title_y, y0 + max(0, bar_h - fitted_title.font_h))

    # Horizontal placement of the title+barcode unit.
    unit_w = title_span + bar_w
    block_w = _LABEL_WIDTH - 2 * margin
    if h_align == "center":
        unit_x = margin + max(0, (block_w - unit_w) // 2)
    elif h_align == "right" and not raw_title:
        unit_x = max(margin, _LABEL_WIDTH - margin - bar_w)
    else:
        unit_x = margin
    if raw_title and not title_on_left:
        bar_x = unit_x
        title_x = unit_x + bar_w + gap
        # Keep title inside the label when barcode is wide.
        title_x = min(title_x, _LABEL_WIDTH - margin - title_col)
        bar_x = max(margin, min(bar_x, title_x - gap - bar_w))
    elif raw_title:
        title_x = unit_x
        bar_x = unit_x + title_col + gap
    else:
        title_x = margin
        bar_x = unit_x

    parts = [_zpl_header()]
    if raw_title and fitted_title.text:
        parts.append(
            _text_zpl(
                title_x,
                title_y,
                fitted_title.text,
                fitted_title.font_h,
                fitted_title.font_w,
                "left",
                title_col,
            )
        )
    parts.append(_barcode_zpl(bar_x, y0, payload, bar_h, module))
    if show_caption:
        parts.append(
            _text_zpl(
                bar_x,
                y0 + bar_h + gap_bc,
                fitted_caption.text,
                fitted_caption.font_h,
                fitted_caption.font_w,
                "left" if title_on_left or h_align == "center" else "right",
                max(bar_w, bar_max_w),
            )
        )
    if show_footer:
        parts.append(_signature_zpl(margin))
    parts.append(f"^PQ{copies}")
    parts.append("^XZ")
    return BuildResult("".join(parts), tuple(dict.fromkeys(warnings)))


def _build_stack_zpl(
    title: str,
    serial: str,
    recipe: LookRecipe,
    layout: LabelLayout,
    *,
    copies: int,
) -> BuildResult:
    payload = _decode_barcode_input(serial)
    warnings: list[str] = []
    has_title = bool(title.strip())
    title_pref_h, title_pref_w = layout.title_dims
    caption_pref_h, caption_pref_w = layout.caption_dims
    show_footer = layout.show_footer
    dense = (title_pref_h + recipe.bar_h + caption_pref_h) > 220
    margin = _adaptive_margin(dense)
    bottom = _content_bottom(margin, show_footer=show_footer)
    block_w = _LABEL_WIDTH - 2 * margin
    quiet = max(10, recipe.module_pref * 8)
    module = _fit_module(
        payload, recipe.module_pref, block_w - quiet, recipe.bar_fill
    )
    warnings.extend(_barcode_warnings(module, recipe.module_pref))

    fitted_title = FittedText("", 0, 0, False, False)
    if has_title:
        fitted_title = _fit_text_to_width(
            " ".join(title.split()),
            title_pref_h,
            title_pref_w,
            float(block_w),
            _MIN_TITLE_H,
        )
        if fitted_title.shrunk:
            warnings.append("Title shrunk to fit")
        if fitted_title.truncated:
            warnings.append("Title shortened to fit")

    fitted_caption = _fit_text_to_width(
        _caption_display(serial),
        caption_pref_h,
        caption_pref_w,
        float(block_w),
        _MIN_CAPTION_H,
    )
    if fitted_caption.shrunk:
        warnings.append("Caption shrunk to fit")
    if fitted_caption.truncated:
        warnings.append("Caption shortened to fit")

    title_h = fitted_title.font_h if has_title else 0
    gap_tb = max(6, int(title_h * 0.18)) if has_title else 0
    gap_bc = max(5, int(fitted_caption.font_h * 0.12))
    bar_h = recipe.bar_h
    block_h = title_h + gap_tb + bar_h + gap_bc + fitted_caption.font_h
    if margin + block_h > bottom:
        overflow = margin + block_h - bottom
        bar_h = max(_MIN_BAR_H, bar_h - overflow)
        block_h = title_h + gap_tb + bar_h + gap_bc + fitted_caption.font_h
    h_align, v_align = _NOTE_ALIGN[layout.note_align]
    content_h = max(0, bottom - margin)
    if v_align == "top":
        y = margin
    elif v_align == "bottom":
        y = max(margin, bottom - block_h)
    else:
        y = margin + max(0, (content_h - block_h) // 2)
    parts = [_zpl_header()]
    if has_title:
        parts.append(
            _text_zpl(
                margin,
                y,
                fitted_title.text,
                fitted_title.font_h,
                fitted_title.font_w,
                h_align,
                block_w,
            )
        )
        y += title_h + gap_tb
    bar_w = _estimate_barcode_width(payload, module)
    if h_align == "left":
        bar_x = margin
    elif h_align == "right":
        bar_x = max(margin, _LABEL_WIDTH - margin - bar_w)
    else:
        bar_x = margin + max(0, (block_w - bar_w) // 2)
    parts.append(_barcode_zpl(bar_x, y, payload, bar_h, module))
    y += bar_h + gap_bc
    parts.append(
        _text_zpl(
            margin,
            y,
            fitted_caption.text,
            fitted_caption.font_h,
            fitted_caption.font_w,
            h_align,
            block_w,
        )
    )
    if show_footer:
        parts.append(_signature_zpl(margin))
    parts.append(f"^PQ{copies}")
    parts.append("^XZ")
    return BuildResult("".join(parts), tuple(dict.fromkeys(warnings)))


def _compose_label(
    title: str,
    serial: str,
    layout: LabelLayout | None = None,
    *,
    copies: int = 1,
) -> BuildResult:
    layout = layout or LabelLayout()
    quantity = max(1, min(10, int(copies)))
    if not serial.strip():
        return _build_note_zpl(title, layout, copies=quantity)
    # Note look with a barcode value → print as Quiet (UI normally auto-switches).
    look = _DEFAULT_LOOK if layout.look == _NOTE_LOOK else layout.look
    recipe = _LOOKS[look]
    if recipe.stack == "side":
        return _build_side_zpl(
            title,
            serial.strip(),
            recipe,
            layout,
            copies=quantity,
        )
    return _build_stack_zpl(
        title,
        serial.strip(),
        recipe,
        layout,
        copies=quantity,
    )


def _build_zpl(
    title: str,
    serial: str,
    layout: LabelLayout | None = None,
    *,
    copies: int = 1,
) -> str:
    return _compose_label(title, serial, layout, copies=copies).zpl


_UI_SCALE = 2
_PREVIEW_DEBOUNCE_MS = 350
_PREVIEW_MAX_WIDTH = 360
_PREVIEW_MAX_HEIGHT = 160
_BUTTON_GAP = 14
_STATUS_MAX_CHARS = 72
_SUCCESS_HOLD_MS = 1100
# One-click title prefixes under the Title field.
_TITLE_CHIPS = ("Act", "S/N", "Req", "Ord")
_WINDOW_BOTTOM_MARGIN = 20
# Calm workshop palette — cool mist + deep slate ink + quiet teal.
# Psychology: soft ground lowers arousal; high-contrast type aids scanning;
# teal CTA signals “go” without alarm (unlike red) or SaaS-purple cliché.
_THEME = {
    "bg": "#E7EEF2",
    "surface": "#F5F8FA",
    "stage": "#D5DEE5",
    "ink": "#1A2A33",
    "muted": "#5C6D78",
    "line": "#C5D0D8",
    "accent": "#2C6B66",
    "accent_hover": "#245853",
    "accent_soft": "#D7E8E6",
    "ok": "#2F6B4F",
    "warn": "#8F6A28",
    "danger": "#A13D3D",
    "white": "#FFFFFF",
}


def _pick_ui_font() -> str:
    """Prefer a calm humanist sans; avoid Inter/Roboto/Arial defaults."""
    available = {name.lower() for name in tkfont.families()}
    for candidate in (
        "Cantarell",
        "Noto Sans Display",
        "Segoe UI Variable",
        "Segoe UI",
        "Noto Sans",
        "DejaVu Sans",
    ):
        if candidate.lower() in available:
            return candidate
    return "TkDefaultFont"


def _pick_mono_font() -> str:
    available = {name.lower() for name in tkfont.families()}
    for candidate in (
        "JetBrains Mono",
        "Cascadia Mono",
        "Cascadia Code",
        "Consolas",
        "DejaVu Sans Mono",
    ):
        if candidate.lower() in available:
            return candidate
    return "TkFixedFont"


def _apply_app_theme() -> tuple[str, str]:
    return _pick_ui_font(), _pick_mono_font()


def _status_line(message: str) -> str:
    text = " ".join(message.split())
    if len(text) <= _STATUS_MAX_CHARS:
        return text
    return text[: _STATUS_MAX_CHARS - 1] + "…"


def _labelary_url() -> str:
    return (
        f"http://api.labelary.com/v1/printers/"
        f"{_LABELARY_DPMM}dpmm/labels/{_LABELARY_WIDTH_IN}x{_LABELARY_HEIGHT_IN}/0/"
    )


def _fetch_labelary_png(zpl: str) -> bytes:
    request = urllib.request.Request(
        _labelary_url(),
        data=zpl.encode("utf-8"),
        headers={
            "Accept": "image/png",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = detail or exc.reason
        raise RuntimeError(f"Labelary error ({exc.code}): {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Preview unavailable: {exc.reason}") from exc


def _png_to_preview_image(
    png_data: bytes,
    max_width: int = _PREVIEW_MAX_WIDTH,
    max_height: int = _PREVIEW_MAX_HEIGHT,
) -> ImageTk.PhotoImage:
    """Scale the label and seat it on a soft shadowed stage (figure–ground)."""
    label = Image.open(io.BytesIO(png_data)).convert("RGBA")
    scale = min(
        1.0,
        max_width / max(1, label.width),
        max_height / max(1, label.height),
    )
    if scale < 1.0:
        label = label.resize(
            (max(1, int(label.width * scale)), max(1, int(label.height * scale))),
            Image.Resampling.LANCZOS,
        )

    pad_x, pad_y = 12, 10
    stage_w = label.width + pad_x * 2 + 8
    stage_h = label.height + pad_y * 2 + 8
    stage = Image.new("RGBA", (stage_w, stage_h), (0, 0, 0, 0))

    shadow_layer = Image.new("RGBA", (stage_w, stage_h), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.rounded_rectangle(
        (pad_x + 3, pad_y + 5, pad_x + label.width + 3, pad_y + label.height + 5),
        radius=6,
        fill=(26, 42, 51, 55),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    stage.alpha_composite(shadow_layer)

    card = Image.new("RGBA", (label.width + 2, label.height + 2), (255, 255, 255, 255))
    card_draw = ImageDraw.Draw(card)
    card_draw.rectangle(
        (0, 0, card.width - 1, card.height - 1), outline=(197, 208, 216, 255)
    )
    stage.paste(card, (pad_x - 1, pad_y - 1))
    stage.alpha_composite(label, (pad_x, pad_y))
    return ImageTk.PhotoImage(stage)



def _preview_label(
    title: str, serial: str, layout: LabelLayout | None = None
) -> BuildResult:
    return _compose_label(title.strip(), serial.strip(), layout)


def _send_zpl(zpl: str) -> tuple[str, int]:
    host, port = _load_printer_config()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(zpl.encode("utf-8"))
    return host, port


def _work_area_origin_and_size(window: tk.Tk) -> tuple[int, int, int, int]:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        cursor = POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        monitor = user32.MonitorFromPoint(cursor, 2)  # MONITOR_DEFAULTTONEAREST
        if monitor:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right - work.left, work.bottom - work.top

    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def _place_window(
    window: tk.Misc,
    *,
    width: int | None = None,
    height: int | None = None,
    min_width: int = 0,
    anchor: str = "center",
) -> None:
    """Size and place a window. anchor: center | bottom (bottom-center of work area)."""
    window.update_idletasks()
    width = max(width or window.winfo_reqwidth(), min_width)
    height = height or window.winfo_reqheight()
    area_x, area_y, area_w, area_h = _work_area_origin_and_size(window)
    width = min(width, max(320, area_w - 24))
    height = min(height, max(320, area_h - 24))
    x = area_x + max(0, (area_w - width) // 2)
    if anchor == "bottom":
        y = area_y + max(0, area_h - height - _WINDOW_BOTTOM_MARGIN)
    else:
        y = area_y + max(0, (area_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    try:
        window.deiconify()
    except tk.TclError:
        pass


def _center_window(
    window: tk.Misc,
    *,
    width: int | None = None,
    height: int | None = None,
    min_width: int = 0,
) -> None:
    _place_window(
        window, width=width, height=height, min_width=min_width, anchor="center"
    )


def _parse_args():
    saved = _load_label_settings()
    parser = argparse.ArgumentParser(description="Print a barcode label on a Zebra printer.")
    parser.add_argument(
        "title",
        nargs="?",
        default="",
        help="Optional title seed (everyday launch leaves this empty)",
    )
    parser.add_argument(
        "--look",
        choices=[*_LOOK_ORDER, _NOTE_LOOK],
        default=saved.look,
        help="Label look / visual recipe",
    )
    parser.add_argument(
        "--note-size",
        choices=_SIZE_STEPS,
        default=saved.note_size,
        help="Note text size when printing without a barcode",
    )
    parser.add_argument(
        "--note-align",
        choices=_NOTE_ALIGN_STEPS,
        default=saved.note_align,
        help="Label alignment (Left/Right/Top/Bottom/Center) for all looks",
    )
    parser.add_argument(
        "--title-size",
        choices=_SIZE_STEPS,
        default=saved.title_size,
        help="Title text size on barcode labels",
    )
    parser.add_argument(
        "--caption-size",
        choices=_SIZE_STEPS,
        default=saved.caption_size,
        help="Caption text size under the barcode",
    )
    parser.add_argument(
        "--no-caption",
        action="store_true",
        help="Hide barcode caption text (Side title look)",
    )
    parser.add_argument(
        "--caption",
        action="store_true",
        help="Show barcode caption text",
    )
    parser.add_argument(
        "--no-footer",
        action="store_true",
        help="Hide author/date footer stamp",
    )
    parser.add_argument(
        "--footer",
        action="store_true",
        help="Show author/date footer stamp",
    )
    return parser.parse_args(), saved


def _pack_inline_size_stepper(
    parent: ttk.Frame,
    *,
    get_size,
    set_size,
    on_change,
    frame_style: str = "Card.TFrame",
    label_style: str = "Field.TLabel",
):
    """Compact − / size / + control for the right side of an input row.

    Returns (stepper_frame, refresh_display).
    """
    stepper = ttk.Frame(parent, style=frame_style)
    display_var = tk.StringVar(value=_SIZE_COMPACT[_normalize_size(get_size())])

    def refresh_display(*_args: object) -> None:
        display_var.set(_SIZE_COMPACT[_normalize_size(get_size())])

    def bump(delta: int) -> None:
        set_size(_bump_size(get_size(), delta))
        refresh_display()
        on_change()

    ttk.Button(stepper, text="−", width=3, command=lambda: bump(-1)).pack(side=tk.LEFT)
    ttk.Label(
        stepper, textvariable=display_var, width=4, anchor="center", style=label_style
    ).pack(side=tk.LEFT, padx=2)
    ttk.Button(stepper, text="+", width=3, command=lambda: bump(1)).pack(side=tk.LEFT)
    return stepper, refresh_display


def _look_display_names() -> list[str]:
    return [_LOOKS[k].title for k in _LOOK_ORDER] + [_NOTE_LOOK_TITLE]


def _look_id_from_title(title: str) -> str:
    if title.strip() == _NOTE_LOOK_TITLE:
        return _NOTE_LOOK
    for key in _LOOK_ORDER:
        if _LOOKS[key].title == title:
            return key
    return _DEFAULT_LOOK


def _look_title_for_id(look_id: str) -> str:
    if look_id == _NOTE_LOOK:
        return _NOTE_LOOK_TITLE
    return _LOOKS.get(look_id, _LOOKS[_DEFAULT_LOOK]).title


def _apply_ttk_theme(root: tk.Misc, ui_font: str) -> None:
    t = _THEME
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=t["bg"], foreground=t["ink"], font=(ui_font, 11))
    style.configure("TFrame", background=t["bg"])
    style.configure("Card.TFrame", background=t["surface"])
    style.configure("Stage.TFrame", background=t["stage"])
    style.configure("More.TFrame", background=t["bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["ink"], font=(ui_font, 11))
    style.configure("Card.TLabel", background=t["surface"], foreground=t["ink"])
    style.configure("Brand.TLabel", background=t["surface"], foreground=t["ink"], font=(ui_font, 18, "bold"))
    style.configure("Hint.TLabel", background=t["surface"], foreground=t["muted"], font=(ui_font, 10))
    style.configure("Field.TLabel", background=t["surface"], foreground=t["muted"], font=(ui_font, 10))
    style.configure("More.TLabel", background=t["bg"], foreground=t["muted"], font=(ui_font, 10))
    style.configure("Status.TLabel", background=t["surface"], foreground=t["danger"], font=(ui_font, 10))
    style.configure("Warn.TLabel", background=t["surface"], foreground=t["warn"], font=(ui_font, 10))
    style.configure("Ok.TLabel", background=t["surface"], foreground=t["ok"], font=(ui_font, 10))
    style.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"], font=(ui_font, 10))
    style.configure("TButton", font=(ui_font, 11), padding=(10, 6))
    style.configure("Accent.TButton", font=(ui_font, 12, "bold"), padding=(14, 8))
    style.map(
        "Accent.TButton",
        background=[("!disabled", t["accent"]), ("pressed", t["accent_hover"]), ("active", t["accent_hover"])],
        foreground=[("!disabled", t["white"])],
    )
    style.configure("Align.TButton", font=(ui_font, 10), padding=(8, 4))
    style.configure("AlignOn.TButton", font=(ui_font, 10, "bold"), padding=(8, 4))
    style.configure(
        "Chip.TButton",
        font=(ui_font, 9),
        padding=(5, 1),
        foreground=t["muted"],
    )
    style.map(
        "Chip.TButton",
        foreground=[("!disabled", t["muted"]), ("active", t["ink"]), ("pressed", t["ink"])],
    )
    style.map(
        "AlignOn.TButton",
        background=[("!disabled", t["accent"]), ("pressed", t["accent_hover"]), ("active", t["accent_hover"])],
        foreground=[("!disabled", t["white"])],
    )
    style.configure("Link.TButton", font=(ui_font, 10), foreground=t["muted"], background=t["surface"], borderwidth=0)
    style.configure("TEntry", fieldbackground=t["white"], foreground=t["ink"], padding=6)
    style.configure("TCombobox", fieldbackground=t["white"], foreground=t["ink"], padding=4)
    style.configure("TCheckbutton", background=t["surface"], foreground=t["muted"], font=(ui_font, 10))
    style.configure("More.TCheckbutton", background=t["bg"], foreground=t["muted"], font=(ui_font, 10))


def _open_printer_manager(parent: tk.Tk, on_changed) -> None:
    """Modal dialog to add / edit / remove named printers in printer_config.json."""
    store = _load_printer_store()
    t = _THEME
    ui_font = _pick_ui_font()

    win = tk.Toplevel(parent)
    win.title("Manage printers")
    win.transient(parent)
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass
    win.configure(bg=t["bg"])
    _apply_ttk_theme(win, ui_font)

    body = ttk.Frame(win, style="Card.TFrame", padding=16)
    body.grid(row=0, column=0, padx=14, pady=14, sticky="nsew")
    body.grid_columnconfigure(1, weight=1)

    ttk.Label(body, text="Printers", style="Brand.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    ttk.Label(body, text="Saved on this computer only", style="Hint.TLabel").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(0, 8)
    )

    listbox = tk.Listbox(
        body,
        height=6,
        width=42,
        exportselection=False,
        activestyle="dotbox",
        background=t["bg"],
        foreground=t["ink"],
        selectbackground=t["accent"],
        selectforeground=t["white"],
        highlightthickness=1,
        highlightbackground=t["line"],
        borderwidth=0,
        font=(ui_font, 11),
    )
    listbox.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 10))

    name_var = tk.StringVar()
    host_var = tk.StringVar()
    port_var = tk.StringVar(value=str(_DEFAULT_PRINTER_PORT))
    status_var = tk.StringVar(value="")

    ttk.Label(body, text="Name", style="Field.TLabel").grid(row=3, column=0, sticky="w", pady=4)
    name_entry = ttk.Entry(body, textvariable=name_var, width=32, font=(ui_font, 12))
    name_entry.grid(row=3, column=1, sticky="we", pady=4)

    ttk.Label(body, text="IP / host", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=4)
    host_entry = ttk.Entry(body, textvariable=host_var, width=32, font=(ui_font, 12))
    host_entry.grid(row=4, column=1, sticky="we", pady=4)

    ttk.Label(body, text="Port", style="Field.TLabel").grid(row=5, column=0, sticky="w", pady=4)
    port_entry = ttk.Entry(body, textvariable=port_var, width=10, font=(ui_font, 12))
    port_entry.grid(row=5, column=1, sticky="w", pady=4)

    status_label = ttk.Label(body, textvariable=status_var, style="Hint.TLabel")
    status_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

    editing_name = {"value": ""}

    def refresh_list(select_name: str | None = None) -> None:
        listbox.delete(0, tk.END)
        for printer in store.printers:
            mark = " ●" if printer.name == store.selected else ""
            listbox.insert(tk.END, f"{printer.name}  ·  {printer.host}:{printer.port}{mark}")
        if select_name:
            for i, printer in enumerate(store.printers):
                if printer.name == select_name:
                    listbox.selection_clear(0, tk.END)
                    listbox.selection_set(i)
                    listbox.see(i)
                    break

    def clear_fields() -> None:
        editing_name["value"] = ""
        name_var.set("")
        host_var.set("")
        port_var.set(str(_DEFAULT_PRINTER_PORT))

    def load_selected(_event=None) -> None:
        selection = listbox.curselection()
        if not selection:
            return
        printer = store.printers[selection[0]]
        editing_name["value"] = printer.name
        name_var.set(printer.name)
        host_var.set(printer.host)
        port_var.set(str(printer.port))

    def persist() -> None:
        _save_printer_store(store)
        on_changed(store)

    def on_new() -> None:
        clear_fields()
        status_var.set("New printer")
        name_entry.focus_set()

    def on_save() -> None:
        name = name_var.get().strip()
        host = host_var.get().strip()
        try:
            port = int(port_var.get().strip())
        except ValueError:
            status_var.set("Port must be a number")
            return
        if not name or not host:
            status_var.set("Name and IP / host are required")
            return
        if not (1 <= port <= 65535):
            status_var.set("Port must be between 1 and 65535")
            return
        entry = PrinterEntry(name=name, host=host, port=port)
        old = editing_name["value"]
        if old and old != name:
            store.printers = [p for p in store.printers if p.name != old]
            if store.selected == old:
                store.selected = name
        else:
            store.printers = [p for p in store.printers if p.name != name]
        store.printers.append(entry)
        store.printers.sort(key=lambda p: p.name.lower())
        if not store.selected:
            store.selected = name
        persist()
        editing_name["value"] = name
        refresh_list(name)
        status_var.set(f'Saved "{name}"')

    def on_delete() -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showinfo("Printer", "Select a printer to delete.", parent=win)
            return
        name = store.printers[selection[0]].name
        if not messagebox.askyesno("Delete printer", f'Delete "{name}"?', parent=win):
            return
        store.printers = [p for p in store.printers if p.name != name]
        if store.selected == name:
            store.selected = store.printers[0].name if store.printers else ""
        persist()
        clear_fields()
        refresh_list()
        status_var.set(f'Deleted "{name}"')

    def on_use() -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showinfo("Printer", "Select a printer to use.", parent=win)
            return
        store.selected = store.printers[selection[0]].name
        persist()
        refresh_list(store.selected)
        status_var.set(f'Using "{store.selected}"')

    def on_close() -> None:
        win.destroy()

    buttons = ttk.Frame(body, style="Card.TFrame")
    buttons.grid(row=7, column=0, columnspan=2, sticky="we", pady=(14, 0))
    ttk.Button(buttons, text="Clear", command=on_new).pack(side=tk.LEFT)
    ttk.Button(buttons, text="Save", command=on_save).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(buttons, text="Delete", command=on_delete).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(buttons, text="Use", command=on_use).pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(buttons, text="Done", command=on_close).pack(side=tk.RIGHT)

    listbox.bind("<<ListboxSelect>>", load_selected)
    listbox.bind("<Double-Button-1>", lambda _e: on_use())
    refresh_list()
    if not store.printers:
        status_var.set("No printers yet — fill the fields and click Save")
        name_entry.focus_set()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.update_idletasks()
    _center_window(win)
    win.grab_set()
    parent.wait_window(win)


def _ask_label_fields(default_title: str = "", layout: LabelLayout | None = None):
    """Everyday UI on stable ttk: title, barcode, preview, print. Extras under More."""
    layout = layout or _load_label_settings()
    t = _THEME
    # Remember last barcode look so Note mode doesn't wipe Side title / Quiet / etc.
    last_barcode_look = {
        "id": layout.look if layout.look != _NOTE_LOOK else _DEFAULT_LOOK
    }

    root = tk.Tk()
    root.withdraw()
    ui_font = _pick_ui_font()
    mono_font = _pick_mono_font()
    _apply_ttk_theme(root, ui_font)
    root.title("Print label")
    root.resizable(False, False)
    root.configure(bg=t["bg"])
    try:
        # Mild HiDPI bump only — avoid CustomTkinter-style window scaling crashes.
        current = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", min(2.0, current * 1.25))
    except tk.TclError:
        pass
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    outer = ttk.Frame(root, style="TFrame", padding=12)
    outer.pack(fill="both", expand=True)

    # Action bar packed first at bottom so More/Close/Print stay visible.
    more_open = tk.BooleanVar(value=False)
    actions = ttk.Frame(outer, style="TFrame")
    actions.pack(side=tk.BOTTOM, fill="x", pady=(10, 0))
    actions.grid_columnconfigure(0, weight=1)
    session_var = tk.StringVar(value="")
    ttk.Label(actions, textvariable=session_var, style="Muted.TLabel").grid(
        row=0, column=0, sticky="w"
    )
    btn_row = ttk.Frame(actions, style="TFrame")
    btn_row.grid(row=0, column=1, sticky="e")

    def on_more_click() -> None:
        more_open.set(not more_open.get())

    more_button = ttk.Button(btn_row, text="More", command=on_more_click)
    more_button.pack(side=tk.LEFT)
    cancel_button = ttk.Button(btn_row, text="Close", command=lambda: on_cancel())
    cancel_button.pack(side=tk.LEFT, padx=(8, 0))
    print_button = ttk.Button(
        btn_row, text="Print  ↵", style="Accent.TButton", command=lambda: on_print()
    )
    print_button.pack(side=tk.LEFT, padx=(8, 0))

    card = ttk.Frame(outer, style="Card.TFrame", padding=16)
    card.pack(side=tk.TOP, fill="both", expand=True)
    card.grid_columnconfigure(0, weight=1)
    card.grid_columnconfigure(1, weight=1)

    printer_store = {"value": _load_printer_store()}
    auto_look = {"busy": False}
    preview_state = {
        "photo": None,
        "request_id": 0,
        "after_id": None,
        "poll_after": None,
        "closed": False,
        "printed": 0,
    }
    preview_queue: queue.Queue = queue.Queue()
    close_armed_at = {"ms": 0}

    # Wide layout: form left, live preview right.
    form = ttk.Frame(card, style="Card.TFrame")
    form.grid(row=0, column=0, sticky="nwe", padx=(0, 12))
    form.grid_columnconfigure(0, weight=1)

    # Everyday launch starts blank — ignore preset titles from old hotkeys/CLI.
    title_var = tk.StringVar(value="")
    serial_var = tk.StringVar()
    initial_look_title = _NOTE_LOOK_TITLE
    look_title_var = tk.StringVar(value=initial_look_title)
    note_size_var = tk.StringVar(value=layout.note_size)
    note_align_var = tk.StringVar(value=layout.note_align)
    title_size_var = tk.StringVar(value=layout.title_size)
    caption_size_var = tk.StringVar(value=layout.caption_size)
    show_caption_var = tk.BooleanVar(value=layout.show_caption)
    show_footer_var = tk.BooleanVar(value=layout.show_footer)
    # Copies are never persisted — always start at 1.
    copies_var = tk.StringVar(value="1")
    printer_var = tk.StringVar(value=printer_store["value"].selected)

    def _on_inline_size_change() -> None:
        schedule_preview()
        persist_settings()

    def _title_size_get() -> str:
        # Title field steppers control note size without a barcode.
        if serial_var.get().strip():
            return title_size_var.get()
        return note_size_var.get()

    def _title_size_set(value: str) -> None:
        if serial_var.get().strip():
            title_size_var.set(value)
        else:
            note_size_var.set(value)

    ttk.Label(form, text="Title", style="Field.TLabel").grid(row=0, column=0, sticky="w")
    title_row = ttk.Frame(form, style="Card.TFrame")
    title_row.grid(row=1, column=0, sticky="we", pady=(2, 4))
    title_row.grid_columnconfigure(0, weight=1)
    title_entry = ttk.Entry(title_row, textvariable=title_var, width=28, font=(ui_font, 13))
    title_entry.grid(row=0, column=0, sticky="we")
    title_stepper, refresh_title_size_display = _pack_inline_size_stepper(
        title_row,
        get_size=_title_size_get,
        set_size=_title_size_set,
        on_change=_on_inline_size_change,
    )
    title_stepper.grid(row=0, column=1, sticky="e", padx=(8, 0))

    chip_row = ttk.Frame(form, style="Card.TFrame")
    chip_row.grid(row=2, column=0, sticky="w", pady=(0, 10))

    def apply_title_chip(value: str) -> None:
        title_var.set(value)
        focus_barcode()

    for chip in _TITLE_CHIPS:
        ttk.Button(
            chip_row,
            text=chip,
            style="Chip.TButton",
            command=lambda v=chip: apply_title_chip(v),
        ).pack(side=tk.LEFT, padx=(0, 4))

    ttk.Label(form, text="Barcode", style="Field.TLabel").grid(row=3, column=0, sticky="w")
    barcode_row = ttk.Frame(form, style="Card.TFrame")
    barcode_row.grid(row=4, column=0, sticky="we", pady=(2, 0))
    barcode_row.grid_columnconfigure(0, weight=1)
    serial_entry = ttk.Entry(
        barcode_row, textvariable=serial_var, width=28, font=(mono_font, 13)
    )
    serial_entry.grid(row=0, column=0, sticky="we")
    caption_stepper, refresh_caption_size_display = _pack_inline_size_stepper(
        barcode_row,
        get_size=lambda: caption_size_var.get(),
        set_size=lambda value: caption_size_var.set(value),
        on_change=_on_inline_size_change,
    )
    caption_stepper.grid(row=0, column=1, sticky="e", padx=(8, 0))

    stage = ttk.Frame(card, style="Stage.TFrame", padding=6)
    stage.grid(row=0, column=1, sticky="n")
    stage.grid_columnconfigure(0, weight=1)
    stage.grid_rowconfigure(0, minsize=_PREVIEW_MAX_HEIGHT)
    # tk.Label holds the preview image reliably across themes.
    preview_label = tk.Label(
        stage,
        text="Preview",
        bg=t["stage"],
        fg=t["muted"],
        font=(ui_font, 11),
        anchor="center",
        justify="center",
        width=32,
        height=6,
    )
    preview_label.grid(row=0, column=0, sticky="n")

    status_var = tk.StringVar(value="")
    status_label = ttk.Label(card, textvariable=status_var, style="Status.TLabel")
    status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    more = ttk.Frame(card, style="More.TFrame", padding=(0, 8, 0, 0))
    more.grid_columnconfigure(1, weight=1)

    ttk.Label(more, text="Printer", style="More.TLabel").grid(row=0, column=0, sticky="w", pady=4)
    printer_row = ttk.Frame(more, style="More.TFrame")
    printer_row.grid(row=0, column=1, sticky="w", pady=4)
    printer_combo = ttk.Combobox(
        printer_row,
        textvariable=printer_var,
        values=printer_store["value"].names(),
        state="readonly",
        width=22,
        font=(ui_font, 11),
    )
    printer_combo.pack(side=tk.LEFT)

    def refresh_printer_combo(store: PrinterStore | None = None) -> None:
        store = store or _load_printer_store()
        printer_store["value"] = store
        names = store.names()
        printer_combo.configure(values=names)
        if store.selected and store.selected in names:
            printer_var.set(store.selected)
        elif names:
            printer_var.set(names[0])
        else:
            printer_var.set("")

    def on_printer_selected(_event=None) -> None:
        name = printer_var.get().strip()
        store = printer_store["value"]
        if not name or name == store.selected or store.get(name) is None:
            return
        store.selected = name
        _save_printer_store(store)

    def open_manager() -> None:
        _open_printer_manager(root, refresh_printer_combo)

    ttk.Button(printer_row, text="Manage…", command=open_manager).pack(side=tk.LEFT, padx=(8, 0))
    printer_combo.bind("<<ComboboxSelected>>", on_printer_selected)

    look_label = ttk.Label(more, text="Look", style="More.TLabel")
    look_label.grid(row=1, column=0, sticky="w", pady=4)
    look_combo = ttk.Combobox(
        more,
        textvariable=look_title_var,
        values=_look_display_names(),
        state="readonly",
        width=16,
        font=(ui_font, 11),
    )
    look_combo.grid(row=1, column=1, sticky="w", pady=4)

    def _on_setting_change() -> None:
        schedule_preview()
        persist_settings()

    align_row = ttk.Frame(more, style="More.TFrame")
    align_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
    ttk.Label(align_row, text="Align", style="More.TLabel").pack(side=tk.LEFT)
    align_btns: dict[str, ttk.Button] = {}

    def _paint_align_buttons() -> None:
        current = _normalize_note_align(note_align_var.get())
        for key, btn in align_btns.items():
            btn.configure(style="AlignOn.TButton" if key == current else "Align.TButton")

    def _set_note_align(value: str) -> None:
        note_align_var.set(_normalize_note_align(value))
        _paint_align_buttons()
        _on_setting_change()

    align_host = ttk.Frame(align_row, style="More.TFrame")
    align_host.pack(side=tk.LEFT, padx=(8, 0))
    for key in _NOTE_ALIGN_STEPS:
        btn = ttk.Button(
            align_host,
            text=_NOTE_ALIGN_LABELS[key],
            style="Align.TButton",
            command=lambda k=key: _set_note_align(k),
        )
        btn.pack(side=tk.LEFT, padx=(0, 4))
        align_btns[key] = btn
    _paint_align_buttons()

    caption_row = ttk.Frame(more, style="More.TFrame")
    caption_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

    def _on_caption_toggle() -> None:
        update_mode_chrome()
        _on_setting_change()

    ttk.Checkbutton(
        caption_row,
        text="Barcode caption",
        variable=show_caption_var,
        style="More.TCheckbutton",
        command=_on_caption_toggle,
    ).pack(side=tk.LEFT)

    footer_row = ttk.Frame(more, style="More.TFrame")
    footer_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
    ttk.Checkbutton(
        footer_row,
        text="Footer (name · date)",
        variable=show_footer_var,
        style="More.TCheckbutton",
        command=_on_setting_change,
    ).pack(side=tk.LEFT)

    ttk.Label(more, text="Copies", style="More.TLabel").grid(row=5, column=0, sticky="w", pady=4)
    copies_combo = ttk.Combobox(
        more,
        textvariable=copies_var,
        values=[str(n) for n in range(1, 11)],
        state="readonly",
        width=5,
        font=(ui_font, 11),
    )
    copies_combo.grid(row=5, column=1, sticky="w", pady=4)

    def _fit_window() -> None:
        root.update_idletasks()
        card.update_idletasks()
        actions.update_idletasks()
        # Wide short dialog, pinned to the bottom so it doesn't cover work above.
        pad = 36
        needed_w = max(760, card.winfo_reqwidth() + pad, actions.winfo_reqwidth() + pad)
        needed_h = card.winfo_reqheight() + actions.winfo_reqheight() + pad
        min_h = actions.winfo_reqheight() + pad + 180
        needed_h = max(needed_h, min_h)
        try:
            root.minsize(720, min_h)
        except tk.TclError:
            pass
        _place_window(
            root,
            width=needed_w,
            height=needed_h,
            min_width=720,
            anchor="bottom",
        )

    def toggle_more(*_args: object) -> None:
        if more_open.get():
            more.grid(row=2, column=0, columnspan=2, sticky="we", pady=(10, 0))
            more_button.configure(text="Less")
        else:
            more.grid_remove()
            more_button.configure(text="More")
        root.after_idle(_fit_window)

    more_open.trace_add("write", toggle_more)

    def current_layout() -> LabelLayout:
        look = _look_id_from_title(look_title_var.get())
        if look == _NOTE_LOOK:
            look = last_barcode_look["id"]
        return LabelLayout(
            look=look,
            note_size=note_size_var.get(),
            note_align=note_align_var.get(),
            title_size=title_size_var.get(),
            caption_size=caption_size_var.get(),
            show_caption=bool(show_caption_var.get()),
            show_footer=bool(show_footer_var.get()),
        )

    def persist_settings() -> None:
        _save_label_settings(current_layout())

    def current_copies() -> int:
        try:
            return max(1, min(10, int(copies_var.get())))
        except ValueError:
            return 1

    def reset_copies() -> None:
        copies_var.set("1")

    def set_look_title(title: str) -> None:
        if look_title_var.get() == title:
            return
        auto_look["busy"] = True
        try:
            look_title_var.set(title)
        finally:
            auto_look["busy"] = False

    def sync_auto_look(*_args: object) -> None:
        if auto_look["busy"]:
            return
        has_barcode = bool(serial_var.get().strip())
        current = look_title_var.get()
        current_id = _look_id_from_title(current)
        if not has_barcode:
            if current_id != _NOTE_LOOK:
                last_barcode_look["id"] = current_id
            set_look_title(_NOTE_LOOK_TITLE)
        elif current == _NOTE_LOOK_TITLE:
            set_look_title(_look_title_for_id(last_barcode_look["id"]))

    def update_mode_chrome(*_args: object) -> None:
        has_barcode = bool(serial_var.get().strip())
        look_id = _look_id_from_title(look_title_var.get())
        # Align applies to every look (Note and barcode).
        align_row.grid()
        if has_barcode:
            look_combo.configure(state="readonly")
            if look_id != _NOTE_LOOK:
                last_barcode_look["id"] = look_id
        else:
            set_look_title(_NOTE_LOOK_TITLE)
            look_combo.configure(state="disabled")
            look_id = _NOTE_LOOK
        # Caption toggle is for Side title only.
        show_cap_toggle = has_barcode and look_id == "side"
        if show_cap_toggle:
            caption_row.grid()
        else:
            caption_row.grid_remove()
        # Caption size −/+ sits beside the barcode field when a caption applies.
        show_cap_size = has_barcode and (
            look_id != "side" or bool(show_caption_var.get())
        )
        if show_cap_size:
            caption_stepper.grid()
        else:
            caption_stepper.grid_remove()
        refresh_title_size_display()
        refresh_caption_size_display()
        if more_open.get():
            root.after_idle(_fit_window)

    def set_status(kind: str, message: str) -> None:
        styles = {
            "ok": "Ok.TLabel",
            "warn": "Warn.TLabel",
            "error": "Status.TLabel",
            "info": "Muted.TLabel",
        }
        status_label.configure(style=styles.get(kind, "Muted.TLabel"))
        status_var.set(_status_line(message) if message else "")

    def apply_preview(
        request_id: int,
        png_data: bytes | None,
        error: str | None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        if preview_state["closed"] or request_id != preview_state["request_id"]:
            return
        try:
            if png_data is None:
                preview_state["photo"] = None
                preview_label.configure(image="", text=error or "Preview unavailable")
                set_status("error", error or "Preview unavailable")
                return
            photo = _png_to_preview_image(png_data)
            preview_state["photo"] = photo
            preview_label.configure(image=photo, text="")
            if warnings:
                set_status("warn", " · ".join(warnings))
            elif not status_var.get().startswith("Sent"):
                set_status("info", "")
        except Exception as exc:
            _log_exception("apply_preview", exc)
            preview_state["photo"] = None
            preview_label.configure(image="", text="Preview failed")
            set_status("error", f"Preview failed: {exc}")

    def refresh_preview() -> None:
        request_id = preview_state["request_id"] + 1
        preview_state["request_id"] = request_id
        try:
            built = _preview_label(title_var.get(), serial_var.get(), current_layout())
        except Exception as exc:
            _log_exception("refresh_preview.build", exc)
            apply_preview(request_id, None, f"Preview failed: {exc}", ())
            return
        zpl = built.zpl
        warnings = built.warnings

        def fetch() -> None:
            try:
                png_data = _fetch_labelary_png(zpl)
                error = None
            except Exception as exc:
                png_data = None
                error = str(exc)
            if not preview_state["closed"]:
                preview_queue.put((request_id, png_data, error, warnings))

        threading.Thread(target=fetch, daemon=True).start()

    def poll_preview_queue() -> None:
        try:
            while True:
                request_id, png_data, error, warnings = preview_queue.get_nowait()
                apply_preview(request_id, png_data, error, warnings)
        except queue.Empty:
            pass
        if not preview_state["closed"]:
            preview_state["poll_after"] = root.after(50, poll_preview_queue)

    def schedule_preview(*_args: object) -> None:
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        preview_state["after_id"] = root.after(_PREVIEW_DEBOUNCE_MS, refresh_preview)

    def set_printing(enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        print_button.state(state)
        cancel_button.state(state)
        title_entry.state(["!disabled"] if enabled else ["disabled"])
        serial_entry.state(["!disabled"] if enabled else ["disabled"])

    def focus_barcode() -> None:
        serial_entry.focus_set()
        try:
            serial_entry.selection_range(0, tk.END)
        except tk.TclError:
            pass

    def after_success(printer_name: str) -> None:
        preview_state["printed"] += 1
        n = preview_state["printed"]
        session_var.set(f"{n} printed" if n > 1 else "1 printed")
        set_status("ok", f"Sent to {printer_name} ✓")
        set_printing(True)
        reset_copies()
        serial_var.set("")
        root.after(50, focus_barcode)
        root.after(
            _SUCCESS_HOLD_MS,
            lambda: set_status("info", "")
            if status_var.get().startswith("Sent")
            else None,
        )

    def on_print() -> None:
        title = title_var.get().strip()
        serial = serial_var.get().strip()
        if not serial and not title:
            messagebox.showwarning(
                "Nothing to print",
                "Add a title or scan a barcode first.",
                parent=root,
            )
            title_entry.focus_set()
            return

        built = _compose_label(title, serial, current_layout(), copies=current_copies())
        zpl = built.zpl

        set_printing(False)
        set_status("info", "Sending…")
        root.update_idletasks()

        try:
            store = _load_printer_store()
            printer = store.selected_printer()
            if printer is None:
                raise FileNotFoundError(
                    "No printer configured. Open More → Manage…"
                )
            set_status("info", f"Connecting to {printer.name}…")
            root.update_idletasks()
            _send_zpl(zpl)
        except FileNotFoundError as exc:
            set_status("error", str(exc))
            set_printing(True)
            more_open.set(True)
            if messagebox.askyesno(
                "No printer",
                f"{exc}\n\nOpen printer manager now?",
                parent=root,
            ):
                open_manager()
            return
        except OSError as exc:
            try:
                host, port = _load_printer_config()
                target = f"{host}:{port}"
            except FileNotFoundError:
                target = "printer"
            set_status("error", f"Could not connect to {target}: {exc}")
            set_printing(True)
            focus_barcode()
            return
        except Exception as exc:
            set_status("error", f"Print failed: {type(exc).__name__}: {exc}")
            set_printing(True)
            focus_barcode()
            return

        after_success(printer.name)

    def on_cancel() -> None:
        preview_state["closed"] = True
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        poll_after = preview_state.get("poll_after")
        if poll_after is not None:
            root.after_cancel(poll_after)
        root.destroy()

    def on_escape(_event=None):
        try:
            now = int(root.tk.call("clock", "milliseconds"))
        except tk.TclError:
            now = close_armed_at["ms"]
        if now < close_armed_at["ms"]:
            return "break"
        on_cancel()
        return "break"

    title_var.trace_add("write", schedule_preview)
    title_var.trace_add("write", sync_auto_look)
    serial_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", sync_auto_look)
    serial_var.trace_add("write", update_mode_chrome)
    look_title_var.trace_add("write", schedule_preview)
    look_title_var.trace_add("write", update_mode_chrome)
    look_title_var.trace_add("write", lambda *_: persist_settings())
    look_combo.bind("<<ComboboxSelected>>", schedule_preview)
    note_size_var.trace_add("write", schedule_preview)
    note_align_var.trace_add("write", schedule_preview)
    title_size_var.trace_add("write", schedule_preview)
    caption_size_var.trace_add("write", schedule_preview)
    sync_auto_look()
    update_mode_chrome()
    persist_settings()

    def focus_title() -> None:
        title_entry.focus_set()
        try:
            title_entry.selection_range(0, tk.END)
        except tk.TclError:
            pass

    def on_title_return(_event=None):
        focus_barcode()
        return "break"

    for sequence in ("<Return>", "<KP_Enter>"):
        serial_entry.bind(sequence, lambda _event: on_print())
        title_entry.bind(sequence, on_title_return)
    root.bind("<Escape>", on_escape)
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def focus_initial_entry() -> None:
        try:
            root.lift()
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        # Always start on Title — chips and typing fill it; barcode is next.
        focus_title()

    def report_callback_exception(exc_type, exc_value, exc_traceback) -> None:
        try:
            with open(_ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"\n--- {date.today().isoformat()} tk_callback ---\n")
                fh.write(
                    "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                )
        except OSError:
            pass
        try:
            set_status("error", f"{getattr(exc_type, '__name__', 'Error')}: {exc_value}")
        except Exception:
            pass

    root.report_callback_exception = report_callback_exception

    _fit_window()
    try:
        close_armed_at["ms"] = int(root.tk.call("clock", "milliseconds")) + 2000
    except tk.TclError:
        close_armed_at["ms"] = 0
    poll_preview_queue()
    schedule_preview()
    root.after(0, focus_initial_entry)
    root.after(150, focus_initial_entry)
    root.mainloop()

def print_label(default_title: str = "", layout: LabelLayout | None = None):
    _ask_label_fields(default_title, layout)


if __name__ == "__main__":
    try:
        args, saved = _parse_args()
        show_caption = saved.show_caption
        if args.caption:
            show_caption = True
        elif args.no_caption:
            show_caption = False
        show_footer = saved.show_footer
        if args.footer:
            show_footer = True
        elif args.no_footer:
            show_footer = False
        print_label(
            args.title,
            LabelLayout(
                look=args.look,
                note_size=args.note_size,
                note_align=args.note_align,
                title_size=args.title_size,
                caption_size=args.caption_size,
                show_caption=show_caption,
                show_footer=show_footer,
            ),
        )
    except Exception as exc:
        _log_exception("main", exc)
        try:
            messagebox.showerror(
                "Label printer failed",
                f"{type(exc).__name__}: {exc}\n\nDetails written to:\n{_ERROR_LOG_PATH}",
            )
        except Exception:
            print(f"Label printer failed: {exc}", file=sys.stderr)
            print(f"Details: {_ERROR_LOG_PATH}", file=sys.stderr)
        raise
