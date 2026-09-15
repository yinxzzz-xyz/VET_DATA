# VET_DATA modular merge

`VET_DATA_merged.py` is the supported launcher. The complete base
implementation is bundled as `vet_data_modular/baseline.py`; `legacy.py` only
provides compatibility exports from that package-local module. The application
therefore runs with the contents of `vet_data` alone and does not load any
historical source file from its parent folder.

The separately testable modules add v10 signal-list interactions, plotting
behavior, collapsible panels, searchable calculation channels, and background
data-file loading while retaining the base protocol/container-frame behavior.

Run checks with:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "vet_data")
& "D:\Python\python.exe" -m unittest discover -s tests -v
```

Representative MDF/BLF/DBC/ARXML files are still required for vehicle-data
compatibility testing.  CSV and widget behavior are covered by synthetic tests.
