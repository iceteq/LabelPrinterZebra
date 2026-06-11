import argparse
import json
import socket
import sys
import tkinter as tk
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


def _build_zpl(title: str, serial: str) -> str:
    # ^CI28 = UTF-8; ^BC f=Y prints interpretation line below the bars
    return (
        "^XA^CI28"
        f"^FO50,40^A0N,50,50^FD{_zpl_field(title)}^FS"
        f"^FO50,100^BY3,3,120^BCN,120,Y,N,N^FD{_zpl_field(serial)}^FS"
        "^XZ"
    )


_UI_SCALE = 2


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


def _center_window(window: tk.Tk) -> None:
    window.update_idletasks()
    width = window.winfo_reqwidth()
    height = window.winfo_reqheight()
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
    return parser.parse_args()


def _ask_label_fields(default_title: str = ""):
    pad = 12 * _UI_SCALE

    root = tk.Tk()
    root.withdraw()
    root.title("Print label")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * _UI_SCALE)

    frame = ttk.Frame(root, padding=pad)
    frame.grid(row=0, column=0)

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

    result = {"cancelled": True}

    def on_print():
        if not serial_var.get().strip():
            messagebox.showwarning(
                "Missing value",
                "Enter a barcode value to print.",
                parent=root,
            )
            serial_entry.focus_force()
            return
        result["title"] = title_var.get().strip()
        result["serial"] = serial_var.get().strip()
        result["cancelled"] = False
        root.destroy()

    def on_cancel():
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=2, column=0, columnspan=2, pady=(pad, 0))
    ttk.Button(buttons, text="Print", command=on_print, width=10).grid(
        row=0, column=0, padx=(0, 6 * _UI_SCALE)
    )
    ttk.Button(buttons, text="Cancel", command=on_cancel, width=10).grid(
        row=0, column=1
    )

    root.bind("<Return>", lambda _event: on_print())
    root.bind("<Escape>", lambda _event: on_cancel())
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    def focus_serial_entry():
        root.lift()
        root.attributes("-topmost", True)
        serial_entry.focus_force()

    _center_window(root)
    root.after(0, focus_serial_entry)
    root.after(100, focus_serial_entry)
    root.mainloop()

    if result["cancelled"]:
        return None
    return result["title"], result["serial"]


def print_label(default_title: str = ""):
    fields = _ask_label_fields(default_title)
    if fields is None:
        return
    title, serial = fields
    zpl = _build_zpl(title, serial)
    host, port = _load_printer_config()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(zpl.encode("utf-8"))


if __name__ == "__main__":
    print_label(_parse_args().title)
