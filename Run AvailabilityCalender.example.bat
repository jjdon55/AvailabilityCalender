@echo off
REM ============================================================
REM  AvailabilityCalender local launcher - TEMPLATE
REM
REM  To use this file:
REM  1. Make a copy named "Run AvailabilityCalender.bat"
REM     (the .bat without ".example" in the name)
REM  2. Open the copy in Notepad
REM  3. Replace REPLACE_WITH_YOUR_JD_KEY below with the JD_KEY
REM     value from .streamlit\secrets.toml
REM  4. Save and double-click to launch
REM
REM  The non-example .bat is gitignored so the key never reaches
REM  GitHub. Do not commit a .bat with a real key in it.
REM ============================================================

echo Installing/checking required libraries...
pip install streamlit icalendar python-dateutil
echo.
echo Starting AvailabilityCalender...
echo Opening browser to your JD view...
echo Keep this window open while using the app.
echo Close this window to shut down the app.
echo.
cd /d "C:\Users\jj\Desktop\AvailabilityCalender"
start "" "http://localhost:8501/?key=REPLACE_WITH_YOUR_JD_KEY"
python -m streamlit run "C:\Users\jj\Desktop\AvailabilityCalender\app.py" --server.headless=true
pause