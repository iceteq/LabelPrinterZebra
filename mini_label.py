import argparse
import base64
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
from tkinter import messagebox, ttk

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _CONFIG_DIR / "printer_config.json"
_EXAMPLE_NAME = "printer_config.example.json"
_BARCODE_ESCAPES_PATH = _CONFIG_DIR / "barcode_escapes.json"
_BARCODE_ESCAPES_EXAMPLE = _CONFIG_DIR / "barcode_escapes.example.json"


def _load_printer_config():
    if not _CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Missing {_CONFIG_PATH.name}. Copy {_EXAMPLE_NAME} to {_CONFIG_PATH.name} "
            f"and set your printer's host and port. See README.md."
        )
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return str(data["host"]), int(data["port"])


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
_SIZE_STEPS = ("small", "medium", "large")
_SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large"}
_NOTE_SIZE = {
    "small": (32, 30),
    "medium": (44, 40),
    "large": (58, 52),
}


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
_LOOKS: dict[str, LookRecipe] = {
    "scan": LookRecipe(
        look_id="scan",
        title="Scan",
        blurb="Everyday — tall bars, quiet title",
        align="left",
        valign="top",
        stack="vertical",
        title_h=28,
        title_w=26,
        bar_h=158,
        module_pref=3,
        caption_h=26,
        caption_w=22,
        bar_fill=0.62,
    ),
    "read_id": LookRecipe(
        look_id="read_id",
        title="Read ID",
        blurb="Large caption for act / part numbers",
        align="left",
        valign="top",
        stack="vertical",
        title_h=26,
        title_w=24,
        bar_h=108,
        module_pref=3,
        caption_h=42,
        caption_w=34,
        bar_fill=0.55,
    ),
    "label": LookRecipe(
        look_id="label",
        title="Label",
        blurb="Centered mark — title leads",
        align="center",
        valign="middle",
        stack="vertical",
        title_h=60,
        title_w=52,
        bar_h=132,
        module_pref=3,
        caption_h=18,
        caption_w=14,
        bar_fill=0.58,
    ),
    "quiet": LookRecipe(
        look_id="quiet",
        title="Quiet",
        blurb="Longer values, calmer type",
        align="left",
        valign="top",
        stack="vertical",
        title_h=36,
        title_w=32,
        bar_h=112,
        module_pref=2,
        caption_h=18,
        caption_w=14,
        bar_fill=0.70,
    ),
    "side": LookRecipe(
        look_id="side",
        title="Side title",
        blurb="Title beside the barcode",
        align="left",
        valign="middle",
        stack="side",
        title_h=38,
        title_w=34,
        bar_h=128,
        module_pref=3,
        caption_h=24,
        caption_w=20,
        bar_fill=0.55,
    ),
}

_LOOK_ORDER = ("scan", "read_id", "label", "quiet", "side")
_DEFAULT_LOOK = "scan"


def _normalize_look(value: str) -> str:
    key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "readid": "read_id",
        "read": "read_id",
        "side_title": "side",
        "sidetitle": "side",
        "default": "scan",
    }
    key = aliases.get(key, key)
    if key not in _LOOKS:
        raise ValueError(f"look must be one of {list(_LOOK_ORDER)}")
    return key


def _normalize_size(value: str) -> str:
    key = str(value).strip().lower()
    if key not in _SIZE_STEPS:
        raise ValueError(f"size must be one of {list(_SIZE_STEPS)}")
    return key


def _bump_size(current: str, delta: int) -> str:
    idx = _SIZE_STEPS.index(_normalize_size(current))
    return _SIZE_STEPS[max(0, min(len(_SIZE_STEPS) - 1, idx + delta))]


@dataclass(frozen=True)
class LabelLayout:
    look: str = _DEFAULT_LOOK
    note_size: str = "medium"

    def __post_init__(self) -> None:
        object.__setattr__(self, "look", _normalize_look(self.look))
        object.__setattr__(self, "note_size", _normalize_size(self.note_size))

    @property
    def recipe(self) -> LookRecipe:
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


_MIN_TITLE_H = 18
_MIN_CAPTION_H = 14
_MIN_NOTE_H = 20
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
    margin = _adaptive_margin(prefer_h >= 50)
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
    block_h = est_lines * fitted.font_h
    if est_lines <= 2:
        y = margin + max(0, (bottom - margin - block_h) // 2)
        align = "center"
    else:
        y = margin
        align = "left"
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
            align,
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
    recipe = layout.recipe
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
_PREVIEW_MAX_WIDTH = 420
_PREVIEW_MAX_HEIGHT = 170
_BUTTON_GAP = 8
_STATUS_MAX_CHARS = 72


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


def _png_to_photo(png_data: bytes, max_width: int = _PREVIEW_MAX_WIDTH) -> tk.PhotoImage:
    photo = tk.PhotoImage(data=base64.b64encode(png_data))
    if photo.width() > max_width:
        factor = max(2, (photo.width() + max_width - 1) // max_width)
        photo = photo.subsample(factor, factor)
    return photo


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


def _center_window(window: tk.Tk, *, height: int | None = None) -> None:
    window.update_idletasks()
    width = window.winfo_reqwidth()
    height = height or window.winfo_reqheight()
    area_x, area_y, area_w, area_h = _work_area_origin_and_size(window)
    x = area_x + max(0, (area_w - width) // 2)
    y = area_y + max(0, (area_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.deiconify()


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
        choices=_LOOK_ORDER,
        default=_DEFAULT_LOOK,
        help="Label look / visual recipe",
    )
    parser.add_argument(
        "--note-size",
        choices=_SIZE_STEPS,
        default="medium",
        help="Note text size when printing without a barcode",
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
    return [_LOOKS[k].title for k in _LOOK_ORDER]


def _look_id_from_title(title: str) -> str:
    for key in _LOOK_ORDER:
        if _LOOKS[key].title == title:
            return key
    return _DEFAULT_LOOK


def _ask_label_fields(default_title: str = "", layout: LabelLayout | None = None):
    layout = layout or LabelLayout()
    pad = 14 * _UI_SCALE

    root = tk.Tk()
    root.withdraw()
    root.title("Print label")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * _UI_SCALE)

    style = ttk.Style(root)
    try:
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", font=("Segoe UI", 8), foreground="#B00020")
        style.configure("Warn.TLabel", font=("Segoe UI", 8), foreground="#8A6116")
        style.configure("Ok.TLabel", font=("Segoe UI", 8), foreground="#1B5E20")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=pad)
    frame.grid(row=0, column=0)

    ttk.Label(frame, text="Title").grid(row=0, column=0, sticky="w", pady=(0, 4 * _UI_SCALE))
    title_var = tk.StringVar(value=default_title)
    title_entry = ttk.Entry(frame, textvariable=title_var, width=34)
    title_entry.grid(row=0, column=1, sticky="we", pady=(0, 4 * _UI_SCALE))

    ttk.Label(frame, text="Barcode").grid(row=1, column=0, sticky="w")
    serial_var = tk.StringVar()
    serial_entry = ttk.Entry(frame, textvariable=serial_var, width=34)
    serial_entry.grid(row=1, column=1, sticky="we")

    ttk.Label(frame, text="Look").grid(row=2, column=0, sticky="w", pady=(10 * _UI_SCALE, 0))
    look_title_var = tk.StringVar(value=layout.recipe.title)
    look_combo = ttk.Combobox(
        frame,
        textvariable=look_title_var,
        values=_look_display_names(),
        state="readonly",
        width=18,
    )
    look_combo.grid(row=2, column=1, sticky="w", pady=(10 * _UI_SCALE, 0))

    hint_var = tk.StringVar(value=layout.recipe.blurb)
    hint_label = ttk.Label(frame, textvariable=hint_var, style="Hint.TLabel")
    hint_label.grid(row=3, column=1, sticky="w", pady=(2, 0))

    note_size_var = tk.StringVar(value=layout.note_size)

    def schedule_preview_proxy(*_args: object) -> None:
        schedule_preview()

    note_stepper, note_size_label = _add_size_stepper(
        frame,
        row=4,
        label="Note size",
        size_var=note_size_var,
        on_change=schedule_preview_proxy,
    )

    copies_row = 5
    preview_row = 6
    ttk.Label(frame, text="Copies").grid(
        row=copies_row, column=0, sticky="w", pady=(8 * _UI_SCALE, 0)
    )
    copies_var = tk.StringVar(value="1")
    copies_combo = ttk.Combobox(
        frame,
        textvariable=copies_var,
        values=[str(n) for n in range(1, 11)],
        state="readonly",
        width=8,
    )
    copies_combo.grid(row=copies_row, column=1, sticky="w", pady=(8 * _UI_SCALE, 0))
    frame.grid_rowconfigure(
        preview_row,
        minsize=(20 * _UI_SCALE) + (8 * _UI_SCALE) * 2 + _PREVIEW_MAX_HEIGHT,
    )

    def current_layout() -> LabelLayout:
        return LabelLayout(
            look=_look_id_from_title(look_title_var.get()),
            note_size=note_size_var.get(),
        )

    def current_copies() -> int:
        try:
            return max(1, min(10, int(copies_var.get())))
        except ValueError:
            return 1

    def update_mode_chrome(*_args: object) -> None:
        has_barcode = bool(serial_var.get().strip())
        look_id = _look_id_from_title(look_title_var.get())
        hint_var.set(_LOOKS[look_id].blurb if has_barcode else "Note — text only, no barcode")
        # Note size only when there is no barcode.
        if has_barcode:
            note_size_label.grid_remove()
            note_stepper.grid_remove()
        else:
            note_size_label.grid()
            note_stepper.grid()

    preview_frame = ttk.LabelFrame(frame, text="Preview", padding=8 * _UI_SCALE)
    preview_frame.grid(
        row=preview_row, column=0, columnspan=2, sticky="we", pady=(pad, 0)
    )
    preview_frame.grid_rowconfigure(0, minsize=_PREVIEW_MAX_HEIGHT)
    preview_frame.grid_columnconfigure(0, weight=1)
    preview_label = ttk.Label(
        preview_frame,
        text="Loading preview…",
        anchor="center",
        justify="center",
    )
    preview_label.grid(row=0, column=0, sticky="n")

    preview_state = {"photo": None, "request_id": 0, "after_id": None, "closed": False}

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
            preview_label.configure(image="", text=error or "Preview unavailable")
            status_label.configure(style="Status.TLabel")
            status_var.set(_status_line(error or "Preview unavailable"))
            return
        photo = _png_to_photo(png_data)
        preview_state["photo"] = photo
        preview_label.configure(image=photo, text="")
        if warnings:
            status_label.configure(style="Warn.TLabel")
            status_var.set(_status_line(" · ".join(warnings)))
        else:
            status_var.set("")

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

    title_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", update_mode_chrome)
    look_title_var.trace_add("write", schedule_preview)
    look_title_var.trace_add("write", update_mode_chrome)
    look_combo.bind("<<ComboboxSelected>>", schedule_preview)
    note_size_var.trace_add("write", schedule_preview)
    update_mode_chrome()

    status_var = tk.StringVar(value="")
    status_label = ttk.Label(frame, textvariable=status_var, style="Status.TLabel")
    status_label.grid(
        row=preview_row + 1, column=0, columnspan=2, sticky="nw", pady=(4 * _UI_SCALE, 0)
    )

    def set_printing(enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        print_button.state(state)
        cancel_button.state(state)

    def on_print():
        title = title_var.get().strip()
        serial = serial_var.get().strip()
        if not serial and not title:
            messagebox.showwarning(
                "Missing value",
                "Enter text in Title or a barcode value to print.",
                parent=root,
            )
            title_entry.focus_force()
            return

        built = _compose_label(
            title, serial, current_layout(), copies=current_copies()
        )
        zpl = built.zpl

        set_printing(False)
        status_label.configure(style="Status.TLabel")
        status_var.set(_status_line("Sending to printer…"))
        root.update_idletasks()

        try:
            host, port = _load_printer_config()
            status_var.set(_status_line(f"Connecting to {host}:{port}…"))
            root.update_idletasks()
            _send_zpl(zpl)
        except FileNotFoundError as exc:
            status_var.set(_status_line(str(exc)))
            set_printing(True)
            serial_entry.focus_force()
            return
        except OSError as exc:
            try:
                host, port = _load_printer_config()
                target = f"{host}:{port}"
            except FileNotFoundError:
                target = "printer"
            status_var.set(_status_line(f"Could not connect to {target}: {exc}"))
            set_printing(True)
            serial_entry.focus_force()
            return
        except Exception as exc:
            status_var.set(_status_line(f"Print failed: {type(exc).__name__}: {exc}"))
            set_printing(True)
            serial_entry.focus_force()
            return

        preview_state["closed"] = True
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        root.destroy()

    def on_cancel():
        preview_state["closed"] = True
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(
        row=preview_row + 2, column=0, columnspan=2, sticky="w", pady=(_BUTTON_GAP, 0)
    )
    print_button = ttk.Button(buttons, text="Print", command=on_print)
    print_button.pack(side=tk.LEFT)
    cancel_button = ttk.Button(buttons, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=(8, 0))

    for sequence in ("<Return>", "<KP_Enter>"):
        root.bind(sequence, lambda _event: on_print())
        serial_entry.bind(sequence, lambda _event: on_print())
        title_entry.bind(sequence, lambda _event: on_print())
    root.bind("<Escape>", lambda _event: on_cancel())
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def focus_initial_entry():
        root.lift()
        root.attributes("-topmost", True)
        if default_title.strip():
            serial_entry.focus_force()
        else:
            title_entry.focus_force()

    _center_window(root)
    schedule_preview()
    root.after(0, focus_initial_entry)
    root.after(100, focus_initial_entry)
    root.mainloop()


def print_label(default_title: str = "", layout: LabelLayout | None = None):
    _ask_label_fields(default_title, layout)


if __name__ == "__main__":
    args = _parse_args()
    print_label(
        args.title,
        LabelLayout(look=args.look, note_size=args.note_size),
    )
