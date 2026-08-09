import os
import sys
import tempfile
from pathlib import Path

# Must run before `import jobs` (in any test module) reads JOBS_DB_PATH at
# import time -- otherwise tests silently write into the real backend/jobs.db.
os.environ["JOBS_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test_jobs.db")

# Same reasoning for the X bookmarks store: `x_bookmarks` resolves DB_PATH and
# DATA_DIR at import time and calls init_db() there, so both must point at a
# throwaway location before any test module imports it.
_x_tmp = Path(tempfile.mkdtemp())
os.environ["X_BOOKMARKS_DB"] = str(_x_tmp / "test_x_bookmarks.db")
os.environ["X_DATA_DIR"] = str(_x_tmp / "x-data")

sys.path.insert(0, str(Path(__file__).parent))
