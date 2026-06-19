@echo off
echo Building EmailManager...
pyinstaller --noconsole --onefile --icon=app.ico ^
    --add-data "active.ico;." ^
    --add-data "stopped.ico;." ^
    --hidden-import flask ^
    --hidden-import werkzeug ^
    --hidden-import werkzeug.serving ^
    --hidden-import werkzeug.routing.rules ^
    emailmanager.py
echo.
echo Done! Executable is at dist\emailmanager.exe
pause
