"""Physical native-grid 700/850/925-hPa moisture conversions (no interpolation).

SPFH (kg kg-1) and native TMP (K) can yield dewpoint (K) at the same
isobaric level. Never derive from a different pressure, grid or analysis time.
All calculations preserve NaNs and missing-value sentinels.
"""
import math
from array import array
import sys


def dewpoint_kelvin_from_specific_humidity(q, pressure_hpa):
    if not math.isfinite(q) or not (0 < q < .1) or pressure_hpa not in (700, 850, 925):
        return math.nan
    e = (float(q) * float(pressure_hpa) * 100.0) / (.62197 + .37803 * float(q))
    if not 0 < e < pressure_hpa*100:
        return math.nan
    log_ratio = math.log(e / 611.2)
    d = 17.67 - log_ratio
    if d <= 0:
        return math.nan
    value = 273.15 + 243.5 * log_ratio / d
    return value if 150 <= value <= 330 else math.nan


def derive_dewpoint_bytes(q_bytes, t_bytes, pressure_hpa):
    q = array('f'); t = array('f')
    q.frombytes(q_bytes); t.frombytes(t_bytes)
    if len(q) != len(t):
        raise ValueError('SPFH and temperature grids differ; cannot derive dewpoint')
    if sys.byteorder != 'little':
        q.byteswap(); t.byteswap()
    for i in range(len(q)):
        value = dewpoint_kelvin_from_specific_humidity(q[i],pressure_hpa)
        # A physically invalid source temperature invalidates this profile point.
        if not (math.isfinite(t[i]) and 150 <= t[i] <= 340 and value <= t[i]+.3):
            value = math.nan
        q[i] = value
    if sys.byteorder != 'little': q.byteswap()
    return q.tobytes()
