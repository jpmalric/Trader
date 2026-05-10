@echo off
:: ============================================================
:: Planificateur Windows — Screener quotidien 9h00
:: Double-cliquez pour installer la tache planifiee
:: ============================================================

set PYTHON=C:\Users\jeanp\AppData\Local\Python\pythoncore-3.14-64\python.exe
set SCRIPT=C:\Users\jeanp\Documents\GitHub\Trader\run_daily.bat
set TASKNAME=ScreenerQuotidien

echo.
echo  Installation de la tache planifiee "%TASKNAME%"
echo  Heure : tous les jours a 08h30 (refresh + envoi email)
echo.

:: Supprime l'ancienne tache si elle existe
schtasks /delete /tn "%TASKNAME%" /f >nul 2>&1

:: Cree la nouvelle tache (avec reveil depuis veille)
schtasks /create ^
  /tn "%TASKNAME%" ^
  /tr "\"%SCRIPT%\"" ^
  /sc daily ^
  /st 08:30 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /it ^
  /f

:: Active le reveil depuis veille pour cette tache
powershell -Command "& { $t = Get-ScheduledTask -TaskName '%TASKNAME%'; $t.Settings.WakeToRun = $true; Set-ScheduledTask $t }"

if %ERRORLEVEL% EQU 0 (
    echo  OK - Tache installee avec succes !
    echo.
    echo  Pour verifier : ouvrez le Planificateur de taches Windows
    echo  Pour tester maintenant :
    echo    schtasks /run /tn "%TASKNAME%"
) else (
    echo  ERREUR - Essayez en executant ce fichier en tant qu'Administrateur
)

echo.
pause
