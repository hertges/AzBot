import ctypes, signal
ctypes.pythonapi.raise_ = None
import os
os.kill(os.getpid(), 11)
