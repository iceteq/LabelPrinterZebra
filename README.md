# Label printer (Zebra ZPL)

Small Python script that sends ZPL to a Zebra printer on your network over a raw TCP socket (usually port **9100**).

## First-time setup

1. **Run the app**

   ```text
   python mini_label.py
   ```

2. **Add a printer in the UI**  
   Open **More options → Manage…** to add a name, IP/hostname, and port (typically **9100**).  
   Your list is saved locally in **`printer_config.json`** (gitignored — not in the repo).

   Optional: copy the example file first if you prefer editing JSON by hand:

   ```text
   copy printer_config.example.json printer_config.json
   ```

## Repository layout

| File | Tracked in git? | Purpose |
|------|-----------------|--------|
| `printer_config.example.json` | Yes | Example multi-printer shape (dummy IP). |
| `printer_config.json` | **No** (gitignored) | Your real printers + which one is selected. |

Everyday use is just **Title**, **Barcode**, preview, and **Print**. Look, note size, copies, and printer management stay under **More options**.

Default look is **Quiet**. Empty labels auto-switch to **Note** (text only).

If you print with no printers saved, the app asks you to open **Manage…**.

Legacy configs that only had `"host"` / `"port"` are still read and upgraded automatically on save.

## Requirements

Python 3 with tkinter, plus:

```text
pip install -r requirements.txt
```

Uses **CustomTkinter** and **Pillow** for the print dialog UI and preview.
