# ClawCU v0.2.12

🌐 Language:
[English](RELEASE_v0.2.12.md) | [中文](RELEASE_v0.2.12.zh-CN.md)

Release Date: April 22, 2026

> `v0.2.12` tightens the day-to-day `list` workflow: you can now bypass the daily Available Versions cache with `--no-cache` when you need a fresh registry read right now, and older instance records with extra legacy fields no longer break `clawcu list`.

* * *
## Highlights

- **`clawcu list --no-cache`**
  - Forces a fresh Available Versions fetch for the human footer.
  - Leaves the main instance table behavior unchanged.
  - Still refreshes the on-disk cache after a successful fetch, so the next plain `list` is warm again.

- **Legacy record compatibility**
  - Older managed-instance JSON files may carry fields that newer code no longer models directly.
  - `InstanceRecord.from_dict()` now ignores unknown keys instead of failing hard.
  - Real-world effect: `clawcu list`, `inspect`, and related flows can keep reading older state instead of dying on deserialization.

- **Documentation alignment**
  - README and usage docs now describe the real `config` mapping correctly: OpenClaw uses `configure`, Hermes uses `setup`.
  - The `list` docs now include the explicit cache-bypass path.

- **396 tests** at release time, with dedicated coverage for `list --no-cache` and legacy record loading.

* * *
## Why `--no-cache` matters

The daily cache introduced in `v0.2.10` made `clawcu list` fast and predictable, which was the right default. But once that cache existed, there was still one missing operational move: "I know the registry changed; show me the fresh answer now."

That is what `v0.2.12` adds:

```bash
clawcu list --no-cache
```

This does not disable the cache globally. It simply tells this invocation to skip reading today's cache entry, fetch fresh tags, then write the successful result back so the next ordinary `clawcu list` benefits from it.

So the UX becomes:

- fast by default;
- fresh on demand;
- still cache-warm after the manual refresh.

* * *
## Why the legacy-record fix matters

One rough edge showed up during real command validation: older instance records could still contain fields such as `a2a_enabled`. Those fields were harmless historically, but newer code no longer modeled them directly. The result was worse than it should have been: `clawcu list` could fail before it even rendered the table.

`v0.2.12` changes the deserialization rule from "every key must still exist in the dataclass" to "known fields are loaded, unknown historical fields are ignored."

That is a better contract for local lifecycle state:

- old records should stay readable;
- forward cleanup of the schema should not strand users;
- the CLI should degrade gracefully when it encounters historical baggage.

* * *
## Compatibility

`v0.2.12` is a drop-in upgrade from `v0.2.11`.

- No breaking CLI changes.
- `clawcu list` keeps its default cached behavior.
- `--no-cache` is additive and optional.
- Existing old records with extra keys are now more likely to work without manual cleanup.

* * *
## Recommended Workflow

Use the default path when you just want a fast operational snapshot:

```bash
clawcu list
```

Use the explicit refresh path when you care about the freshest registry state:

```bash
clawcu list --no-cache
```

Then continue with the normal lifecycle flow:

```bash
clawcu inspect writer
clawcu upgrade writer --version 2026.4.21
```

* * *
## Closing Note

`v0.2.11` made runtime image overrides durable across the lifecycle. `v0.2.12` is a smaller release, but it lands in the same spirit: the default path stays fast, the explicit path stays available when you need it, and old local state is treated with a little more respect.

Next up for `0.3.0`: the unified `--output {table|json|yaml}` protocol, provider bundle provenance, and promoting active-provider to a first-class field.
