#Requires AutoHotkey v2.0

; Hotkeys to open the label dialog with a preset title (barcode field is focused for scanning).
; Keep this file next to mini_label.py in your git clone, then reload the script after git pull.

SetWorkingDir A_ScriptDir

^!a:: RunLabel("Act")
^!s:: RunLabel("Serienummer")
^!r:: RunLabel("Req")

RunLabel(title) {
    script := A_ScriptDir "\mini_label.py"
    ; Show normally — do not use a hidden window style.
    Run Format('python "{1}" "{2}"', script, title), A_ScriptDir, "Normal"
}
