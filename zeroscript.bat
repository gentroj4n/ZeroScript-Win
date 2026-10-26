@echo off
setlocal
cd /d "%~dp0"

rem Default to the local Ollama OpenAI-compatible endpoint.
rem Existing environment values take precedence over these defaults.
if not defined ZS_MODEL_BASE_URL set "ZS_MODEL_BASE_URL=http://127.0.0.1:11434/v1"
if not defined ZS_MODEL_API_KEY set "ZS_MODEL_API_KEY=ollama"
if not defined ZS_MODEL set "ZS_MODEL=qwen3:8b"

py -3 -c "import websockets, rich, prompt_toolkit" >nul 2>nul
if errorlevel 1 (
  echo Installing ZeroScript CLI dependencies for the Python launcher...
  py -3 -m pip install --user websockets rich prompt_toolkit
  if errorlevel 1 (
    echo Could not install ZeroScript CLI dependencies.
    echo Check internet access and Python pip, then retry.
    exit /b 1
  )
)
py -3 -m zeroscript_cli %*
exit /b %errorlevel%
