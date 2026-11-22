@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建 Python 虚拟环境...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    where python >nul 2>nul
    if errorlevel 1 (
      echo 未找到 Python。请先安装 Python 3.11 或更高版本。
      pause
      exit /b 1
    )
    python -m venv .venv
  )
  if errorlevel 1 (
    echo 虚拟环境创建失败。
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import fastapi, sqlalchemy, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo 正在安装项目依赖，首次运行需要连接网络...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败。请检查网络后重新运行。
    pause
    exit /b 1
  )
)

if not exist ".env" copy /y ".env.example" ".env" >nul

if not exist "demo.db" (
  echo 正在创建脱敏演示数据...
  ".venv\Scripts\python.exe" scripts\create_demo_data.py
  if errorlevel 1 (
    echo 演示数据创建失败。
    pause
    exit /b 1
  )
)

set "DATABASE_URL=sqlite:///./demo.db"
set "DINGTALK_LOGIN_ENABLED=false"
set "LOCAL_USER_ID=1"
set "PREVIEW_SAFE_MODE=true"
set "SESSION_COOKIE_SECURE=false"
set "SF_MOCK_MODE=true"
set "SF_LIVE_ORDER_ENABLED=false"
set "DB_MOCK_MODE=true"
set "DB_LIVE_ORDER_ENABLED=false"
set "JDL_LIVE_ORDER_ENABLED=false"
set "SF_CITY_LIVE_ORDER_ENABLED=false"
echo.
echo 系统启动后将自动打开：http://127.0.0.1:26666/
echo 请保持当前窗口运行。
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:26666/'"
".venv\Scripts\python.exe" run.py
