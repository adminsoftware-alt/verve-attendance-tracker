@echo off
REM ============================================
REM SCOUT BOT VM QUICK SETUP
REM Run as Administrator
REM ============================================

echo === Scout Bot VM Setup ===

REM 1. Create directory
echo [1/5] Creating C:\ScoutBot...
mkdir C:\ScoutBot 2>nul

REM 2. Create join_meeting.bat (EDIT THE MEETING ID AND PASSWORD!)
echo [2/5] Creating join_meeting.bat...
(
echo @echo off
echo timeout /t 60 /nobreak
echo start zoommtg://zoom.us/join?confno=9034027764^&pwd=YOUR_PASSWORD_HERE
) > C:\ScoutBot\join_meeting.bat
echo Created! EDIT C:\ScoutBot\join_meeting.bat with your meeting password!

REM 3. Create scheduled task
echo [3/5] Creating scheduled task for 9:55 AM...
schtasks /create /tn "ScoutBot-JoinMeeting" /tr "C:\ScoutBot\join_meeting.bat" /sc daily /st 09:55 /f

REM 4. Configure auto-login
echo [4/5] Configuring auto-login...
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 1 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName /t REG_SZ /d "dataapps" /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /t REG_SZ /d "nhhjayUY4~w.hR<" /f

REM 5. Disable sleep
echo [5/5] Disabling sleep...
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

echo.
echo === Setup Complete ===
echo.
echo REMAINING MANUAL STEPS:
echo 1. Download Zoom from https://zoom.us/download
echo 2. Install and log in as Scout Bot
echo 3. Zoom Settings ^> General ^> Enable "Start Zoom when I start Windows"
echo 4. EDIT C:\ScoutBot\join_meeting.bat with your meeting password
echo 5. Reboot to test
echo.
pause
