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

Everyday use is **Title**, quick chips (**Act · S/N · Req · Ord**), **Barcode**, preview, and **Print**. Look, alignment, copies, and printer management stay under **More**.

The dialog is wide (preview on the right) and opens at the **bottom** of the screen. Launch with **Ctrl+Alt+L** (`print_label.ahk`) — title starts empty. Enter in Title moves to Barcode; Enter in Barcode prints. After a successful print the dialog closes; press **Ctrl+Alt+L** again for the next label. The hotkey uses **pythonw** so no black console window appears.

Default look is **Quiet**. Text-only labels stay in **Note** mode (Look is disabled until a barcode is entered). Notes support sizes through **XXL** and Left / Right / Top / Bottom / Center alignment. Copies always start at **1**.

If you print with no printers saved, the app asks you to open **Manage…**.

Legacy configs that only had `"host"` / `"port"` are still read and upgraded automatically on save.

## Requirements

Python 3 with **tkinter**, plus:

```text
pip install -r requirements.txt
```

Uses **Pillow** for the label preview. Run `mini_label.py` from this repo folder (not an old unzipped copy).

If the window still fails to stay open, check `label_printer_error.log` next to the script.
