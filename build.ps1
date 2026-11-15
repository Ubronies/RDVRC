# Сборка одного exe: dist\DiscordVoiceCatcher.exe
# Запуск: powershell -ExecutionPolicy Bypass -File build.ps1
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --onefile --windowed --name DiscordVoiceCatcher `
    --icon "$PSScriptRoot\icon.ico" `
    --hidden-import pystray._win32 --hidden-import dvc_gui --collect-data customtkinter `
    --workpath build\work --specpath build --distpath dist `
    discord_voice_catcher.py
