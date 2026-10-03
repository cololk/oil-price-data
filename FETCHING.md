# Brent quote refresh

`python3 scripts/fetch_brent.py` uses Python's standard library. Run offline tests
with `python3 -m unittest discover -s tests -v`.

## Data and routes

Only Yahoo Finance's USD Brent futures symbol `BZ=F` is accepted. WTI (`CL=F`)
plus a fixed spread is not a Brent quote and is never used. This is a futures
quote, not Brent spot prices or a guaranteed real-time exchange feed.

The fetcher tries Yahoo query1 directly, Yahoo query2 directly, then the same
Yahoo Brent endpoint through AllOrigins. These are **three connection routes
to one data provider**, not independent sources. The proxy is last, so its
failure no longer prevents a working direct request. A provider-wide outage
can still cause a failed refresh. No credentials or paid service are required.

Each route has a 20-second socket timeout and at most two attempts. Transient
network errors, invalid outer JSON, HTTP 408/429/5xx and proxy upstream 408/429/5xx
receive a two-second backoff before retrying. Permanent HTTP failures and
invalid/stale quotes move to the next route. The Actions job has a five-minute
limit. JSON logs record route, attempt, elapsed time, HTTP status, upstream HTTP
status where available, exception type and a safe reason code. URLs, response
bodies, request headers and arbitrary exception messages are not logged.

Prices must be finite and positive, the symbol/currency must match, and Yahoo's
market timestamp must exist, be no more than five minutes in the future and no
older than 96 hours. The 96-hour tolerance allows weekends and common three-day
holidays; it is not a claim that the quote is live. Quotes cannot replace a
previously verified quote with an older market timestamp. `prev` is the reported
previous close or the last prior exchange-local day's bar (null if unavailable).
It is never the close from before the entire requested chart range.

## JSON compatibility and freshness

Existing `price`, `prev`, `updated`, `date`, and `source` fields remain.
`source` stays `github-actions`. Added fields are:

- `provider`, `symbol`, `currency`, `route`: actual provenance.
- `quote_time`: Yahoo's market timestamp in UTC; use this to assess quote age.
- `updated`, `date`: when a changed verified quote was successfully collected;
  an unchanged quote does not advance these fields.
- `fetched_at`: latest successful retrieval time, even if the quote was unchanged.
- `checked_at`: latest completed refresh attempt.
- `stale`: true if the latest refresh failed; false only after validation succeeds.
- `error`: safe failure code, present only when the latest refresh failed.

On failure, any readable valid cached quote keeps its original price, previous
close, timestamps and provenance. Only `stale`, `checked_at`, and `error` change.
Legacy cached data may have used the old WTI estimate; a failed refresh must not
relabel it as verified Brent. A missing or invalid cache is not replaced with a
fake quote. `brent-status.json` always records a completed refresh's status and
whether a usable cached quote exists, including when no quote can be published.
Files are replaced atomically; disk or unexpected programming errors still fail
the command and are visible in the Actions log.

The workflow commits data/status after a handled fetch failure but **retains the
failed job result and notifications**. Offline test failure prevents fetching
and publishing. Cancellation skips publication. Concurrent runs are serialized;
push conflicts are surfaced rather than force-pushed. No other workflows should
write these files without coordinating with this concurrency group.

Existing consumers can continue reading the original fields, but consumers
that ignore the new status fields will not display the explicit stale warning.
They should check `brent-status.json`, `stale`, and the age of `quote_time` (or
legacy `updated`) when displaying prices. No consumer implementation was
available in this repository to update.

## Rollout validation

After review and publication, inspect the next scheduled run (or an explicitly
authorized manual run). Confirm `fetch_ok`, `symbol: BZ=F`, a plausible current
`quote_time`, successful data/status commit and the published Pages JSON.
Both success and failure paths are covered offline; public endpoint reachability
must also be checked on the GitHub runner. Do not use workflow reruns to mask
provider errors or disable failure notifications.
