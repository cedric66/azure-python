# CVE / Prisma vulnerability report

_[← Back to the README](../README.md)_

Enrich a Prisma export (or a CVE list) with NVD/KEV/EPSS and classify findings by layer.

## CVE / Prisma Vulnerability Report

Use `vulnerabilities` when you have a Prisma vulnerability export in `.xlsx`
format or a simple CVE list and want an Excel workbook that separates likely
base-image, application dependency, and platform/runtime-framework ownership.

```bash
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules examples/vulnerability_classification.example.json
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules classification-rules/ --offline
uv run python aks_report.py vulnerabilities --cves cves.txt
```

Inputs:

- Prisma report: `.xlsx`. If `--sheet` is omitted, all sheets are scanned.
- CVE list: `.txt`, `.csv`, `.json`, or `.xlsx`.
- Classification rule files: optional JSON files or a directory of JSON files
  through `--classification-rules` / `--rules`. These are not Azure Policy.
  They are local override rules that teach the script your ownership model
  when Prisma context is ambiguous. The supplied
  `examples/vulnerability_classification.example.json` shows the schema.
- Internet enrichment: NVD CVE 2.0, CISA KEV, and EPSS. Use `--offline` when
  running without internet; the report will classify from Prisma context and
  local classification rules only.

Prisma email/XLSX headers currently handled include: `registry`, `repository`,
`tag`, `id`, `distro`, `hostname`, `cve id`, `compliance`, `result`, `type`,
`severity`, `packages`, `package version`, `package license`, `cvss`,
`fix status`, `risk factors`, `cause`, `published`, `image id`,
`vulnerability link`, and `purl`.

Layer definitions:

- `base_image`: OS/base-image packages such as OpenSSL/glibc/curl from
  `deb`, `rpm`, or `apk` package managers.
- `application`: application/library dependencies such as npm, Maven, pip,
  NuGet, gem, Composer, or Cargo packages.
- `platform`: application runtime/framework layer such as Java/OpenJDK,
  Node.js, Python, .NET, Tomcat, or Spring Boot.

Classification is deterministic and precedence-ordered (first match wins):
JSON rule → runtime/framework by package name → package type → NVD CPE part →
OS-shaped path/PURL → unknown. A runtime delivered even as an OS package (a JRE
installed via `apt`) is `platform`, not `base_image`. A populated OS/Distro
column is **not** by itself treated as base-image evidence — Prisma exports
carry it on every row, so the package type is trusted instead. Every
`Classification` row records the deciding rule in a `signal` column
(`json_rule`, `runtime_name`, `pkgtype_os`, `pkgtype_app`, `cpe_o`, `cpe_a`,
`distro_path`, `app_path`, `none`) and flags `unknown`/low-confidence rows with
`needs_review = REVIEW` so you can audit and triage the verdicts. The `Summary`
section also includes a `BySeverity` tab — a severity × layer grid with a
clustered bar chart — and `ByLayer` carries a findings-by-layer bar chart.

Example rule:

```json
{
  "classification_rules": [
    {
      "name": "Java runtimes are platform",
      "layer": "platform",
      "match": { "package": ["java", "openjdk", "jdk", "jre"] }
    }
  ]
}
```
