Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
pythonw = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
launcher = files.BuildPath(root, "scripts\run_silent.py")
shell.Run Chr(34) & pythonw & Chr(34) & " -X utf8 " & Chr(34) & launcher & Chr(34), 0, False
