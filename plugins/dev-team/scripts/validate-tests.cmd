@echo off
set PYTHONPATH=%~dp0;%PYTHONPATH%
python -m pytest "%~dp0test_dev_team.py" -v
exit /b %ERRORLEVEL%
