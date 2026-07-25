# Just Press Record — iCloud filename timezone bug

**To:** Open Planet Software (Just Press Record)
**Date:** 2026-07-25
**Reporter:** Alex Gostev (macOS daemon user)
**Severity:** Low (cosmetic, but breaks downstream automation that relies on filenames as timestamps)

## Summary

Just Press Record names iCloud-synced `.m4a` files using CET (UTC+1) year-round, ignoring the user's local DST. In regions that observe DST (e.g. Norway, which uses CEST / UTC+2 in summer), filenames are 1 hour behind the actual local recording time during summer months.

## Reproduction

1. macOS system timezone set to **Europe/Oslo** (CEST in July).
2. Start a Just Press Record recording at **14:08 local time** on 2026-07-23.
3. Recording saves to `~/Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-07-23/13-08-14.m4a`.

**Observed:** filename hour is `13`, not `14`.
**Expected:** filename hour matches the local wall-clock time of recording (`14`), or the filename carries an explicit timezone marker (e.g. `13-08-14+01.m4a`) so consumers can convert correctly.

## Evidence

```
$ date -u
Fri 25 Jul 2026 09:18:30 UTC

$ date
Fri 25 Jul 2026 11:18:30 CEST

# Recording made at 14:08 local on 2026-07-23:
$ ls ~/Library/Mobile\ Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-07-23/
13-08-14.m4a   ← 13:08 CET (= 14:08 CEST)
```

The 1-hour skew is consistent with CET (UTC+1) being used instead of Europe/Oslo (CEST in July = UTC+2). In winter (CET = UTC+1, no DST), the filename matches local time and the bug is invisible.

## Impact

Downstream consumers that treat the JPR filename as a naive local-time timestamp inherit the 1-hour skew during summer. Example: my SuperwhisperObsidianWF pipeline writes meeting-analysis Markdown files named `YY-MM-DD HH.MM - <title>.md`, where `HH.MM` is derived from the JPR filename. In summer, every meeting's analysis file is named 1 hour behind the actual meeting time — making the vault harder to navigate by chronology and breaking any automation that uses the timestamp prefix as a meeting-time reference.

The fix on my side was to parse the JPR filename as CET (UTC+1 fixed) and convert to my local timezone — but every downstream consumer has to do this dance. A fix at the source would be cleaner.

## Suggested fix

Two options, in order of preference:

1. **Use the user's local timezone** (whatever the macOS system tz is at recording time) when naming iCloud-synced files. This matches the file's role as a human-facing artifact and the user's mental model of "this meeting was at 14:08".
2. **Keep CET but make it explicit** — append a timezone suffix to the filename (e.g. `13-08-14+01.m4a` or `13-08-14CET.m4a`) so consumers can parse it correctly without guessing.

Option 1 is preferable because it eliminates the skew entirely and makes the filename self-describing for the user's local context.

## Workaround (consumer-side)

For any consumer that parses JPR filenames:

```python
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JPR_FIXED_CET = timezone(timedelta(hours=1))  # JPR uses UTC+1 year-round
OSLO_TZ = ZoneInfo("Europe/Oslo")            # user's local tz

naive_jpr = datetime(2026, 7, 23, 13, 8, 14)  # parsed from filename
local_time = naive_jpr.replace(tzinfo=JPR_FIXED_CET).astimezone(OSLO_TZ).replace(tzinfo=None)
# → 14:08:30 in summer, 13:08 in winter
```

This works but requires every consumer to know JPR's internal convention. Documenting the convention in the JPR help/docs would also help, even without a code change.

## Related

- File naming convention observed: `YYYY-MM-DD/HH-MM-SS.m4a` under `iCloud~com~openplanetsoftware~just-press-record/Documents/`
- Folder name (date) appears correct (matches recording date); only the time component is UTC+1-fixed.