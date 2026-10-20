#Requires AutoHotkey v2.0

; Ctrl+Alt+L opens the label dialog with an empty title (title field focused).
; Keep this file next to mini_label.py in your git clone, then reload after git pull.
; Uses pythonw so no black console window appears behind the dialog.

SetWorkingDir A_ScriptDir

^!l:: RunLabel()

RunLabel() {
    script := A_ScriptDir "\mini_label.py"
    ; Prefer pythonw (GUI, no console). Fall back to hidden python console.
    try {
        Run Format('pythonw "{1}"', script), A_ScriptDir
    } catch {
        Run Format('python "{1}"', script), A_ScriptDir, "Hide"
    }
}
