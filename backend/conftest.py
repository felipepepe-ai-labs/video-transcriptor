import os
import sys
import tempfile
from pathlib import Path

# Must run before `import jobs` (in any test module) reads JOBS_DB_PATH at
# import time -- otherwise tests silently write into the real backend/jobs.db.
os.environ["JOBS_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test_jobs.db")

sys.path.insert(0, str(Path(__file__).parent))
