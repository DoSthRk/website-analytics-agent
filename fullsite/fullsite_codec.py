"""Versioned, bounded row codec. Dictionary contains schema/public constants only."""
import hashlib,json,zlib
ENCODING='zlib-dict-json/v2'
_template={'sessions':0,'totalUsers':0,'activeUsers':0,'engagedSessions':0,'engagementRate':0.0,'screenPageViews':0,'keyEvents':0,'clicks':0,'impressions':0,'ctr':0.0,'position':0.0,'storedSubmissions':0,'quarantinedSubmissions':0,'nonQuarantinedSubmissions':0,'landingPagePlusQueryString':'/i/','sourceUrl':'https://www.genemedi.net/i/','page':'https://www.genemedi.net/i/','query':'','date':'2026-11-10','collected_at':'2026-11-10T03:48:00.000000+00:00','source':'ga4','truncated':False,'source_request_status':'ok','source_reported_rows':0,'provider_finality':'unknown','dimension_redacted':False,'source_status':'legacy_claim_unverified','finality_basis':'configured_lag_estimate_not_provider_verified','site':'genemedi-net','data_date':'2026-11-10','data_status':'complete','ga4_sessions':0,'ga4_key_events':0,'gsc_clicks':0,'gsc_impressions':0,'stored_submissions':0,'accepted_inquiries':0,'product_line_id':'','product_name':'','category_l1':'','category_l2':'','category_l3':'','refreshed_at':'2026-11-10T03:48:00+00:00','mapping_version':'3','information_mapping_version':'1','gsc':'inquiry','partial':None}
DICTIONARY=json.dumps(_template,ensure_ascii=False,separators=(',',':')).encode()+b' https://genemedi.net/i/ "source":"gsc" "source":"inquiry" "truncated":null "dimension_redacted":true '
DICTIONARY_SHA256=hashlib.sha256(DICTIONARY).hexdigest()
def encode_payload(row):
 data=json.dumps({k:v for k,v in row.items() if k not in ('start','end','row_id')},ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
 if len(data)>1048576:raise ValueError('row too large')
 c=zlib.compressobj(6,zlib.DEFLATED,zlib.MAX_WBITS,zdict=DICTIONARY)
 return b'FSD2'+c.compress(data)+c.flush()
def decode_payload(raw,max_bytes=1048576):
 if isinstance(raw,str):
  data=raw.encode()
 elif isinstance(raw,(bytes,bytearray,memoryview)):
  raw=bytes(raw)
  if raw.startswith(b'FSD2'):d=zlib.decompressobj(zdict=DICTIONARY);raw=raw[4:]
  else:d=zlib.decompressobj()
  data=d.decompress(raw,max_bytes+1)
  if len(data)>max_bytes or d.unconsumed_tail or d.unused_data or not d.eof:raise ValueError('invalid or oversized compressed row')
 else:raise ValueError('unsupported row payload')
 if len(data)>max_bytes:raise ValueError('row too large')
 row=json.loads(data)
 if not isinstance(row,dict):raise ValueError('row must be object')
 return row

BLOCK_ENCODING='zlib-json-blocks/v2'
MAX_BLOCK_BYTES=8*1024*1024
MAX_BLOCK_ROWS=500
def encode_block(rows):
 if not isinstance(rows,list) or not 0<len(rows)<=MAX_BLOCK_ROWS or any(not isinstance(r,dict) for r in rows):raise ValueError('invalid block rows')
 raw=json.dumps(rows,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
 if len(raw)>MAX_BLOCK_BYTES:raise ValueError('block too large')
 return b'FSB2'+zlib.compress(raw,3)
def decode_block(payload):
 if not isinstance(payload,(bytes,bytearray,memoryview)):raise ValueError('invalid block encoding')
 payload=bytes(payload)
 if not payload.startswith(b'FSB2'):raise ValueError('invalid block version')
 d=zlib.decompressobj();raw=d.decompress(payload[4:],MAX_BLOCK_BYTES+1)
 if len(raw)>MAX_BLOCK_BYTES or d.unused_data or d.unconsumed_tail or not d.eof:raise ValueError('invalid or oversized block')
 rows=json.loads(raw)
 if not isinstance(rows,list) or not 0<len(rows)<=MAX_BLOCK_ROWS or any(not isinstance(r,dict) for r in rows):raise ValueError('invalid block rows')
 return rows
