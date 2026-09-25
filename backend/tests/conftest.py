"""pytest bootstrap: keep test-driven CLI calls out of the real usage log."""
import os

os.environ.setdefault("WATERFREE_USAGE_LOG", "0")
