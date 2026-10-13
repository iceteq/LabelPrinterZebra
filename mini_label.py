import argparse
import io
import json
import re
import socket
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _CONFIG_DIR / "printer_config.json"
_EXAMPLE_NAME = "printer_config.example.json"
_BARCODE_ESCAPES_PATH = _CONFIG_DIR / "barcode_escapes.json"
_BARCODE_ESCAPES_EXAMPLE = _CONFIG_DIR / "barcode_escapes.example.json"
_DEFAULT_PRINTER_PORT = 9100


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
# Note-only sizes — fit-to-box shrinks when content needs it.
_NOTE_SIZE = {
    "small": (52, 46),
    "medium": (72, 64),
    "large": (96, 84),
    "xl": (124, 108),
    "xxl": (156, 136),
}
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
    note_size: str = "medium"
    note_align: str = _DEFAULT_NOTE_ALIGN

    def __post_init__(self) -> None:
        object.__setattr__(self, "look", _normalize_look(self.look))
        object.__setattr__(self, "note_size", _normalize_size(self.note_size))
        object.__setattr__(self, "note_align", _normalize_note_align(self.note_align))

    @property
    def recipe(self) -> LookRecipe:
        # Note is UI/mode only; barcode recipes fall back to Quiet if needed.
        if self.look == _NOTE_LOOK:
            return _LOOKS[_DEFAULT_LOOK]
        return _LOOKS[self.look]


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


def _estimate_wrapped_lines(text: str, font_h: int, block_w: float) -> int:
    words = text.split()
    if not words:
        return 1
    lines = 1
    used = 0.0
    space = _char_width_dots(" ", font_h)
    for word in words:
        ww = _text_width_dots(word, font_h)
        if ww > block_w:
            # Very long token: rough char-wrap estimate.
            lines += max(1, int(ww // max(1.0, block_w)))
            used = ww % max(1.0, block_w)
            continue
        if used == 0:
            used = ww
        elif used + space + ww <= block_w:
            used += space + ww
        else:
            lines += 1
            used = ww
    return max(1, lines)


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
) -> str:
    zpl_align = _H_ALIGN[h_align]
    return (
        f"^FO{x},{y}^FB{block_w},{max_lines},0,{zpl_align},0"
        f"^A0N,{font_h},{font_w}^FD{_zpl_field(text)}^FS"
    )


def _barcode_zpl(x: int, y: int, payload: str, height: int, module: int) -> str:
    encoded = _zpl_fh_payload(payload)
    return (
        f"^FO{x},{y}^BY{module},3,{height}"
        f"^FH^BCN,{height},N,N,N^FD{encoded}^FS"
    )


def _stamp_reserve() -> int:
    return _STAMP_FONT_H + 14


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


def _content_bottom(margin: int) -> int:
    return _LABEL_HEIGHT - margin - _stamp_reserve()


def _build_note_zpl(title: str, layout: LabelLayout, *, copies: int) -> BuildResult:
    prefer_h, prefer_w = _NOTE_SIZE[layout.note_size]
    h_align, v_align = _NOTE_ALIGN[layout.note_align]
    margin = _adaptive_margin(prefer_h >= 72)
    bottom = _content_bottom(margin)
    block_w = _LABEL_WIDTH - 2 * margin
    available = max(prefer_h, bottom - margin)
    # Provisional line budget at preferred size, then refit after shrink.
    max_lines = max(1, min(_TEXT_ONLY_MAX_LINES, available // max(1, prefer_h + 4)))
    fitted = _fit_note_text(title, prefer_h, prefer_w, float(block_w), max_lines)
    max_lines = max(
        1, min(_TEXT_ONLY_MAX_LINES, available // max(1, fitted.font_h + 4))
    )
    # If shrink unlocked more lines, try once more at the fitted size.
    fitted = _fit_note_text(
        title, prefer_h, prefer_w, float(block_w), max_lines
    )
    est_lines = min(
        max_lines,
        _estimate_wrapped_lines(fitted.text, fitted.font_h, float(block_w)),
    )
    block_h = est_lines * (fitted.font_h + 4) - 4
    content_h = max(0, bottom - margin)
    if v_align == "top":
        y = margin
    elif v_align == "bottom":
        y = max(margin, bottom - block_h)
    else:
        y = margin + max(0, (content_h - block_h) // 2)
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
            max_lines=max_lines,
        )
        + _signature_zpl(margin)
        + f"^PQ{copies}"
        + "^XZ"
    )
    return BuildResult(zpl, tuple(warnings))


def _build_side_zpl(
    title: str,
    serial: str,
    recipe: LookRecipe,
    *,
    copies: int,
) -> BuildResult:
    payload = _decode_barcode_input(serial)
    margin = 40
    gap = 10
    warnings: list[str] = []
    raw_title = " ".join(title.split()) if title.strip() else ""
    title_h, title_w = recipe.title_h, recipe.title_w
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
                raw_title, recipe.title_h, recipe.title_w, float(title_col), _MIN_TITLE_H
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

    bar_area_x = margin + (title_col + gap if raw_title else 0)
    bar_max_w = _LABEL_WIDTH - bar_area_x - margin
    quiet = 12
    module = _fit_module(
        payload, recipe.module_pref, max(40, bar_max_w - quiet), recipe.bar_fill
    )
    warnings.extend(_barcode_warnings(module, recipe.module_pref))
    bar_h = recipe.bar_h
    caption_pref_h, caption_pref_w = recipe.caption_h, recipe.caption_w
    gap_bc = max(5, int(caption_pref_h * 0.12))
    bottom = _content_bottom(margin)
    block_h = bar_h + gap_bc + caption_pref_h
    if margin + block_h > bottom:
        bar_h = max(_MIN_BAR_H, bar_h - (margin + block_h - bottom))
    y0 = margin + max(0, (bottom - margin - (bar_h + gap_bc + caption_pref_h)) // 2)
    # Recompute vertical center with fitted caption height after fit.
    bar_w = _estimate_barcode_width(payload, module)
    bar_x = bar_area_x
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
    block_h = bar_h + gap_bc + fitted_caption.font_h
    y0 = margin + max(0, (bottom - margin - block_h) // 2)
    title_y = y0 + max(0, (bar_h - fitted_title.font_h) // 2)

    parts = [_zpl_header()]
    if raw_title and fitted_title.text:
        parts.append(
            _text_zpl(
                margin,
                title_y,
                fitted_title.text,
                fitted_title.font_h,
                fitted_title.font_w,
                "left",
                title_col,
            )
        )
    parts.append(_barcode_zpl(bar_x, y0, payload, bar_h, module))
    parts.append(
        _text_zpl(
            bar_x,
            y0 + bar_h + gap_bc,
            fitted_caption.text,
            fitted_caption.font_h,
            fitted_caption.font_w,
            "left",
            max(bar_w, bar_max_w),
        )
    )
    parts.append(_signature_zpl(margin))
    parts.append(f"^PQ{copies}")
    parts.append("^XZ")
    return BuildResult("".join(parts), tuple(dict.fromkeys(warnings)))


def _build_stack_zpl(
    title: str,
    serial: str,
    recipe: LookRecipe,
    *,
    copies: int,
) -> BuildResult:
    payload = _decode_barcode_input(serial)
    warnings: list[str] = []
    has_title = bool(title.strip())
    caption_pref_h, caption_pref_w = recipe.caption_h, recipe.caption_w
    dense = (recipe.title_h + recipe.bar_h + caption_pref_h) > 220
    margin = _adaptive_margin(dense)
    bottom = _content_bottom(margin)
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
            recipe.title_h,
            recipe.title_w,
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
    if recipe.valign == "middle":
        y = margin + max(0, (bottom - margin - block_h) // 2)
    else:
        y = margin + 4
    align = recipe.align
    parts = [_zpl_header()]
    if has_title:
        parts.append(
            _text_zpl(
                margin,
                y,
                fitted_title.text,
                fitted_title.font_h,
                fitted_title.font_w,
                align,
                block_w,
            )
        )
        y += title_h + gap_tb
    bar_w = _estimate_barcode_width(payload, module)
    if align == "left":
        bar_x = margin
    elif align == "right":
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
            align,
            block_w,
        )
    )
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
        return _build_side_zpl(title, serial.strip(), recipe, copies=quantity)
    return _build_stack_zpl(title, serial.strip(), recipe, copies=quantity)


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
_PREVIEW_MAX_WIDTH = 520
_PREVIEW_MAX_HEIGHT = 220
_BUTTON_GAP = 14
_STATUS_MAX_CHARS = 72
_SUCCESS_HOLD_MS = 1100
_FADE_STEPS = 10
_FADE_MS = 18

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
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    ui = _pick_ui_font()
    mono = _pick_mono_font()
    return ui, mono


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
    png_data: bytes, max_width: int = _PREVIEW_MAX_WIDTH
) -> ctk.CTkImage:
    """Scale the label and seat it on a soft shadowed stage (figure–ground)."""
    label = Image.open(io.BytesIO(png_data)).convert("RGBA")
    scale = min(1.0, max_width / max(1, label.width))
    if scale < 1.0:
        label = label.resize(
            (max(1, int(label.width * scale)), max(1, int(label.height * scale))),
            Image.Resampling.LANCZOS,
        )

    pad_x, pad_y, shadow = 18, 16, 10
    stage_w = label.width + pad_x * 2 + shadow
    stage_h = label.height + pad_y * 2 + shadow
    stage = Image.new("RGBA", (stage_w, stage_h), (0, 0, 0, 0))

    # Soft contact shadow — depth without glow or heavy chrome.
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

    return ctk.CTkImage(
        light_image=stage,
        dark_image=stage,
        size=(stage_w, stage_h),
    )


def _fade_in(window: tk.Misc, *, steps: int = _FADE_STEPS, ms: int = _FADE_MS) -> None:
    """Gentle appear — reduces startle when the dialog pops over work."""
    try:
        window.attributes("-alpha", 0.0)
    except tk.TclError:
        return

    def step(i: int = 0) -> None:
        try:
            window.attributes("-alpha", min(1.0, (i + 1) / steps))
        except tk.TclError:
            return
        if i + 1 < steps:
            window.after(ms, lambda: step(i + 1))

    window.after(ms, step)


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


def _center_window(
    window: tk.Misc,
    *,
    width: int | None = None,
    height: int | None = None,
    min_width: int = 0,
) -> None:
    window.update_idletasks()
    width = max(width or window.winfo_reqwidth(), min_width)
    height = height or window.winfo_reqheight()
    area_x, area_y, area_w, area_h = _work_area_origin_and_size(window)
    width = min(width, max(320, area_w - 24))
    height = min(height, max(480, area_h - 24))
    x = area_x + max(0, (area_w - width) // 2)
    y = area_y + max(0, (area_h - height) // 2)
    # CTk.geometry() re-applies window scaling; winfo_* is already in pixels.
    tk.Wm.geometry(window, f"{width}x{height}+{x}+{y}")
    try:
        window.deiconify()
    except tk.TclError:
        pass


def _parse_args():
    parser = argparse.ArgumentParser(description="Print a barcode label on a Zebra printer.")
    parser.add_argument(
        "title",
        nargs="?",
        default="",
        help="Default text for the label title field (e.g. Act, Serienummer, Req)",
    )
    parser.add_argument(
        "--look",
        choices=[*_LOOK_ORDER, _NOTE_LOOK],
        default=_DEFAULT_LOOK,
        help="Label look / visual recipe",
    )
    parser.add_argument(
        "--note-size",
        choices=_SIZE_STEPS,
        default="medium",
        help="Note text size when printing without a barcode",
    )
    parser.add_argument(
        "--note-align",
        choices=_NOTE_ALIGN_STEPS,
        default=_DEFAULT_NOTE_ALIGN,
        help="Note text alignment when printing without a barcode",
    )
    return parser.parse_args()


def _add_size_stepper(
    parent: ttk.Frame,
    *,
    row: int,
    label: str,
    size_var: tk.StringVar,
    on_change,
) -> tuple[ttk.Frame, ttk.Label]:
    display_var = tk.StringVar(value=_SIZE_LABELS[_normalize_size(size_var.get())])

    def refresh_display(*_args: object) -> None:
        display_var.set(_SIZE_LABELS[_normalize_size(size_var.get())])

    size_var.trace_add("write", refresh_display)

    name_label = ttk.Label(parent, text=label)
    name_label.grid(row=row, column=0, sticky="w", pady=(6 * _UI_SCALE, 0))
    stepper = ttk.Frame(parent)
    stepper.grid(row=row, column=1, sticky="w", pady=(6 * _UI_SCALE, 0))

    def bump(delta: int) -> None:
        size_var.set(_bump_size(size_var.get(), delta))
        on_change()

    ttk.Button(stepper, text="−", width=3, command=lambda: bump(-1)).pack(side=tk.LEFT)
    ttk.Label(stepper, textvariable=display_var, width=8, anchor="center").pack(
        side=tk.LEFT, padx=(4, 4)
    )
    ttk.Button(stepper, text="+", width=3, command=lambda: bump(1)).pack(side=tk.LEFT)
    return stepper, name_label


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


def _open_printer_manager(parent: tk.Tk, on_changed) -> None:
    """Modal dialog to add / edit / remove named printers in printer_config.json."""
    store = _load_printer_store()
    t = _THEME
    ui_font, _mono = _apply_app_theme()

    win = ctk.CTkToplevel(parent)
    win.title("Manage printers")
    win.transient(parent)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.configure(fg_color=t["bg"])

    body = ctk.CTkFrame(win, fg_color=t["surface"], corner_radius=14, border_width=1, border_color=t["line"])
    body.grid(row=0, column=0, padx=18, pady=18, sticky="nsew")
    body.grid_columnconfigure(1, weight=1)

    ctk.CTkLabel(
        body,
        text="Printers",
        font=ctk.CTkFont(family=ui_font, size=22, weight="bold"),
        text_color=t["ink"],
    ).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(16, 2))
    ctk.CTkLabel(
        body,
        text="Saved on this computer only",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
    ).grid(row=1, column=0, columnspan=2, sticky="w", padx=18, pady=(0, 10))

    list_frame = ctk.CTkFrame(body, fg_color=t["bg"], corner_radius=10)
    list_frame.grid(row=2, column=0, columnspan=2, sticky="we", padx=18, pady=(0, 12))
    listbox = tk.Listbox(
        list_frame,
        height=6,
        width=36,
        exportselection=False,
        activestyle="dotbox",
        background=t["bg"],
        foreground=t["ink"],
        selectbackground=t["accent"],
        selectforeground=t["white"],
        highlightthickness=0,
        borderwidth=0,
        font=(ui_font, 11),
    )
    listbox.pack(fill="both", expand=True, padx=8, pady=8)

    def field_label(row: int, text: str) -> None:
        ctk.CTkLabel(
            body,
            text=text,
            font=ctk.CTkFont(family=ui_font, size=14),
            text_color=t["muted"],
        ).grid(row=row, column=0, sticky="w", padx=(18, 10), pady=5)

    name_var = tk.StringVar()
    host_var = tk.StringVar()
    port_var = tk.StringVar(value=str(_DEFAULT_PRINTER_PORT))
    status_var = tk.StringVar(value="")

    field_label(3, "Name")
    name_entry = ctk.CTkEntry(
        body,
        textvariable=name_var,
        width=260,
        height=40,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=15),
    )
    name_entry.grid(row=3, column=1, sticky="we", padx=(0, 18), pady=5)

    field_label(4, "IP / host")
    host_entry = ctk.CTkEntry(
        body,
        textvariable=host_var,
        width=260,
        height=40,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=15),
    )
    host_entry.grid(row=4, column=1, sticky="we", padx=(0, 18), pady=5)

    field_label(5, "Port")
    port_entry = ctk.CTkEntry(
        body,
        textvariable=port_var,
        width=100,
        height=40,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=15),
    )
    port_entry.grid(row=5, column=1, sticky="w", padx=(0, 18), pady=5)

    status_label = ctk.CTkLabel(
        body,
        textvariable=status_var,
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
        anchor="w",
    )
    status_label.grid(row=6, column=0, columnspan=2, sticky="we", padx=18, pady=(8, 0))

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
        name_entry.focus_force()

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

    buttons = ctk.CTkFrame(body, fg_color="transparent")
    buttons.grid(row=7, column=0, columnspan=2, sticky="we", padx=18, pady=(14, 16))
    btn_kwargs = {
        "height": 32,
        "corner_radius": 8,
        "font": ctk.CTkFont(family=ui_font, size=14),
    }
    ctk.CTkButton(
        buttons,
        text="Clear",
        command=on_new,
        fg_color=t["bg"],
        hover_color=t["line"],
        text_color=t["ink"],
        width=70,
        **btn_kwargs,
    ).pack(side=tk.LEFT)
    ctk.CTkButton(
        buttons,
        text="Save",
        command=on_save,
        fg_color=t["accent"],
        hover_color=t["accent_hover"],
        text_color=t["white"],
        width=70,
        **btn_kwargs,
    ).pack(side=tk.LEFT, padx=(8, 0))
    ctk.CTkButton(
        buttons,
        text="Delete",
        command=on_delete,
        fg_color=t["bg"],
        hover_color=t["line"],
        text_color=t["danger"],
        width=70,
        **btn_kwargs,
    ).pack(side=tk.LEFT, padx=(8, 0))
    ctk.CTkButton(
        buttons,
        text="Use",
        command=on_use,
        fg_color=t["accent_soft"],
        hover_color=t["line"],
        text_color=t["accent"],
        width=70,
        **btn_kwargs,
    ).pack(side=tk.LEFT, padx=(8, 0))
    ctk.CTkButton(
        buttons,
        text="Done",
        command=on_close,
        fg_color=t["ink"],
        hover_color="#132029",
        text_color=t["white"],
        width=80,
        **btn_kwargs,
    ).pack(side=tk.RIGHT)

    listbox.bind("<<ListboxSelect>>", load_selected)
    listbox.bind("<Double-Button-1>", lambda _e: on_use())
    refresh_list()
    if not store.printers:
        status_var.set("No printers yet — fill the fields and click Save")
        name_entry.focus_force()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.update_idletasks()
    _center_window(win)
    _fade_in(win)
    win.grab_set()
    parent.wait_window(win)


def _ask_label_fields(default_title: str = "", layout: LabelLayout | None = None):
    """Calm everyday UI: content → preview → print; extras under More."""
    layout = layout or LabelLayout()
    t = _THEME

    root = ctk.CTk()
    root.withdraw()
    ui_font, mono_font = _apply_app_theme()
    # CTk has its own DPI handling; keep a mild bump instead of 2× tk scaling.
    try:
        ctk.set_widget_scaling(1.15 if _UI_SCALE >= 2 else 1.0)
        ctk.set_window_scaling(1.15 if _UI_SCALE >= 2 else 1.0)
    except Exception:
        pass
    root.title("Print label")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.configure(fg_color=t["bg"])

    dialog_width = 600

    outer = ctk.CTkFrame(root, fg_color=t["bg"], corner_radius=0, width=dialog_width)
    outer.grid(row=0, column=0, sticky="nsew")
    outer.grid_columnconfigure(0, weight=1)
    outer.grid_propagate(True)

    # Soft surface card — one composition, not a dashboard of chrome.
    card = ctk.CTkFrame(
        outer,
        fg_color=t["surface"],
        corner_radius=16,
        border_width=1,
        border_color=t["line"],
        width=dialog_width - 32,
    )
    card.grid(row=0, column=0, padx=16, pady=16, sticky="nsew")
    card.grid_columnconfigure(0, weight=1)

    # Brand-first whisper: product name leads; job is secondary.
    ctk.CTkLabel(
        card,
        text="Label",
        font=ctk.CTkFont(family=ui_font, size=26, weight="bold"),
        text_color=t["ink"],
        anchor="w",
    ).grid(row=0, column=0, sticky="w", padx=22, pady=(18, 0))
    ctk.CTkLabel(
        card,
        text="What you see is what prints",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
        anchor="w",
    ).grid(row=1, column=0, sticky="w", padx=22, pady=(2, 14))

    printer_store = {"value": _load_printer_store()}
    auto_look = {"busy": False}
    preview_state = {
        "photo": None,
        "request_id": 0,
        "after_id": None,
        "closed": False,
        "printed": 0,
        "pulse_after": None,
    }

    content = ctk.CTkFrame(card, fg_color="transparent")
    content.grid(row=2, column=0, sticky="we", padx=22)
    content.grid_columnconfigure(0, weight=1)

    ctk.CTkLabel(
        content,
        text="Title",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
        anchor="w",
    ).grid(row=0, column=0, sticky="w")
    title_var = tk.StringVar(value=default_title)
    field_width = dialog_width - 76
    title_entry = ctk.CTkEntry(
        content,
        textvariable=title_var,
        width=field_width,
        height=44,
        corner_radius=10,
        border_width=1,
        border_color=t["line"],
        fg_color=t["white"],
        text_color=t["ink"],
        placeholder_text="Act, Req, Serienummer…",
        font=ctk.CTkFont(family=ui_font, size=17),
    )
    title_entry.grid(row=1, column=0, sticky="we", pady=(4, 12))

    ctk.CTkLabel(
        content,
        text="Barcode",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
        anchor="w",
    ).grid(row=2, column=0, sticky="w")
    serial_var = tk.StringVar()
    serial_entry = ctk.CTkEntry(
        content,
        textvariable=serial_var,
        width=field_width,
        height=44,
        corner_radius=10,
        border_width=1,
        border_color=t["line"],
        fg_color=t["white"],
        text_color=t["ink"],
        placeholder_text="Scan or type · empty = note",
        font=ctk.CTkFont(family=mono_font, size=17),
    )
    serial_entry.grid(row=3, column=0, sticky="we", pady=(4, 0))

    # Preview stage — figure–ground: cooler well makes the white label pop.
    stage = ctk.CTkFrame(
        card,
        fg_color=t["stage"],
        corner_radius=14,
        border_width=0,
    )
    stage.grid(row=3, column=0, sticky="we", padx=22, pady=(16, 0))
    stage.grid_columnconfigure(0, weight=1)
    stage.grid_rowconfigure(0, minsize=_PREVIEW_MAX_HEIGHT + 36)
    preview_label = ctk.CTkLabel(
        stage,
        text="Preparing preview…",
        font=ctk.CTkFont(family=ui_font, size=15),
        text_color=t["muted"],
        anchor="center",
    )
    preview_label.grid(row=0, column=0, sticky="nsew", padx=12, pady=14)

    status_var = tk.StringVar(value="")
    status_label = ctk.CTkLabel(
        card,
        textvariable=status_var,
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["danger"],
        anchor="w",
    )
    status_label.grid(row=4, column=0, sticky="we", padx=22, pady=(8, 0))

    more_open = tk.BooleanVar(value=False)
    more_toggle = ctk.CTkButton(
        card,
        text="More options  ▾",
        command=lambda: more_open.set(not more_open.get()),
        fg_color="transparent",
        hover_color=t["bg"],
        text_color=t["muted"],
        height=34,
        anchor="w",
        font=ctk.CTkFont(family=ui_font, size=14),
    )
    more_toggle.grid(row=5, column=0, sticky="w", padx=18, pady=(8, 0))

    more = ctk.CTkFrame(card, fg_color=t["bg"], corner_radius=12)
    more.grid_columnconfigure(1, weight=1)

    if not default_title.strip() and layout.look in (_DEFAULT_LOOK, _NOTE_LOOK):
        initial_look_title = _NOTE_LOOK_TITLE
    else:
        initial_look_title = _look_title_for_id(layout.look)
    look_title_var = tk.StringVar(value=initial_look_title)
    note_size_var = tk.StringVar(value=layout.note_size)
    note_align_var = tk.StringVar(value=layout.note_align)
    # Always start at 1 — never carry over the last print quantity.
    copies_var = tk.StringVar(value="1")
    printer_var = tk.StringVar(value=printer_store["value"].selected)

    def more_label(row: int, text: str) -> None:
        ctk.CTkLabel(
            more,
            text=text,
            font=ctk.CTkFont(family=ui_font, size=14),
            text_color=t["muted"],
        ).grid(row=row, column=0, sticky="w", padx=(14, 12), pady=8)

    more_label(0, "Printer")
    printer_row = ctk.CTkFrame(more, fg_color="transparent")
    printer_row.grid(row=0, column=1, sticky="w", pady=8, padx=(0, 14))
    printer_combo = ctk.CTkComboBox(
        printer_row,
        variable=printer_var,
        values=printer_store["value"].names() or [""],
        width=180,
        height=36,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        button_color=t["ink"],
        button_hover_color="#132029",
        dropdown_fg_color=t["white"],
        dropdown_hover_color=t["bg"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=14),
        state="readonly",
    )
    printer_combo.pack(side=tk.LEFT)

    def refresh_printer_combo(store: PrinterStore | None = None) -> None:
        store = store or _load_printer_store()
        printer_store["value"] = store
        names = store.names()
        printer_combo.configure(values=names or [""])
        if store.selected and store.selected in names:
            printer_var.set(store.selected)
        elif names:
            printer_var.set(names[0])
        else:
            printer_var.set("")

    def on_printer_selected(choice: str) -> None:
        name = (choice or "").strip()
        store = printer_store["value"]
        if not name or name == store.selected or store.get(name) is None:
            return
        store.selected = name
        _save_printer_store(store)

    def open_manager() -> None:
        _open_printer_manager(root, refresh_printer_combo)

    ctk.CTkButton(
        printer_row,
        text="Manage…",
        command=open_manager,
        width=88,
        height=36,
        corner_radius=8,
        fg_color=t["surface"],
        hover_color=t["line"],
        text_color=t["ink"],
        border_width=1,
        border_color=t["line"],
        font=ctk.CTkFont(family=ui_font, size=14),
    ).pack(side=tk.LEFT, padx=(8, 0))
    printer_combo.configure(command=on_printer_selected)

    look_label = ctk.CTkLabel(
        more,
        text="Look",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
    )
    look_label.grid(row=1, column=0, sticky="w", padx=(14, 12), pady=8)
    look_combo = ctk.CTkComboBox(
        more,
        variable=look_title_var,
        values=_look_display_names(),
        width=160,
        height=36,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        button_color=t["ink"],
        button_hover_color="#132029",
        dropdown_fg_color=t["white"],
        dropdown_hover_color=t["bg"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=14),
        state="readonly",
        command=lambda _v: schedule_preview(),
    )
    look_combo.grid(row=1, column=1, sticky="w", pady=8)

    size_row = ctk.CTkFrame(more, fg_color="transparent")
    size_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=4)
    note_size_label = ctk.CTkLabel(
        size_row,
        text="Note size",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
    )
    note_size_label.pack(side=tk.LEFT)
    note_stepper = ctk.CTkFrame(size_row, fg_color="transparent")
    note_stepper.pack(side=tk.LEFT, padx=(10, 0))
    size_display = tk.StringVar(value=_SIZE_LABELS[_normalize_size(note_size_var.get())])

    def _bump_note(delta: int) -> None:
        note_size_var.set(_bump_size(note_size_var.get(), delta))
        size_display.set(_SIZE_LABELS[_normalize_size(note_size_var.get())])
        schedule_preview()

    def _on_note_size_write(*_a: object) -> None:
        size_display.set(_SIZE_LABELS[_normalize_size(note_size_var.get())])

    note_size_var.trace_add("write", _on_note_size_write)
    step_btn = {
        "width": 36,
        "height": 34,
        "corner_radius": 8,
        "fg_color": t["surface"],
        "hover_color": t["line"],
        "text_color": t["ink"],
        "border_width": 1,
        "border_color": t["line"],
        "font": ctk.CTkFont(family=ui_font, size=17),
    }
    ctk.CTkButton(note_stepper, text="−", command=lambda: _bump_note(-1), **step_btn).pack(
        side=tk.LEFT
    )
    ctk.CTkLabel(
        note_stepper,
        textvariable=size_display,
        width=84,
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["ink"],
    ).pack(side=tk.LEFT, padx=6)
    ctk.CTkButton(note_stepper, text="+", command=lambda: _bump_note(1), **step_btn).pack(
        side=tk.LEFT
    )

    align_row = ctk.CTkFrame(more, fg_color="transparent")
    align_row.grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 8))
    ctk.CTkLabel(
        align_row,
        text="Align",
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
    ).pack(side=tk.LEFT)
    align_btns: dict[str, ctk.CTkButton] = {}

    def _paint_align_buttons() -> None:
        current = _normalize_note_align(note_align_var.get())
        for key, btn in align_btns.items():
            if key == current:
                btn.configure(
                    fg_color=t["accent"],
                    hover_color=t["accent_hover"],
                    text_color=t["white"],
                    border_width=0,
                )
            else:
                btn.configure(
                    fg_color=t["surface"],
                    hover_color=t["line"],
                    text_color=t["ink"],
                    border_width=1,
                    border_color=t["line"],
                )

    def _set_note_align(value: str) -> None:
        note_align_var.set(_normalize_note_align(value))
        _paint_align_buttons()
        schedule_preview()

    align_host = ctk.CTkFrame(align_row, fg_color="transparent")
    align_host.pack(side=tk.LEFT, padx=(10, 0))
    for key in _NOTE_ALIGN_STEPS:
        btn = ctk.CTkButton(
            align_host,
            text=_NOTE_ALIGN_LABELS[key],
            width=72,
            height=34,
            corner_radius=8,
            font=ctk.CTkFont(family=ui_font, size=13),
            command=lambda k=key: _set_note_align(k),
        )
        btn.pack(side=tk.LEFT, padx=(0, 6))
        align_btns[key] = btn
    _paint_align_buttons()

    more_label(4, "Copies")
    copies_combo = ctk.CTkComboBox(
        more,
        variable=copies_var,
        values=[str(n) for n in range(1, 11)],
        width=80,
        height=36,
        corner_radius=8,
        border_color=t["line"],
        fg_color=t["white"],
        button_color=t["ink"],
        button_hover_color="#132029",
        dropdown_fg_color=t["white"],
        dropdown_hover_color=t["bg"],
        text_color=t["ink"],
        font=ctk.CTkFont(family=ui_font, size=14),
        state="readonly",
    )
    copies_combo.grid(row=4, column=1, sticky="w", pady=8)

    layout_metrics = {"base_h": None}

    def _fit_window() -> None:
        root.update_idletasks()
        card.update_idletasks()
        if not more_open.get():
            layout_metrics["base_h"] = max(card.winfo_reqheight() + 40, 720)
            needed_h = layout_metrics["base_h"]
        else:
            more.update_idletasks()
            base = layout_metrics["base_h"] or max(card.winfo_reqheight() + 40, 720)
            # More panel sits between toggle and actions — add its height explicitly.
            needed_h = base + max(more.winfo_reqheight(), 240) + 12
        needed_w = max(dialog_width, card.winfo_reqwidth() + 32)
        _center_window(root, width=needed_w, height=needed_h, min_width=dialog_width)

    def toggle_more(*_args: object) -> None:
        if more_open.get():
            more.grid(row=6, column=0, sticky="we", padx=22, pady=(4, 0))
            more_toggle.configure(text="More options  ▴")
        else:
            more.grid_remove()
            more_toggle.configure(text="More options  ▾")
        root.update_idletasks()
        _fit_window()
        root.after(40, _fit_window)

    more_open.trace_add("write", toggle_more)

    actions = ctk.CTkFrame(card, fg_color="transparent")
    actions.grid(row=7, column=0, sticky="we", padx=22, pady=(16, 18))
    actions.grid_columnconfigure(0, weight=1)
    session_var = tk.StringVar(value="")
    ctk.CTkLabel(
        actions,
        textvariable=session_var,
        font=ctk.CTkFont(family=ui_font, size=14),
        text_color=t["muted"],
        anchor="w",
    ).grid(row=0, column=0, sticky="w")
    btn_row = ctk.CTkFrame(actions, fg_color="transparent")
    btn_row.grid(row=0, column=1, sticky="e")
    cancel_button = ctk.CTkButton(
        btn_row,
        text="Close",
        command=lambda: on_cancel(),
        width=108,
        height=44,
        corner_radius=10,
        fg_color=t["bg"],
        hover_color=t["line"],
        text_color=t["ink"],
        border_width=1,
        border_color=t["line"],
        font=ctk.CTkFont(family=ui_font, size=15),
    )
    cancel_button.pack(side=tk.LEFT)
    print_button = ctk.CTkButton(
        btn_row,
        text="Print  ↵",
        command=lambda: on_print(),
        width=132,
        height=44,
        corner_radius=10,
        fg_color=t["accent"],
        hover_color=t["accent_hover"],
        text_color=t["white"],
        font=ctk.CTkFont(family=ui_font, size=17, weight="bold"),
    )
    print_button.pack(side=tk.LEFT, padx=(10, 0))

    def current_layout() -> LabelLayout:
        return LabelLayout(
            look=_look_id_from_title(look_title_var.get()),
            note_size=note_size_var.get(),
            note_align=note_align_var.get(),
        )

    def current_copies() -> int:
        try:
            return max(1, min(10, int(copies_var.get())))
        except ValueError:
            return 1

    def reset_copies() -> None:
        copies_var.set("1")
        try:
            copies_combo.set("1")
        except tk.TclError:
            pass

    def set_look_title(title: str) -> None:
        if look_title_var.get() == title:
            return
        auto_look["busy"] = True
        try:
            look_title_var.set(title)
            look_combo.set(title)
        finally:
            auto_look["busy"] = False

    def sync_auto_look(*_args: object) -> None:
        """No barcode → Note (locked); barcode → Quiet when leaving Note."""
        if auto_look["busy"]:
            return
        has_barcode = bool(serial_var.get().strip())
        current = look_title_var.get()
        if not has_barcode:
            set_look_title(_NOTE_LOOK_TITLE)
        elif current == _NOTE_LOOK_TITLE:
            set_look_title(_LOOKS[_DEFAULT_LOOK].title)

    def update_mode_chrome(*_args: object) -> None:
        has_barcode = bool(serial_var.get().strip())
        if has_barcode:
            size_row.grid_remove()
            align_row.grid_remove()
            look_combo.configure(state="readonly")
            look_label.configure(text_color=t["muted"])
        else:
            size_row.grid()
            align_row.grid()
            set_look_title(_NOTE_LOOK_TITLE)
            look_combo.configure(state="disabled")
            look_label.configure(text_color=t["line"])

    def set_status(kind: str, message: str) -> None:
        colors = {
            "ok": t["ok"],
            "warn": t["warn"],
            "error": t["danger"],
            "info": t["muted"],
        }
        status_label.configure(text_color=colors.get(kind, t["muted"]))
        status_var.set(_status_line(message) if message else "")

    def pulse_print_success() -> None:
        """Brief CTA confirmation — reward without fireworks."""
        print_button.configure(fg_color=t["ok"])
        after_id = preview_state.get("pulse_after")
        if after_id is not None:
            root.after_cancel(after_id)

        def restore() -> None:
            print_button.configure(fg_color=t["accent"])
            preview_state["pulse_after"] = None

        preview_state["pulse_after"] = root.after(420, restore)

    def apply_preview(
        request_id: int,
        png_data: bytes | None,
        error: str | None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        if preview_state["closed"] or request_id != preview_state["request_id"]:
            return
        if png_data is None:
            preview_state["photo"] = None
            preview_label.configure(image=None, text=error or "Preview unavailable")
            set_status("error", error or "Preview unavailable")
            return
        photo = _png_to_preview_image(png_data)
        preview_state["photo"] = photo
        preview_label.configure(image=photo, text="")
        if warnings:
            set_status("warn", " · ".join(warnings))
        elif not status_var.get().startswith("Sent"):
            set_status("info", "")

    def refresh_preview() -> None:
        request_id = preview_state["request_id"] + 1
        preview_state["request_id"] = request_id
        built = _preview_label(title_var.get(), serial_var.get(), current_layout())
        zpl = built.zpl
        warnings = built.warnings

        def fetch() -> None:
            try:
                png_data = _fetch_labelary_png(zpl)
                error = None
            except RuntimeError as exc:
                png_data = None
                error = str(exc)
            if not preview_state["closed"]:
                root.after(
                    0,
                    lambda: apply_preview(request_id, png_data, error, warnings),
                )

        threading.Thread(target=fetch, daemon=True).start()

    def schedule_preview(*_args: object) -> None:
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        preview_state["after_id"] = root.after(_PREVIEW_DEBOUNCE_MS, refresh_preview)

    def set_printing(enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        print_button.configure(state=state)
        cancel_button.configure(state=state)
        title_entry.configure(state=state)
        serial_entry.configure(state=state)

    def focus_barcode() -> None:
        serial_entry.focus_force()
        try:
            serial_entry.select_range(0, tk.END)
        except tk.TclError:
            pass

    def after_success(printer_name: str) -> None:
        preview_state["printed"] += 1
        n = preview_state["printed"]
        session_var.set(f"{n} printed" if n > 1 else "1 printed")
        set_status("ok", f"Sent to {printer_name} ✓")
        pulse_print_success()
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
            title_entry.focus_force()
            return

        built = _compose_label(
            title, serial, current_layout(), copies=current_copies()
        )
        zpl = built.zpl

        set_printing(False)
        set_status("info", "Sending…")
        root.update_idletasks()

        try:
            store = _load_printer_store()
            printer = store.selected_printer()
            if printer is None:
                raise FileNotFoundError(
                    "No printer configured. Open More options → Manage…"
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
        pulse = preview_state.get("pulse_after")
        if pulse is not None:
            root.after_cancel(pulse)
        root.destroy()

    title_var.trace_add("write", schedule_preview)
    title_var.trace_add("write", sync_auto_look)
    serial_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", sync_auto_look)
    serial_var.trace_add("write", update_mode_chrome)
    look_title_var.trace_add("write", schedule_preview)
    look_title_var.trace_add("write", update_mode_chrome)
    note_size_var.trace_add("write", schedule_preview)
    note_align_var.trace_add("write", schedule_preview)
    sync_auto_look()
    update_mode_chrome()

    for sequence in ("<Return>", "<KP_Enter>"):
        root.bind(sequence, lambda _event: on_print())
        serial_entry.bind(sequence, lambda _event: on_print())
        title_entry.bind(sequence, lambda _event: on_print())
    # Delay Escape so a leftover key event from the host cannot close on open.
    root.after(400, lambda: root.bind("<Escape>", lambda _event: on_cancel()))
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def focus_initial_entry() -> None:
        root.lift()
        root.attributes("-topmost", True)
        if default_title.strip():
            focus_barcode()
        else:
            title_entry.focus_force()

    _fit_window()
    _fade_in(root)
    schedule_preview()
    root.after(0, focus_initial_entry)
    root.after(120, focus_initial_entry)
    root.mainloop()


def print_label(default_title: str = "", layout: LabelLayout | None = None):
    _ask_label_fields(default_title, layout)


if __name__ == "__main__":
    args = _parse_args()
    print_label(
        args.title,
        LabelLayout(
            look=args.look,
            note_size=args.note_size,
            note_align=args.note_align,
        ),
    )
