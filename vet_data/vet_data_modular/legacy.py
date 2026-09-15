"""Compatibility exports for the bundled VET_DATA base implementation.

The base implementation lives in this package, so starting the modular
application never requires a historical source file outside ``vet_data``.
"""

from . import baseline

LimitedViewBox = baseline.LimitedViewBox
ParseWorker = baseline.ParseWorker
ParseProgressDialog = baseline.ParseProgressDialog
BusConfigWidget = baseline.BusConfigWidget
ARXMLConverterDialog = baseline.ARXMLConverterDialog
