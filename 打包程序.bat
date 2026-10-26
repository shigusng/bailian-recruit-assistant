@echo off
setlocal
cd /d "%~dp0"
if not exist ".build-venv\Scripts\python.exe" (
    python -m venv .build-venv
    if errorlevel 1 goto failed
)
".build-venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
if not exist ".deps\rapidocr_onnxruntime\__init__.py" (
    ".build-venv\Scripts\python.exe" -m pip install --target .deps -r requirements-gui.txt
    if errorlevel 1 goto failed
)
if not exist ".gpu-deps\onnxruntime\capi\DirectML.dll" (
    ".build-venv\Scripts\python.exe" -m pip install --target .gpu-deps --no-deps -r requirements-gpu.txt
    if errorlevel 1 goto failed
)
".build-venv\Scripts\python.exe" build_release.py
if errorlevel 1 goto failed
echo Build complete. Open the output folder in dist.
pause
exit /b 0
:failed
echo Build failed. See the error above.
pause
exit /b 1
