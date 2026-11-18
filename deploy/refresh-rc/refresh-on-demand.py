"""Fixed argument-free operator entry. Run with release-local Python -I -B."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
from website_analytics.analysis.repeatable_runner import main
raise SystemExit(main('on_demand'))
