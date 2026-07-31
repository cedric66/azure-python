# Workload YAML drop (gitignored)

Paste the app team's exported bundle here — a directory, a `.tar.gz`, or a single
multi-doc YAML file:

```
workloads/
  spot-export-prod-20260731.tar.gz   # what examples/export_workloads.sh produces
  team-b/all.yaml                    # multi-doc (--- separated) is fine
```

Then analyse it:

```bash
uv run python aks_report.py workload-spot --bundle workloads/spot-export-prod-20260731.tar.gz
```

This directory is **gitignored** on purpose. Live-cluster exports routinely carry
env vars, internal hostnames, private registry paths and annotations that should
not land in git history. Sanitised copies used by the test suite live in
`tests/fixtures/workloads/` and *are* committed.

## Getting a bundle

Send `examples/export_workloads.sh` to whoever has cluster access. It is
strictly read-only (`kubectl get` only, no mutating verbs, no Secrets or
ConfigMaps), records anything RBAC denies as a `.skipped` file so the analysis
can tell "none exist" from "not allowed to look", and prints a redaction check
before you share the tarball.

```bash
./export_workloads.sh                      # whole cluster
./export_workloads.sh team-a team-b        # named namespaces only
```
