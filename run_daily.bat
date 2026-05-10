@echo off
:: ============================================================
:: Screener quotidien — lance par le Planificateur Windows
:: Execution : refresh donnees + envoi email + mise en veille
:: Log : run_daily.log
:: ============================================================

set PYTHON=C:\Users\jeanp\AppData\Local\Python\pythoncore-3.14-64\python.exe
set DIR=C:\Users\jeanp\Documents\GitHub\Trader
set LOG=%DIR%\run_daily.log

cd /d "%DIR%"

echo. >> "%LOG%"
echo ======================================== >> "%LOG%"
echo [%date% %time%] Demarrage >> "%LOG%"

:: Lance le screener (refresh + rapports + email)
"%PYTHON%" send_reports.py --refresh >> "%LOG%" 2>&1

if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] Succes - email envoye >> "%LOG%"
) else (
    echo [%date% %time%] ERREUR - code %ERRORLEVEL% >> "%LOG%"
)

echo [%date% %time%] Mise en veille dans 60 secondes... >> "%LOG%"

:: Pause 60 secondes (permet d'annuler avec Ctrl+C si necessaire)
timeout /t 60 /nobreak >nul

:: Mise en veille systeme
echo [%date% %time%] Mise en veille. >> "%LOG%"
rundll32.exe powrprof.dll,SetSuspendState 0,1,0
