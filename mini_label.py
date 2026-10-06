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
_MARGIN = 50
_GAP_TITLE_BARCODE = 10
_GAP_BARCODE_CAPTION = 8
_TEXT_ONLY_MAX_LINES = 8
_AUTHOR = "Anton"
_STAMP_FONT_H = 18
_STAMP_FONT_W = 14
# Relative advance widths as fractions of title font height (calibrated so
# ABCDEFGHIJKLMNOPQRSTUVWXY ≈ fills the printable width).
_CHAR_WIDTH_SPACE = 0.28
_CHAR_WIDTH_NARROW = 0.35
_CHAR_WIDTH_LOWER = 0.48
_CHAR_WIDTH_UPPER = 0.57
_CHAR_WIDTH_WIDE = 0.85
_NARROW_CHARS = set("iltrfjI.:;,!|'\"`")
_WIDE_CHARS = set("mw@%")  # lowercase / symbols; A–Z use upper weight (incl. M, W)

_H_ALIGN = {"left": "L", "center": "C", "right": "R"}
_SIZE_STEPS = ("small", "medium", "large")
_SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large"}
# Title / note body font height & width (dots).
_TEXT_SIZE = {
    "small": (36, 36),
    "medium": (50, 50),
    "large": (70, 70),
}
# Barcode bar height and module width (dots).
_BARCODE_SIZE = {
    "small": (80, 2),
    "medium": (120, 3),
    "large": (160, 3),
}
# Human-readable caption under the barcode (dots).
_CAPTION_SIZE = {
    "small": (18, 14),
    "medium": (28, 22),
    "large": (40, 32),
}


def _label_dots(width_in: float, height_in: float, dpmm: int) -> tuple[int, int]:
    dots_per_in = dpmm * 25.4
    return round(width_in * dots_per_in), round(height_in * dots_per_in)


_LABEL_WIDTH, _LABEL_HEIGHT = _label_dots(
    _LABELARY_WIDTH_IN, _LABELARY_HEIGHT_IN, _LABELARY_DPMM
)


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
    align: str = "left"
    title_size: str = "medium"
    barcode_size: str = "medium"
    caption_size: str = "medium"

    def __post_init__(self) -> None:
        if self.align not in _H_ALIGN:
            raise ValueError(f"align must be one of {list(_H_ALIGN)}")
        object.__setattr__(self, "title_size", _normalize_size(self.title_size))
        object.__setattr__(self, "barcode_size", _normalize_size(self.barcode_size))
        object.__setattr__(self, "caption_size", _normalize_size(self.caption_size))

    @property
    def title_font(self) -> tuple[int, int]:
        return _TEXT_SIZE[self.title_size]

    @property
    def caption_font(self) -> tuple[int, int]:
        return _CAPTION_SIZE[self.caption_size]

    @property
    def barcode_metrics(self) -> tuple[int, int]:
        """Return (bar_height, module_width)."""
        return _BARCODE_SIZE[self.barcode_size]


def _text_zpl(
    y: int,
    text: str,
    font_h: int,
    font_w: int,
    h_align: str,
    *,
    max_lines: int = 1,
) -> str:
    block_w = _LABEL_WIDTH - 2 * _MARGIN
    zpl_align = _H_ALIGN[h_align]
    return (
        f"^FO{_MARGIN},{y}^FB{block_w},{max_lines},0,{zpl_align},0"
        f"^A0N,{font_h},{font_w}^FD{_zpl_field(text)}^FS"
    )


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


def _truncate_to_printable_width(text: str, font_h: int) -> str:
    """Fit one line using weighted glyph widths vs printable label width."""
    block_w = float(_LABEL_WIDTH - 2 * _MARGIN)
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


def _zpl_header() -> str:
    return (
        "^XA"
        "^CI28"
        f"^PW{_LABEL_WIDTH}"
        f"^LL{_LABEL_HEIGHT}"
    )


def _caption_display(serial: str) -> str:
    """Human-readable caption: keep typed text (act/part numbers, letters, escapes)."""
    return " ".join(serial.split())


def _estimate_barcode_width(serial: str, module: int) -> int:
    modules = (len(serial) + 3) * 11
    return modules * module


def _barcode_x(serial: str, h_align: str, module: int) -> int:
    width = _estimate_barcode_width(serial, module)
    block_w = _LABEL_WIDTH - 2 * _MARGIN
    if h_align == "left":
        return _MARGIN
    if h_align == "right":
        return max(_MARGIN, _LABEL_WIDTH - _MARGIN - width)
    return _MARGIN + max(0, (block_w - width) // 2)


def _barcode_zpl(serial: str, h_align: str, *, y: int, height: int, module: int) -> str:
    payload = _decode_barcode_input(serial)
    barcode_x = _barcode_x(payload, h_align, module)
    encoded = _zpl_fh_payload(payload)
    # N = no built-in interpretation line; caption is drawn separately for sizing.
    return (
        f"^FO{barcode_x},{y}^BY{module},3,{height}"
        f"^FH^BCN,{height},N,N,N^FD{encoded}^FS"
    )


def _signature_zpl() -> str:
    stamp_y = _LABEL_HEIGHT - _MARGIN - _STAMP_FONT_H
    text = f"{_AUTHOR} · {date.today():%Y-%m-%d}"
    block_w = _LABEL_WIDTH - 2 * _MARGIN
    return (
        f"^FO{_MARGIN},{stamp_y}"
        f"^FB{block_w},1,0,R,0"
        f"^A0N,{_STAMP_FONT_H},{_STAMP_FONT_W}"
        f"^FD{_zpl_field(text)}^FS"
    )


def _content_bottom_y() -> int:
    """Y just above the signature stamp."""
    return _LABEL_HEIGHT - _MARGIN - _STAMP_FONT_H - 6


def _note_max_lines(font_h: int) -> int:
    available = max(font_h, _content_bottom_y() - _MARGIN)
    return max(1, min(_TEXT_ONLY_MAX_LINES, available // font_h))


def _build_zpl(
    title: str,
    serial: str,
    layout: LabelLayout | None = None,
    *,
    copies: int = 1,
) -> str:
    layout = layout or LabelLayout()
    header = _zpl_header()
    signature = _signature_zpl()
    quantity = max(1, min(10, int(copies)))
    pq = f"^PQ{quantity}"
    title_h, title_w = layout.title_font

    if serial.strip():
        title_line = ""
        parts = [header]
        y = _MARGIN
        if title.strip():
            title_line = _truncate_to_printable_width(
                " ".join(title.split()), title_h
            )
            parts.append(
                _text_zpl(y, title_line, title_h, title_w, layout.align)
            )
            y += title_h + _GAP_TITLE_BARCODE

        bar_h, module = layout.barcode_metrics
        caption_h, caption_w = layout.caption_font
        caption_text = _truncate_to_printable_width(
            _caption_display(serial), caption_h
        )
        # Keep barcode + caption above the stamp when sizes are large.
        bottom = _content_bottom_y()
        needed = bar_h + _GAP_BARCODE_CAPTION + caption_h
        if y + needed > bottom:
            overflow = y + needed - bottom
            bar_h = max(40, bar_h - overflow)

        parts.append(
            _barcode_zpl(
                serial, layout.align, y=y, height=bar_h, module=module
            )
        )
        caption_y = y + bar_h + _GAP_BARCODE_CAPTION
        parts.append(
            _text_zpl(
                caption_y, caption_text, caption_h, caption_w, layout.align
            )
        )
        parts.extend([signature, pq, "^XZ"])
        return "".join(parts)

    return (
        header
        + _text_zpl(
            _MARGIN,
            title,
            title_h,
            title_w,
            layout.align,
            max_lines=_note_max_lines(title_h),
        )
        + signature
        + pq
        + "^XZ"
    )


_UI_SCALE = 2
_PREVIEW_DEBOUNCE_MS = 400
_PREVIEW_MAX_WIDTH = 360
_PREVIEW_MAX_HEIGHT = 140
# Gap between the status line and the button row.
_BUTTON_GAP = 6
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


def _preview_zpl(title: str, serial: str, layout: LabelLayout | None = None) -> str:
    return _build_zpl(title.strip(), serial.strip(), layout)


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
        "--align",
        choices=sorted(_H_ALIGN),
        default="left",
        help="Horizontal alignment for title and barcode",
    )
    parser.add_argument(
        "--title-size",
        choices=_SIZE_STEPS,
        default="medium",
        help="Title / note text size",
    )
    parser.add_argument(
        "--barcode-size",
        choices=_SIZE_STEPS,
        default="medium",
        help="Barcode bar height / module size",
    )
    parser.add_argument(
        "--caption-size",
        choices=_SIZE_STEPS,
        default="medium",
        help="Caption text size under the barcode",
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
    """Label + (−) size (+) row; size_var holds small|medium|large."""
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

    minus = ttk.Button(stepper, text="−", width=3, command=lambda: bump(-1))
    minus.pack(side=tk.LEFT)
    value = ttk.Label(stepper, textvariable=display_var, width=8, anchor="center")
    value.pack(side=tk.LEFT, padx=(4, 4))
    plus = ttk.Button(stepper, text="+", width=3, command=lambda: bump(1))
    plus.pack(side=tk.LEFT)
    return stepper, name_label


def _ask_label_fields(default_title: str = "", layout: LabelLayout | None = None):
    layout = layout or LabelLayout()
    pad = 12 * _UI_SCALE

    root = tk.Tk()
    root.withdraw()
    root.title("Print label")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * _UI_SCALE)

    frame = ttk.Frame(root, padding=pad)
    frame.grid(row=0, column=0)
    preview_row_height = (24 * _UI_SCALE) + (6 * _UI_SCALE) * 2 + _PREVIEW_MAX_HEIGHT
    preview_row = 7
    frame.grid_rowconfigure(preview_row, minsize=preview_row_height)

    ttk.Label(frame, text="Title:").grid(
        row=0, column=0, sticky="w", pady=(0, 6 * _UI_SCALE)
    )
    title_var = tk.StringVar(value=default_title)
    title_entry = ttk.Entry(frame, textvariable=title_var, width=32)
    title_entry.grid(row=0, column=1, pady=(0, 6 * _UI_SCALE))

    ttk.Label(frame, text="Barcode value:").grid(row=1, column=0, sticky="w")
    serial_var = tk.StringVar()
    serial_entry = ttk.Entry(frame, textvariable=serial_var, width=32)
    serial_entry.grid(row=1, column=1)

    ttk.Label(frame, text="Align:").grid(row=2, column=0, sticky="w", pady=(6 * _UI_SCALE, 0))
    align_var = tk.StringVar(value=layout.align)
    align_combo = ttk.Combobox(
        frame,
        textvariable=align_var,
        values=sorted(_H_ALIGN),
        state="readonly",
        width=10,
    )
    align_combo.grid(row=2, column=1, sticky="w", pady=(6 * _UI_SCALE, 0))

    title_size_var = tk.StringVar(value=layout.title_size)
    barcode_size_var = tk.StringVar(value=layout.barcode_size)
    caption_size_var = tk.StringVar(value=layout.caption_size)

    def schedule_preview_proxy(*_args: object) -> None:
        schedule_preview()

    _title_stepper, title_size_label = _add_size_stepper(
        frame,
        row=3,
        label="Title size:",
        size_var=title_size_var,
        on_change=schedule_preview_proxy,
    )
    barcode_stepper, _barcode_size_label = _add_size_stepper(
        frame,
        row=4,
        label="Barcode size:",
        size_var=barcode_size_var,
        on_change=schedule_preview_proxy,
    )
    caption_stepper, _caption_size_label = _add_size_stepper(
        frame,
        row=5,
        label="Caption size:",
        size_var=caption_size_var,
        on_change=schedule_preview_proxy,
    )

    ttk.Label(frame, text="Copies:").grid(row=6, column=0, sticky="w", pady=(6 * _UI_SCALE, 0))
    copies_var = tk.StringVar(value="1")
    copies_combo = ttk.Combobox(
        frame,
        textvariable=copies_var,
        values=[str(n) for n in range(1, 11)],
        state="readonly",
        width=10,
    )
    copies_combo.grid(row=6, column=1, sticky="w", pady=(6 * _UI_SCALE, 0))

    def current_layout() -> LabelLayout:
        return LabelLayout(
            align=align_var.get(),
            title_size=title_size_var.get(),
            barcode_size=barcode_size_var.get(),
            caption_size=caption_size_var.get(),
        )

    def current_copies() -> int:
        try:
            return max(1, min(10, int(copies_var.get())))
        except ValueError:
            return 1

    def update_size_controls_for_mode(*_args: object) -> None:
        """No barcode → note mode: title size scales the note; barcode/caption off."""
        has_barcode = bool(serial_var.get().strip())
        title_size_label.configure(
            text="Title size:" if has_barcode else "Note size:"
        )
        state = ["!disabled"] if has_barcode else ["disabled"]
        for child in (*barcode_stepper.winfo_children(), *caption_stepper.winfo_children()):
            try:
                child.state(state)
            except tk.TclError:
                pass

    preview_frame = ttk.LabelFrame(frame, text="Preview", padding=6 * _UI_SCALE)
    preview_frame.grid(
        row=preview_row, column=0, columnspan=2, sticky="w", pady=(pad, 0)
    )
    preview_frame.grid_rowconfigure(0, minsize=_PREVIEW_MAX_HEIGHT)
    preview_label = ttk.Label(
        preview_frame,
        text="Loading preview...",
        anchor="center",
        justify="center",
    )
    preview_label.grid(row=0, column=0, sticky="n")

    preview_state = {"photo": None, "request_id": 0, "after_id": None, "closed": False}

    def apply_preview(request_id: int, png_data: bytes | None, error: str | None) -> None:
        if preview_state["closed"] or request_id != preview_state["request_id"]:
            return
        if png_data is None:
            preview_state["photo"] = None
            preview_label.configure(image="", text=error or "Preview unavailable")
            return
        photo = _png_to_photo(png_data)
        preview_state["photo"] = photo
        preview_label.configure(image=photo, text="")

    def refresh_preview() -> None:
        request_id = preview_state["request_id"] + 1
        preview_state["request_id"] = request_id
        zpl = _preview_zpl(title_var.get(), serial_var.get(), current_layout())

        def fetch() -> None:
            try:
                png_data = _fetch_labelary_png(zpl)
                error = None
            except RuntimeError as exc:
                png_data = None
                error = str(exc)
            if not preview_state["closed"]:
                root.after(0, lambda: apply_preview(request_id, png_data, error))

        threading.Thread(target=fetch, daemon=True).start()

    def schedule_preview(*_args: object) -> None:
        after_id = preview_state["after_id"]
        if after_id is not None:
            root.after_cancel(after_id)
        preview_state["after_id"] = root.after(_PREVIEW_DEBOUNCE_MS, refresh_preview)

    title_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", schedule_preview)
    serial_var.trace_add("write", update_size_controls_for_mode)
    align_var.trace_add("write", schedule_preview)
    align_combo.bind("<<ComboboxSelected>>", schedule_preview)
    title_size_var.trace_add("write", schedule_preview)
    barcode_size_var.trace_add("write", schedule_preview)
    caption_size_var.trace_add("write", schedule_preview)
    update_size_controls_for_mode()

    status_var = tk.StringVar(value="")
    status_style = ttk.Style()
    status_style.configure("Status.TLabel", font=("Segoe UI", 8), foreground="red")
    status_label = ttk.Label(
        frame,
        textvariable=status_var,
        style="Status.TLabel",
    )
    status_label.grid(
        row=8, column=0, columnspan=2, sticky="nw", pady=(2 * _UI_SCALE, 0)
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

        zpl = _build_zpl(title, serial, current_layout(), copies=current_copies())

        set_printing(False)
        status_var.set(_status_line("Sending to printer..."))
        root.update_idletasks()

        try:
            host, port = _load_printer_config()
            status_var.set(_status_line(f"Connecting to {host}:{port}..."))
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
    buttons.grid(row=9, column=0, columnspan=2, sticky="w", pady=(_BUTTON_GAP, 0))
    print_button = ttk.Button(buttons, text="Print", command=on_print)
    print_button.pack(side=tk.LEFT)
    cancel_button = ttk.Button(buttons, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=(6, 0))

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
        LabelLayout(
            align=args.align,
            title_size=args.title_size,
            barcode_size=args.barcode_size,
            caption_size=args.caption_size,
        ),
    )
