# Public QEMU runtime consumer fixture

This is a separate Bzlmod root that proves the public opt-in API without
copying the Noble lock. Run from this directory:

```sh
bazel build --lockfile_mode=off //:public_runtime_aliases
```

It analyzes the generated aliases and materializes the intentional 90-package
closure, but it does not execute QEMU or a guest. It is not part of the normal
test suite because repository fetching is deliberately substantial. The root
uses a local override only to validate the current checkout; a real consumer
must pin `rules_ostest` by full Git SHA and either use the exported lock or own
a reviewed lock as documented in `docs/getting-started.md`.
