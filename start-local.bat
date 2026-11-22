@echo off
set ROOT=%~dp0
if exist "C:\Program Files\Tesseract-OCR" (
    set "PATH=C:\Program Files\Tesseract-OCR;%PATH%"
    set "FINALSAY_TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe"
)
start "FinalSay backend" cmd /k "cd /d %ROOT%backend && set FINALSAY_COMPARISON_MODEL=mock&& .venv\Scripts\python.exe -m uvicorn finalsay.main:app --host 127.0.0.1 --port 8000 --reload"
start "FinalSay web" cmd /k "cd /d %ROOT%apps\web && npm run dev"
powershell -Command "Start-Sleep -Seconds 4"
start http://localhost:5173/
