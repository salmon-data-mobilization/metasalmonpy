"""Bounded public KNB/DataONE metadata capture; no annotation or data submission.

A capture is a receipt of a query, not a Salmon Data Package or a catalogue.
Use existing package creation/review tooling separately after human inspection.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from .knb_environments import knb_config

_FIELDS = 'id,title,formatId,formatType,authoritativeMN,obsoletes,obsoletedBy,dateUploaded,dateModified,beginDate,endDate,resourceMap'

def _capture_failure_warning(message):
    # Diagnostics must not replace the capture error, even with warnings-as-errors.
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except BaseException:
        pass

def _write_capture_receipt(path, receipt):
    # Install the success marker only after its complete contents are written.
    # The temporary file stays within the exclusively reserved capture directory.
    descriptor, temporary = tempfile.mkstemp(prefix='.capture-', suffix='.json', dir=path.parent)
    temporary = Path(temporary)
    try:
        os.close(descriptor)
        temporary.write_text(json.dumps(receipt, indent=2)+'\n')
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except BaseException:
            _capture_failure_warning('Could not remove a temporary receipt; inspect the reserved output directories')
        raise

def _public_get(url, timeout, max_bytes):
    req=Request(url,headers={'User-Agent':'MetaSalmon-public-discovery/0.1','Accept':'application/json'})
    # A process-global opener can carry authentication/cookies; environment
    # proxies can carry credentials. Default capture uses a fresh public client.
    # Inject fetch explicitly when integrating custom network routing.
    opener=build_opener(ProxyHandler({}))
    with opener.open(req,timeout=timeout) as response:
        raw=response.read(max_bytes+1)
    if len(raw)>max_bytes:raise ValueError('Catalogue page exceeds max_bytes')
    return raw

def capture_catalogue_query(query, out, *, catalogue='knb', max_records=100,
                            page_size=50, timeout=30, max_bytes=2_000_000,
                            fetch=None, captured_at=None):
    """Capture public metadata query pages and a checksum-bound provenance receipt.

    ``fetch(url, timeout, max_bytes) -> bytes`` is the offline test seam. Output
    must be new; failed evidence moves to an ``.incomplete`` sibling when possible,
    and the original error or keyboard interrupt is re-raised. Catalogues are live
    indexes, not transactional snapshots.
    Metadata versions and repeated repository encounters are not independent data.
    No source objects are downloaded and no semantic decisions are accepted.
    """
    if not isinstance(query,str) or not query.strip() or len(query)>4096:
        raise ValueError('query must be nonblank text of at most 4096 characters')
    if catalogue not in ('knb','dataone'):raise ValueError('catalogue must be knb or dataone')
    for name,value,limit in [('max_records',max_records,1000),('page_size',page_size,100),('max_bytes',max_bytes,10_000_000)]:
        if type(value) is not int or not 1<=value<=limit:raise ValueError(f'{name} must be an integer from 1 to {limit}')
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0<timeout<=120:
        raise ValueError('timeout must be positive and at most 120 seconds')
    stamp=datetime.now(timezone.utc).isoformat() if captured_at is None else captured_at
    if not isinstance(stamp,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})',stamp):
        raise ValueError('captured_at must be a valid ISO timestamp including a timezone')
    # Python 3.14 accepts 24:00:00 as the next day; the capture contract uses
    # an explicit Gregorian date with time fields in their ordinary ranges.
    if int(stamp[11:13])>23 or int(stamp[14:16])>59 or int(stamp[17:19])>59:
        raise ValueError('captured_at must be a valid ISO timestamp including a timezone')
    # fromisoformat normalizes +02:60 to +03:00. Reject invalid offset fields
    # before parsing, matching the R capture contract and preserving the input.
    if not stamp.endswith('Z') and (int(stamp[-5:-3])>23 or int(stamp[-2:])>59):
        raise ValueError('captured_at must be a valid ISO timestamp including a timezone')
    # Validate calendar/timezone without parsing fractional precision: supported
    # Python 3.9 only accepts some fraction widths. Preserve the input receipt
    # timestamp exactly, as the R adapter does after validating its first 19 bytes.
    validation_stamp = stamp[:19] + ('Z' if stamp.endswith('Z') else stamp[-6:])
    try:
        parsed_stamp=datetime.fromisoformat(validation_stamp.replace('Z','+00:00'))
    except ValueError as error:
        raise ValueError('captured_at must be a valid ISO timestamp including a timezone') from error
    if parsed_stamp.tzinfo is None:
        raise ValueError('captured_at must be a valid ISO timestamp including a timezone')
    if fetch is not None and not callable(fetch):raise ValueError('fetch must be callable or None')
    config=knb_config('production')
    endpoint=(str(config['mn_endpoint'])+'/query/solr/' if catalogue=='knb' else str(config['solr_endpoint']))
    out=Path(out)
    if os.path.lexists(out) or os.path.lexists(out.with_name(out.name+'.incomplete')):raise ValueError('Capture destination must be new; existing evidence is never overwritten')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.mkdir()  # Atomic reservation; racing captures cannot replace one another.
    staging=out
    transport=_public_get if fetch is None else fetch
    pages=[];docs=[];seen=set();total=None
    try:
        while len(docs)<max_records:
            start=len(docs);rows=min(page_size,max_records-start)
            url=endpoint+'?'+urlencode({'q':query,'fq':'formatType:METADATA','fl':_FIELDS,'sort':'id asc','start':start,'rows':rows,'wt':'json'})
            raw=transport(url,timeout,max_bytes)
            if not isinstance(raw,bytes) or len(raw)>max_bytes:raise ValueError('fetch must return bounded bytes')
            name=f'page-{len(pages):04d}.json';(staging/name).write_bytes(raw)
            page=json.loads(raw)['response'];found=page['numFound'];items=page['docs']
            whole=lambda value: type(value) in (int,float) and 0<=value<=9_007_199_254_740_991 and value==int(value)
            if not whole(found) or not whole(page.get('start')) or page['start']!=start or not isinstance(items,list) or len(items)>rows:
                raise ValueError('Malformed catalogue response or unexpected paging')
            found=int(found)
            if total is not None and found!=total:raise ValueError('Catalogue changed during capture; rerun as a new capture')
            total=found
            if start+len(items)>total:raise ValueError('Page exceeds reported catalogue count')
            for item in items:
                pid=item.get('id') if isinstance(item,dict) else None
                if not isinstance(pid,str) or not pid or pid in seen:raise ValueError('Missing or repeated metadata PID; unstable capture')
                seen.add(pid)
            pages.append({'file':name,'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'start':start,'rows':len(items)})
            docs.extend(items)
            if len(docs)>=total:break
            if not items:raise ValueError('Empty page before reported result end')
        receipt={'receipt_version':'0.1','catalogue':catalogue,'endpoint':endpoint,'query':query,
                 'captured_at':stamp,'reported_metadata_matches':total,'captured_metadata_records':len(docs),
                 'complete_for_reported_count':len(docs)==total,'transactional_snapshot':False,
                 'annotation_status':'pending','independent_dataset_count':None,'pages':pages,'records':docs}
        _write_capture_receipt(staging/'capture.json', receipt)
        # capture.json is the success marker in the exclusively reserved directory.
        return receipt
    except (Exception, KeyboardInterrupt):
        incomplete=out.with_name(out.name+'.incomplete')
        # Failure bookkeeping is best effort. Keep the original transport/parser/
        # filesystem exception and any raw evidence if these operations also fail.
        try:
            try:
                (staging/'failure.json').write_text(json.dumps({'status':'incomplete','query':query,'catalogue':catalogue,'pages':pages,'semantic_approval':'pending'})+'\n')
            except BaseException:
                _capture_failure_warning('Could not write the failure receipt; raw evidence remains in the reserved output directories')
            try:
                incomplete.mkdir()  # Reserve without replacing a competing failure capture.
            except FileExistsError:
                pass  # Keep failed evidence in the original reserved destination.
            else:
                for artifact in staging.iterdir():
                    try:
                        os.rename(artifact,incomplete/artifact.name)
                    except BaseException:
                        _capture_failure_warning('Could not move failed evidence; inspect both reserved output directories')
                staging.rmdir()
        except BaseException:
            _capture_failure_warning('Could not finish failure bookkeeping; inspect both reserved output directories')
        raise
