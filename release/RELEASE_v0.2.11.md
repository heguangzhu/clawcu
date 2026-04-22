# ClawCU v0.2.11

🌐 Language:
[English](RELEASE_v0.2.11.md) | [中文](RELEASE_v0.2.11.zh-CN.md)

Release Date: April 22, 2026

> `v0.2.11` teaches ClawCU how to keep a logical service version and a custom runtime image separate: `--version` stays mandatory, `--image` becomes optional on `create` and `upgrade`, and the chosen image now survives later `recreate`, orphan recovery, and `rollback`.

* * *
## Highlights

- **Optional `--image` on `create` and `upgrade`**
  - `--version` remains the required logical version label stored on the instance record.
  - `--image` becomes an explicit runtime override for the Docker artifact that actually starts.
  - When both are supplied, ClawCU records the version from `--version` and runs the image from `--image`.

- **Persisted runtime image chain**
  - The selected `image_tag` is now kept on the managed instance state and in `.clawcu-instance.json`.
  - `retry` and `recreate` reuse that saved image instead of silently recalculating from the version.
  - Recovering an orphaned datadir now restores the same custom image path when metadata is available.

- **Rollback restores the historical image, not just the historical version**
  - Upgrade / rollback history now records `from_image` and `to_image` alongside `from_version` and `to_version`.
  - Rolling back no longer assumes "old version => official default image"; it restores the real runtime artifact that was running before the transition.

- **379 tests** (up from 366 at `v0.2.10`), including custom-image create / same-version image-switch upgrade / recreate reuse / orphan recovery / rollback image restoration coverage.

* * *
## Why split version from image

Users often need two truths at once:

1. the **logical service version** they mean to run (`2026.4.10`);
2. the **actual Docker image** they need in production (`registry.example.com/openclaw:2026.4.10-tools`).

Before `v0.2.11`, ClawCU was strongly version-centric: the version was used both as the user-facing label and as the source of truth for which artifact to run. That worked well for official images, but it made custom runtime layers awkward. A user could add tools or packages to an image, but there was no first-class way to say "this instance is still logically `2026.4.10`, just with my own image build".

`v0.2.11` makes that distinction explicit:

```bash
clawcu create openclaw \
  --name writer-tools \
  --version 2026.4.10 \
  --image registry.example.com/openclaw:2026.4.10-tools
```

The version remains the thing you reason about in `list`, `inspect`, and upgrade history. The image becomes the concrete runtime artifact. That keeps the UX stable while making custom images a supported path instead of a side effect.

* * *
## Why the image chain must persist

Accepting `--image` on `create` or `upgrade` is only useful if later lifecycle commands keep honoring that choice.

Without persistence, a perfectly reasonable flow breaks:

1. create an instance on a custom image;
2. later run `clawcu recreate writer-tools`;
3. silently land back on the default official image because ClawCU recomputed from `version`.

That is exactly the kind of "looked fine until restart day" behavior we want to avoid.

So `v0.2.11` pushes the image override through the whole lifecycle:

- `retry` reuses the saved runtime image for failed creates;
- `recreate` reuses the saved runtime image for managed instances;
- orphan recovery reads the stored `image_tag` from `.clawcu-instance.json` when present;
- `rollback` restores the image recorded on the historical transition, not a freshly derived default.

This turns custom images from a launch-time override into a durable part of instance state.

* * *
## Compatibility

`v0.2.11` is a drop-in upgrade from `v0.2.10`.

- No breaking CLI changes.
- `--version` is still required for `create` and `upgrade`.
- `--image` is additive and optional.
- Existing instances without custom-image history keep working exactly as before.
- Pre-metadata orphan datadirs still recover through `clawcu recreate <orphan> --version <v>`.

The main additive schema change is that instance metadata now carries the runtime image more explicitly, so future lifecycle actions can reconstruct the same artifact chain.

* * *
## Recommended Workflow

For official images, nothing changes:

```bash
clawcu create openclaw --name writer --version 2026.4.10
clawcu upgrade writer --version 2026.4.11
```

For custom runtime images, keep the version explicit and override only the runtime image:

```bash
clawcu upgrade writer-tools \
  --version 2026.4.11 \
  --image registry.example.com/openclaw:2026.4.11-tools
```

After that, `recreate`, orphan recovery, and `rollback` will keep following the recorded image chain automatically.

* * *
## Closing Note

`v0.2.10` made the default `list` path faster and more honest. `v0.2.11` does the same kind of cleanup for custom runtimes: the user intent is now explicit, durable, and reversible across the whole lifecycle.

Next up for `0.3.0`: the unified `--output {table|json|yaml}` protocol, provider bundle provenance, and promoting active-provider to a first-class field.
