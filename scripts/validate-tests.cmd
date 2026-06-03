@echo off
set PYTHONPATH=%~dp0..\plugins\dev-team\scripts;%PYTHONPATH%
python -m pytest "%~dp0..\plugins\dev-team\scripts\test_dev_team.py" -v
exit /b %ERRORLEVEL%
