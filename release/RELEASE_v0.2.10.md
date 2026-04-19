# ClawCU v0.2.10

🌐 Language:
[English](RELEASE_v0.2.10.md) | [中文](RELEASE_v0.2.10.zh-CN.md)

Release Date: April 19, 2026

> `v0.2.10` is a focused polish over `v0.2.9`: the `clawcu list` "Available versions" footer now caches for the day instead of hitting the registry on every invocation, and when the registry is unreachable the footer falls back to local Docker images so you still see something actionable.

* * *
## Highlights

- **Daily cache for Available versions**
  - Successful registry fetches are cached at `<clawcu_home>/cache/available_versions.json`, keyed on service + `image_repo`.
  - Subsequent `clawcu list` calls on the same local calendar day are served from cache — no network round-trip.
  - The cache auto-expires when the local date rolls, or when `image_repo` changes (e.g. via `clawcu setup`).
  - Failures are **never** cached, so a transient outage does not stick for 24 hours.

- **Offline fallback to local Docker images**
  - When the registry fetch fails (network down, DNS, auth) or `--no-remote` is set, the footer now queries `docker image ls <repo>` and surfaces those tags on a continuation line under the error.
  - Prereleases (`-beta`, `-rc`, `-alpha`) and `latest` are filtered to match the remote-versions policy.
  - The red error line still prints, so the user knows *why* the remote list is empty — but now there's a usable fallback next to it.

- **366 tests** (up from 360 at `v0.2.9`), covering the cache hit / cross-day refetch / image_repo-change invalidation / failure-not-cached paths, plus the two local-fallback branches.

* * *
## Why cache for the day

`clawcu list` is a command people run many times a day — "what's running, what's where". In `v0.2.9` we bolted on the Available-versions footer so users could *also* see upgrade candidates from the same place, which was a big UX win. But it came with a cost: every invocation made two network calls to the image registries (one per service), each paying DNS + TLS + HTTP roundtrip.

On a fast connection it's a few hundred ms. On a slow one, it's noticeable. On a flaky one, it's "why is `list` laggy today?".

Day-level granularity matches how often users actually care about new versions. Nobody ships a patch release and expects their users to see it within the hour. A once-per-day refetch is the right trade between freshness and speed:

```
$ time clawcu list              # first call today — hits registry
real    0m2.412s

$ time clawcu list              # second call — served from cache
real    0m0.198s
```

### Cache invalidation rules

The cache is keyed on **`(service, image_repo)`**, not just `service`. This matters because users can change their configured image repo via `clawcu setup` — and when they do, the old cache entry for that service must not be served. The check is dead-simple: if `cached_entry.image_repo != current_image_repo`, refetch.

The cache entry also carries `fetched_date`. If it's not today's local date, refetch.

Nothing else. No explicit invalidation knob, no TTL in seconds, no "force refresh" flag. The `--no-remote` flag still bypasses the whole path for deterministic offline renders.

* * *
## Why local-images fallback

In `v0.2.9` the footer had exactly one failure mode: a red error line saying "couldn't reach the registry", and that was it. The user got no information.

```
$ clawcu list
... [instance table]

Available versions (top 10 by semver, newest first)
  openclaw  Network is unreachable
  hermes    Network is unreachable
```

That's technically correct but practically useless. The user's laptop has a bunch of Docker images locally — that's where `clawcu upgrade` pulls from when the image already exists. Those are perfectly valid upgrade candidates *right now*, without any network.

`v0.2.10` surfaces them:

```
$ clawcu list               # registry unreachable
... [instance table]

Available versions (top 10 by semver, newest first)
  openclaw  Network is unreachable
            local images: 2026.4.12, 2026.4.10, 2026.4.8, 2026.4.5
  hermes    Network is unreachable
            local images: 2026.4.13
```

### Why both, why on failure

- The remote error **stays**: the user needs to know that what they're seeing is the offline fallback, not the authoritative view. A silent degrade would mask a real problem ("my upgrade list stopped growing!").
- The local fallback **only kicks in when remote didn't produce versions**: if the registry is healthy, we show the real thing. We don't bother querying docker locally in the success case — cheaper and avoids noise.
- `--no-remote` **also shows local images**: a user who explicitly went offline still has state they care about on disk; showing it beats showing nothing.
- Filtering matches the remote policy: no prereleases, no `latest`. The point is an "install candidates" surface, and `latest` is a floating tag that makes no sense in a version-comparison list.

* * *
## Compatibility

`v0.2.10` is a drop-in upgrade from `v0.2.9`.

- No breaking CLI changes.
- The `list --json` payload is unchanged in its instance-array contract; the versions footer remains text-mode only.
- `list_service_available_versions` (service-layer API) gains a new `local_versions` key on each entry — additive, existing consumers keep working.
- On first run after upgrading, the cache file doesn't exist yet; the first `list` does one fresh fetch and seeds the cache. Normal.

* * *
## Recommended Workflow

Unchanged from `v0.2.9`:

- Upgrade on a clone first, promote only if the clone holds.
- Snapshots before every upgrade; `rollback` restores from real backups.
- `list --removed` → `recreate <orphan>` for orphan recovery.

`v0.2.10` just makes the default list path faster on the hot path and more honest on the cold path.

* * *
## Closing Note

`v0.2.9` added "Available versions" because seeing upgrade candidates at a glance felt like a real-world need. `v0.2.10` closes the gap between "feature exists" and "feature feels right": no tax on every invocation, and something useful to say even when the registry is down.

Next up for `0.3.0`: the unified `--output {table|json|yaml}` protocol, provider bundle provenance, and promoting active-provider to a first-class field.
