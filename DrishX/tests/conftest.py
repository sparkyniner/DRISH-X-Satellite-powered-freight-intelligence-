import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DRISHX_DATA_DIR', str(Path(__file__).resolve().parents[1] / 'drishx_data' / 'tests'))
