@echo off
setlocal
cd /d "%~dp0\.."
python -m pip install -r webapp\requirements.txt -q
python webapp\app.py
endlocal
