#Requires AutoHotkey v2.0

; Ctrl+Alt+L opens the label dialog with an empty title (title field focused).
; Keep this file next to mini_label.py in your git clone, then reload after git pull.

SetWorkingDir A_ScriptDir

^!l:: RunLabel()

RunLabel() {
    script := A_ScriptDir "\mini_label.py"
    ; Show normally — do not use a hidden window style.
    Run Format('python "{1}"', script), A_ScriptDir, "Normal"
}
