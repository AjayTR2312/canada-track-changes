@echo off
REM ============================================================
REM PXML Document Processor - Stop All Services
REM ============================================================

echo Stopping Nginx...
taskkill /f /im nginx.exe 2>nul

echo Stopping Streamlit...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8501 ^| findstr LISTENING') do taskkill /f /pid %%a 2>nul

echo Stopping FastAPI...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do taskkill /f /pid %%a 2>nul

echo.
echo All services stopped.
pause
