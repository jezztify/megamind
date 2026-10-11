# Implementation

## Outcome
Corrected the Windows build pipeline so it installs MegaMind dependencies into the exact Python environment that invokes PyInstaller. The rebuilt server bundle includes `uvicorn` and starts on port 9090.

## Files changed
- `build_windows.ps1`: invokes `python -m pip install -r requirements.txt` before packaging and invokes PyInstaller via `python -m PyInstaller` to guarantee a single interpreter environment.

## Verification
- Before the fix, `python -m pip show uvicorn fastapi` reported both packages absent from the packaging interpreter.
- Ran `build_windows.ps1` successfully. The PyInstaller output processed `hook-uvicorn.py` and produced both executables.
- Ran `timeout 8s ./dist/MegaMind_Server.exe`; it started successfully, reported `Web UI : http://192.168.0.126:9090`, and did not raise an import error. The timeout intentionally stopped the running server.

## Risks
- The build downloads/updates Python requirements when run, so it requires network access on a newly provisioned build environment.
