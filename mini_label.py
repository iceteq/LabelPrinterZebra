import json
import socket
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


def _ask_label_fields():
    root = tk.Tk()
    root.title("Print label")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=12)
    frame.grid(row=0, column=0)

    ttk.Label(frame, text="Title:").grid(row=0, column=0, sticky="w", pady=(0, 6))
    title_var = tk.StringVar(value="Act")
    title_entry = ttk.Entry(frame, textvariable=title_var, width=32)
    title_entry.grid(row=0, column=1, pady=(0, 6))

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
    buttons.grid(row=2, column=0, columnspan=2, pady=(12, 0))
    ttk.Button(buttons, text="Print", command=on_print, width=10).grid(
        row=0, column=0, padx=(0, 6)
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

    root.update_idletasks()
    root.after(0, focus_serial_entry)
    root.after(100, focus_serial_entry)
    root.mainloop()

    if result["cancelled"]:
        return None
    return result["title"], result["serial"]


def print_label():
    fields = _ask_label_fields()
    if fields is None:
        return
    title, serial = fields
    zpl = _build_zpl(title, serial)
    host, port = _load_printer_config()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(zpl.encode("utf-8"))


if __name__ == "__main__":
    print_label()
