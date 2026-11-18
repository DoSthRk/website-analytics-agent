"""Fixed entry. Run with the pinned venv Python -I -B; accepts no arguments."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
from website_analytics.analysis.release_runner import main
raise SystemExit(main("source"))
