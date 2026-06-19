@echo off
echo Building EmailManager...
pyinstaller --noconsole --onefile --icon=app.ico ^
    --add-data "active.ico;." ^
    --add-data "stopped.ico;." ^
    emailmanager.py
echo.
echo Done! Executable is at dist\emailmanager.exe
pause
