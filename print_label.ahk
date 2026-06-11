#Requires AutoHotkey v2.0

; Hotkeys to open the label dialog with a preset title (barcode field is focused for scanning).
; Run this script at startup or double-click it to enable the hotkeys.

^!a:: RunLabel("Act")
^!s:: RunLabel("Serienummer")
^!r:: RunLabel("Req")

RunLabel(title) {
    script := A_ScriptDir "\mini_label.py"
    cmd := Format('python "{1}" "{2}"', script, title)
    ComObject("WScript.Shell").Run(cmd, 0, false)
}