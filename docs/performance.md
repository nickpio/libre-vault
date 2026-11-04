# Vault latency budgets

Run `./check.sh --script scripts/benchmark_vault.py`. The probe creates isolated
10,000- and 50,000-note vaults and disposable indexes, then removes them. Each
fixture contains nested folders, 1–3 KiB UTF-8 notes, YAML properties, aliases,
nested tags, wiki links, and both common and rare search terms. The payloads are
approximately 18 and 91 MiB. This is a reproducible baseline; it does not model
network filesystems, multi-megabyte individual notes, or every real vault shape.

The probe exits unsuccessfully if a budget is exceeded. Run it without other
heavy jobs when comparing machines or commits. Cold open means a new derived
index, not a cold OS disk cache. Timings include worker queueing, opening its
SQLite connection, execution, and delivering the result to the main loop.

| Metric | Budget |
| --- | --- |
| Worker submission on the UI thread | 50 ms |
| Main-loop heartbeat p99 interval (10 ms timer) | 50 ms |
| Maximum heartbeat interval | 250 ms |
| Cancel request through next queued result | 250 ms |
| Cold indexing | 10 s + 8 ms per note |
| Warm full reconciliation | 5 s + 0.5 ms per note |
| Rare substring search | 1 s + 0.1 ms per note |
| Broad substring search | 2 s + 0.3 ms per note |
| Backlinks | 2 s + 0.2 ms per note |
| Unlinked mentions | 2 s + 0.3 ms per note |
| Actual window open or broad-search settling, each | 10 s + 0.8 ms per note |

The probe also opens the actual GTK window, installs monitors, renders the
initial file page, and runs a broad search. It measures heartbeat intervals
through these steps independently from the worker-only timings. It checks that
only 200 rows are instantiated initially. The file tree and search show their
complete results in additional pages through **Show 200 more**. GTK row creation
and destruction yield after approximately 6 ms of work per idle slice.

## Execution and correctness

Each window has one serial worker. Each job constructs and closes its own Vault
and SQLite connection on that worker thread. SQLite WAL allows foreground
editor reads and saves while the worker scans. Background indexing commits
individual notes so it does not hold a vault-wide write transaction across a
scan. Queries use a read transaction for a consistent SQLite snapshot.

Jobs receive copies of note records, paths, options, and open-buffer overrides;
they never receive GTK widgets or the foreground SQLite connection. Progress is
throttled to approximately ten updates per second and displayed beside a
**Cancel work** button. Cancellation is cooperative at directory entries and
notes, and uses SQLite's progress handler during SQL execution. A single
filesystem operation or parsing one unusually large note can still delay
cancellation; there is no claim of interrupting blocked kernel I/O.

A newer request cancels the previous request in that channel. The UI callback
checks request identity again after dispatch, rejecting results and errors that
were already queued when cancellation happened. Vault replacement, close, and
window destruction invalidate pending work. A foreground save invalidates an
inflight snapshot and triggers reconciliation again. Links also validate the
active pane, details view, and open-buffer contents before displaying mentions.
A filesystem refresh invalidates pending searches, links, and tag counts before
publishing its new snapshot and rerunning the views.

Opening leaves the current vault available while scanning. The replacement is
installed only after a successful scan and dirty-editor confirmation. Cancelling
an initial open leaves the welcome screen available. Cancelling a refresh may
leave partially updated *derived* SQLite rows; a subsequent refresh reconciles
them against disk. It never changes Markdown files. Editor save/conflict checks,
single-note rendering, and bulk organization operations still run on the UI
thread and are outside these vault-scan budgets.

## Recorded baseline

[Raw measurements](performance-results.json) were captured on an AMD Ryzen 5
3600 (6 cores), Linux 7.3.1, Python 3.14.9, with GTK windows on the background test
workspace. Both vault sizes passed the operation, submission, cancellation,
worker-heartbeat, and GTK-heartbeat budgets. The JSON retains every timing and
records slow intervals by phase plus the longest garbage-collection pause.
Rerun on deployment hardware before treating these limits as a release gate.
