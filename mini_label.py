import argparse
import base64
import json
import socket
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

_CONFIG_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _CONFIG_DIR / "printer_config.json"
_EXAMPLE_NAME = "printer_config.example.json"


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


_LABELARY_DPMM = 8
_LABELARY_WIDTH_IN = 4
_LABELARY_HEIGHT_IN = 2
_MARGIN = 50
_TITLE_FONT_H = 50
_TITLE_FONT_W = 50
_BARCODE_HEIGHT = 120
_BARCODE_MODULE = 3
_GAP_TITLE_BARCODE = 10
_TITLE_Y = _MARGIN
_BARCODE_Y = _MARGIN + _TITLE_FONT_H + _GAP_TITLE_BARCODE

_H_ALIGN = {"left": "L", "center": "C", "right": "R"}


def _label_dots(width_in: float, height_in: float, dpmm: int) -> tuple[int, int]:
    dots_per_in = dpmm * 25.4
    return round(width_in * dots_per_in), round(height_in * dots_per_in)


_LABEL_WIDTH, _LABEL_HEIGHT = _label_dots(
    _LABELARY_WIDTH_IN, _LABELARY_HEIGHT_IN, _LABELARY_DPMM
)


@dataclass(frozen=True)
class LabelLayout:
    align: str = "left"

    def __post_init__(self) -> None:
        if self.align not in _H_ALIGN:
            raise ValueError(f"align must be one of {list(_H_ALIGN)}")


def _text_zpl(
    y: int,
    text: str,
    font_h: int,
    font_w: int,
    h_align: str,
) -> str:
    block_w = _LABEL_WIDTH - 2 * _MARGIN
    zpl_align = _H_ALIGN[h_align]
    return (
        f"^FO{_MARGIN},{y}^FB{block_w},1,0,{zpl_align},0"
        f"^A0N,{font_h},{font_w}^FD{_zpl_field(text)}^FS"
    )


def _estimate_barcode_width(serial: str) -> int:
    modules = (len(serial) + 3) * 11
    return modules * _BARCODE_MODULE


def _barcode_x(serial: str, h_align: str) -> int:
    width = _estimate_barcode_width(serial)
    block_w = _LABEL_WIDTH - 2 * _MARGIN
    if h_align == "left":
        return _MARGIN
    if h_align == "right":
        return max(_MARGIN, _LABEL_WIDTH - _MARGIN - width)
    return _MARGIN + max(0, (block_w - width) // 2)


def _build_zpl(title: str, serial: str, layout: LabelLayout | None = None) -> str:
    layout = layout or LabelLayout()
    barcode_x = _barcode_x(serial, layout.align)

    return (
        "^XA"
        "^CI28"
        f"^PW{_LABEL_WIDTH}"
        f"^LL{_LABEL_HEIGHT}"
        f"{_text_zpl(_TITLE_Y, title, _TITLE_FONT_H, _TITLE_FONT_W, layout.align)}"
        f"^FO{barcode_x},{_BARCODE_Y}^BY{_BARCODE_MODULE},3,{_BARCODE_HEIGHT}"
        f"^BCN,{_BARCODE_HEIGHT},Y,N,N^FD{_zpl_field(serial)}^FS"
        "^XZ"
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
    barcode = serial.strip() or "0"
    return _build_zpl(title.strip(), barcode, layout)


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
    return parser.parse_args()


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
    frame.grid_rowconfigure(3, minsize=preview_row_height)

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

    def current_layout() -> LabelLayout:
        return LabelLayout(align=align_var.get())

    preview_frame = ttk.LabelFrame(frame, text="Preview", padding=6 * _UI_SCALE)
    preview_frame.grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(pad, 0)
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
    align_var.trace_add("write", schedule_preview)
    align_combo.bind("<<ComboboxSelected>>", schedule_preview)

    status_var = tk.StringVar(value="")
    status_style = ttk.Style()
    status_style.configure("Status.TLabel", font=("Segoe UI", 8), foreground="red")
    status_label = ttk.Label(
        frame,
        textvariable=status_var,
        style="Status.TLabel",
    )
    status_label.grid(
        row=4, column=0, columnspan=2, sticky="nw", pady=(2 * _UI_SCALE, 0)
    )

    def set_printing(enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        print_button.state(state)
        cancel_button.state(state)

    def on_print():
        if not serial_var.get().strip():
            messagebox.showwarning(
                "Missing value",
                "Enter a barcode value to print.",
                parent=root,
            )
            serial_entry.focus_force()
            return

        title = title_var.get().strip()
        serial = serial_var.get().strip()
        zpl = _build_zpl(title, serial, current_layout())

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
    buttons.grid(row=5, column=0, columnspan=2, sticky="w", pady=(_BUTTON_GAP, 0))
    print_button = ttk.Button(buttons, text="Print", command=on_print)
    print_button.pack(side=tk.LEFT)
    cancel_button = ttk.Button(buttons, text="Cancel", command=on_cancel)
    cancel_button.pack(side=tk.LEFT, padx=(6, 0))

    for sequence in ("<Return>", "<KP_Enter>"):
        root.bind(sequence, lambda _event: on_print())
        serial_entry.bind(sequence, lambda _event: on_print())
    root.bind("<Escape>", lambda _event: on_cancel())
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def focus_serial_entry():
        root.lift()
        root.attributes("-topmost", True)
        serial_entry.focus_force()

    _center_window(root)
    schedule_preview()
    root.after(0, focus_serial_entry)
    root.after(100, focus_serial_entry)
    root.mainloop()


def print_label(default_title: str = "", layout: LabelLayout | None = None):
    _ask_label_fields(default_title, layout)


if __name__ == "__main__":
    args = _parse_args()
    print_label(args.title, LabelLayout(align=args.align))
