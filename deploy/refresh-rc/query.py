"""Credential-free fixed loopback entry; no CLI/source configuration input."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
from website_analytics.analysis.release_runner import main
raise SystemExit(main("query"))
