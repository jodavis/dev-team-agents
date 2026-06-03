@echo off
call "%~dp0validate-build.cmd"
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
call "%~dp0validate-tests.cmd"
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
