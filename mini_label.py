import json
import socket
from pathlib import Path

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


def print_hello():
    # ^CI28 = interpret field data as UTF-8 (needed for å, ä, ö, etc.)
    zpl = (
        "^XA^CI28"
        "^FO50,80^A0N,80,80^FDVänersborg^FS"
        "^FO50,180^BY2,3,80^BCN,100,N,N,N^FDVänersborg^FS"
        "^XZ"
    )
    host, port = _load_printer_config()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(zpl.encode("utf-8"))

if __name__ == "__main__":
    print_hello()