# Label printer (Zebra ZPL)

Small Python script that sends ZPL to a Zebra printer on your network over a raw TCP socket (usually port **9100**).

## First-time setup

1. **Create your local config**  
   The script reads **`printer_config.json`** next to `mini_label.py`. That file is **not** in the repository (so your printer IP stays private and machine-specific).

   Copy the example and edit it:

   ```text
   copy printer_config.example.json printer_config.json
   ```

   On PowerShell you can use `Copy-Item` instead of `copy` if you prefer.

2. **Edit `printer_config.json`**  
   Set **`host`** to your printer’s IP address or hostname, and **`port`** to the raw printing port (typically **9100**).

3. **Run**

   ```text
   python mini_label.py
   ```

## Repository layout

| File | Tracked in git? | Purpose |
|------|-----------------|--------|
| `printer_config.example.json` | Yes | Template with dummy values; copy to `printer_config.json`. |
| `printer_config.json` | **No** (gitignored) | Your real printer `host` / `port`. You create this locally. |

If you run the script without `printer_config.json`, you’ll get an error that reminds you to copy the example file and read this README.

## Requirements

Python 3 (stdlib only: `json`, `socket`, `pathlib`).
