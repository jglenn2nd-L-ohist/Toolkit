#!/usr/bin/env python3
"""
jsa_backfill.py — re-apply the CURRENT rules to jobs already in jsa_jobs.json.

Rule changes only affect new harvests; this cleans what's already stored.

  python jsa_backfill.py            # dry run: prints what would change, writes nothing
  python jsa_backfill.py --apply    # backs up locally, then writes

Only touches jobs whose status is New, Review, or blank.
Applied / Interviewing / Offer / Rejected / Closed are never modified.
Rejected jobs move to jsa_rejects.json with their reason codes (nothing is deleted).
"""

import os, sys, json, time, urllib.error
from datetime import datetime, timezone
import jsa_filters, jsa_enrich, jsa_store

OPEN_STATUSES = {'New', 'Review', '', None}


def main():
    apply = '--apply' in sys.argv
    token = os.environ.get('JSA_GH_TOKEN')
    if not token:
        sys.exit('Set JSA_GH_TOKEN first.')

    data, _ = jsa_store.read(token, jsa_store.JOBS_FILE, {'jobs': []})
    jobs = data.get('jobs', [])
    targets = [j for j in jobs if j.get('status') in OPEN_STATUSES]
    print(f'{len(jobs)} jobs in file; {len(targets)} open (New/Review) will be re-checked '
          f'with rules v{jsa_filters.FILTER_VERSION}\n')

    verdicts, records = {}, []
    for n, j in enumerate(targets, 1):
        probe = dict(j)
        d = jsa_enrich.enrich(probe) if not j.get('enriched') or '--refetch' in sys.argv else None
        if d:
            jsa_enrich.apply_enrichment(probe, d)
        v = jsa_filters.evaluate(probe, d['jd'] if d else None)
        probe.update(v)
        verdicts[j['id']] = probe
        records.append(probe)
        mark = {'pass': ' ', 'review': '?', 'reject': 'X'}[v['filter_status']]
        print(f"{n:>4} {mark} {probe.get('title','')[:38]:<39}{probe.get('company','')[:20]:<21}"
              f"{(probe.get('location') or '-')[:20]:<21}{','.join(v['filter_reasons'])[:60]}")

    by_src, reasons = jsa_store.summarize(records)
    jsa_store.print_summary(by_src, reasons)

    if not apply:
        print('\nDry run — nothing written. Re-run with --apply to commit these changes.')
        return

    backup = f"jsa_jobs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(backup, 'w') as f:
        json.dump(data, f, indent=2)
    print(f'\nBackup saved: {backup}')

    for attempt in range(4):
        live, sha = jsa_store.read(token, jsa_store.JOBS_FILE, {'jobs': []})
        out, rejects = [], []
        for j in live.get('jobs', []):
            v = verdicts.get(j.get('id'))
            # skip anything the app changed while we were running (e.g. you marked it Applied)
            if not v or j.get('status') not in OPEN_STATUSES:
                out.append(j); continue
            if v['filter_status'] == 'reject':
                rejects.append({k: v.get(k) for k in ('title', 'company', 'location', 'url', 'source',
                                'filter_reasons', 'filter_version', 'dateAdded')})
                continue
            v['status'] = 'New' if v['filter_status'] == 'pass' else 'Review'
            out.append(v)
        live['jobs'] = out
        live['savedAt'] = datetime.now(timezone.utc).isoformat()
        live.setdefault('runStats', []).append({'at': live['savedAt'], 'runner': 'backfill',
            'rules': jsa_filters.FILTER_VERSION, 'bySource': by_src, 'reasons': reasons})
        try:
            jsa_store.write(token, jsa_store.JOBS_FILE, live, sha,
                            f'JSA backfill v{jsa_filters.FILTER_VERSION}: -{len(rejects)} rejected')
            break
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and attempt < 3:
                time.sleep(3); continue
            raise

    if rejects:
        rj, rsha = jsa_store.read(token, jsa_store.REJECTS_FILE, {'rejects': []})
        rj['rejects'] = (rejects + rj.get('rejects', []))[:jsa_store.REJECTS_KEEP]
        jsa_store.write(token, jsa_store.REJECTS_FILE, rj, rsha, 'JSA backfill rejects')
    print(f'Applied: {len(rejects)} moved to rejects, {len(out)} jobs remain.')


if __name__ == '__main__':
    main()
