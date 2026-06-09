@echo off
REM ============================================================
REM PXML Document Processor - Start All Services
REM Starts: FastAPI backend, Streamlit frontend, Nginx reverse proxy
REM ============================================================

set PROJECT_DIR=C:\canada_trackchanges\new_approach_working_f1 - Canada_with_validation_deploy_s3_executorpool
set NGINX_DIR=C:\nginx-1.30.2
set VENV=C:\canada_trackchanges\cdn_env\Scripts\activate.bat
set PYTHON=python

echo ============================================================
echo  PXML Document Processor - Starting Services
echo ============================================================
echo.

REM --- Create logs folder ---
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

REM --- Start FastAPI backend (port 8000) ---
echo [1/3] Starting FastAPI backend on port 8000...
start "FastAPI" cmd /k ""%VENV%" && cd /d "%PROJECT_DIR%" && %PYTHON% -m uvicorn pipeline_api:app --host 127.0.0.1 --port 8000 --workers 1"
timeout /t 5 /nobreak > nul

REM --- Start Streamlit frontend (port 8501) ---
echo [2/3] Starting Streamlit frontend on port 8501...
start "Streamlit" cmd /k ""%VENV%" && cd /d "%PROJECT_DIR%" && %PYTHON% -m streamlit run streamlit_app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false"
timeout /t 5 /nobreak > nul

REM --- Start Nginx (port 80) ---
echo [3/3] Starting Nginx on port 80...
cd /d "%NGINX_DIR%"
start "" nginx.exe

echo.
echo ============================================================
echo  All services started!
echo  Access the app at: http://canada-revisions
echo  (or http://10.171.122.4 from other machines)
echo ============================================================
echo.
echo  FastAPI:   http://127.0.0.1:8000
echo  Streamlit: http://127.0.0.1:8501
echo  Nginx:     http://canada-revisions (port 80)
echo.
echo  Logs: %PROJECT_DIR%\logs\
echo ============================================================
pause
