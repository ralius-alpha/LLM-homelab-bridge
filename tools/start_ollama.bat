@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

:: ==========================================
:: Official Ollama + Intel Arc A770 Launcher
:: ==========================================
set OLLAMA_DEBUG=1
set OLLAMA_NUM_PARALLEL=1
set OLLAMA_INTEL_GPU=true
set SYCL_ENABLE_DEFAULT_CONTEXTS=1
set ONEAPI_DEVICE_SELECTOR=level_zero:0
set OLLAMA_GPU_OVERHEAD=1024

set "OLLAMA_OFFICIAL_PATH=%LOCALAPPDATA%\Programs\Ollama"
set "PATH=%OLLAMA_OFFICIAL_PATH%;%PATH%"
set "LOG_FILE=%~dp0ollama_server.log"

:: 起動前のクリーンアップ
taskkill /f /im ollama.exe >nul 2>&1
timeout /t 1 > nul

:menu
cls
echo ===================================================
echo   Official Ollama - Intel Arc A770 Multi-Window
echo ===================================================
echo  [1] DeepSeek-R1 : 7B   (VRAM格納・高速)
echo  [2] DeepSeek-R1 : 14B  (VRAM格納・超おすすめ)
echo  [3] DeepSeek-R1 : 32B  (VRAM+CPU・高精度)
echo  [4] DeepSeek-R1 : 1.5B (軽量テスト用)
echo  [5] EXIT
echo ===================================================
set choice=
set /p choice="Enter menu number (1-5): "

if "%choice%"=="1" set "MODEL=deepseek-r1:7b" & goto run_multi
if "%choice%"=="2" set "MODEL=deepseek-r1:14b" & goto run_multi
if "%choice%"=="3" set "MODEL=deepseek-r1:32b" & goto run_multi
if "%choice%"=="4" set "MODEL=deepseek-r1:1.5b" & goto run_multi
if "%choice%"=="5" goto end
goto menu

:run_multi
if exist "%LOG_FILE%" del /f /q "%LOG_FILE%"
echo.
echo [INFO] 裏方サーバーを起動中...
start /b "" ollama serve > "%LOG_FILE%" 2>&1
timeout /t 3 > nul

echo [INFO] 別ウィンドウで %MODEL% を起動します...
echo [INFO] ※チャット画面を閉じると、サーバーも自動で終了します。
echo ---------------------------------------------------
:: 別ウィンドウ(cmd)を立ち上げて環境変数を引き継ぎつつollama runを実行
start "Ollama Chat - %MODEL%" /wait cmd /c "set OLLAMA_INTEL_GPU=true&& set SYCL_ENABLE_DEFAULT_CONTEXTS=1&& set ONEAPI_DEVICE_SELECTOR=level_zero:0&& "%OLLAMA_OFFICIAL_PATH%\ollama.exe" run %MODEL%"

echo.
echo [INFO] チャットが終了しました。裏方サーバーを安全に停止しています...
taskkill /f /im ollama.exe >nul 2>&1
timeout /t 1 > nul
goto menu

:end
:: 終了時にも念のためプロセスを掃除
taskkill /f /im ollama.exe >nul 2>&1
endlocal
