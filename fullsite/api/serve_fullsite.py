#!/usr/bin/python3
"""Fixed operator launcher. Inputs are not selected by HTTP clients."""
import sys
sys.path.insert(0,'/opt/website-analytics-query/releases/rc-29842e547fed91e1/src')
sys.path.insert(0,'/opt/website-analytics-query/fullsite-20261110')
from fullsite_api import install
install('/var/lib/website-analytics-query/fullsite/fullsite.sqlite')
from website_analytics.analysis.service import main
raise SystemExit(main(['--cases','/var/lib/website-analytics-query/live/cases.sqlite','--refresh-status','/var/lib/website-analytics-query/live/refresh-status.json','--port','8765']))
