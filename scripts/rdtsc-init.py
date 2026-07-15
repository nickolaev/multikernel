import ctypes
from pathlib import Path

__version__ = "0.2.1"

so = ctypes.CDLL(str(Path(__file__).with_name("rdtsc.so.1")), use_errno=True)
get_cycles = so.get_cycles
get_cycles.argtypes = []
get_cycles.restype = ctypes.c_ulonglong
