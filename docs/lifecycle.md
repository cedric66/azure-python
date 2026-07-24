# Lifecycle & EOL radars (no Azure access)

_[← Back to the README](../README.md)_

Reports that pull public lifecycle data (endoflife.date, MS Learn, GitHub releases) - no Azure access required.

## Internet Lifecycle Reports (no Azure access)

Two reports scrape public lifecycle sources instead of your subscriptions, so
they need internet access but no Azure credentials and no `subscriptions.csv`.

`container-eol` pulls https://endoflife.date for the base images and runtimes
container estates are usually built on - Alpine, Debian, Red Hat UBI (RHEL
lifecycle), Java (Eclipse Temurin), Python, Node.js - and groups them the way
an architect reviews them:

- `Summary`: one row per product - recommended build target, supported /
  security-only / EOL cycle counts, and the next EOL hit with days remaining.
- `EolRadar`: every live version across all products in one list, soonest EOL
  first, including versions that died in the last 180 days (`--radar-lookback-days`).
- `OsBaseImages` / `LanguageRuntimes`: full lifecycle tables per group.
- `RawLifecycle`: unmodified endoflife.date fields.

Add more endoflife.date products without code changes:

```bash
uv run python aks_report.py container-eol --products ubuntu,golang,dotnet
```

`aks-lifecycle` scrapes the Microsoft pages that announce AKS lifecycle
changes - the Learn supported-versions and integrations pages plus the weekly
`Azure/AKS` GitHub release notes (`--releases` controls the window, default 30):

- `ReleaseCalendar`: AKS preview/GA/EOL dates per Kubernetes minor, community
  and LTS tracks, with computed status (GA, EOL <90 DAYS, LTS ONLY, EOL).
- `Announcements`: retirements, deprecations and GA notices classified from the
  release notes - the rows flagged RETIREMENT/DEPRECATION are your to-do list.
- `GAFeatures` / `PreviewFeatures` / `BehaviorChanges`: what changed in the window.
- `Addons` / `OpenSourceIntegrations`: documented managed add-ons and integrations.
- `BreakingChanges` / `ComponentUpdates` / `RawReleaseNotes`: reference tabs.
