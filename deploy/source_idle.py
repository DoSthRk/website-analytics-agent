#!/usr/bin/python3
"""Do not import while either existing oneshot collector is still executing."""
import subprocess,sys
if len(sys.argv)!=1:raise SystemExit(2)
for unit in ('website-analytics-sync@full.service','website-analytics-sync@intraday.service'):
 try:
  state=subprocess.check_output(['/usr/bin/systemctl','show',unit,'-p','ActiveState','--value'],text=True,timeout=10).strip()
 except (subprocess.SubprocessError,OSError):raise SystemExit(1)
 if state not in ('inactive','failed'):
  print('fullsite import deferred: original source collector is not idle')
  raise SystemExit(1)
