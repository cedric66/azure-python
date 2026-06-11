"""subscriptions.csv loading, environment resolution, scope picker, common CLI."""
import argparse
import csv
import datetime as dt
import os
import re
import sys

DEFAULT_PROD_VALUES = {"prod", "production", "prd", "live"}
PROD_VALUES = set(DEFAULT_PROD_VALUES)
DEFAULT_ENV_TAG_KEYS = ["environment", "env", "stage"]
DEFAULT_ENV_CODE_MAP = {
    "d": "dev",
    "s": "sit",
    "r": "dr",
    "p": "prod",
    "u": "uat",
    "q": "qa",
    "t": "test",
}
ENV_CODE_MAP = dict(DEFAULT_ENV_CODE_MAP)
ENV_NAME_INFERENCE = True
CLUSTER_FILTER = {"exact": set(), "prefix": set(), "contains": set()}
PROMPT_CLUSTER_FILTER = False

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
TOKEN_RE = re.compile(r"[^a-z0-9]+")
ENV_WORDS = {
    "dev": "dev",
    "development": "dev",
    "sit": "sit",
    "uat": "uat",
    "qa": "qa",
    "test": "test",
    "tst": "test",
    "stage": "stage",
    "stg": "stage",
    "dr": "dr",
    "prod": "prod",
    "production": "prod",
    "prd": "prod",
    "live": "prod",
}


def load_subscriptions(path):
    if not os.path.exists(path):
        sys.exit("Input file not found: %s\nCreate it from the subscriptions.csv template "
                 "(columns: subscription_id,subscription_name,environment,include)." % path)
    subs, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            sid = (row.get("subscription_id") or "").strip().lower()
            if not sid:
                continue
            if not GUID_RE.match(sid):
                sys.exit("%s line %d: '%s' is not a subscription GUID" % (path, i, sid))
            if sid in seen:
                print("WARNING: duplicate subscription %s in %s (line %d), skipping" % (sid, path, i))
                continue
            seen.add(sid)
            inc = (row.get("include") or "y").strip().lower()
            subs.append({
                "subscription_id": sid,
                "subscription_name": (row.get("subscription_name") or "").strip(),
                "environment": norm_env(row.get("environment")),
                "include": inc in ("y", "yes", "true", "1"),
            })
    active = [s for s in subs if s["include"]]
    if not active:
        sys.exit("No subscriptions with include=Y in %s" % path)
    return active


def norm_env(v):
    return (v or "").strip().lower()


def is_prod(env):
    return norm_env(env) in PROD_VALUES


def parse_kv_map(raw, default):
    if raw is None:
        return dict(default)
    out = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            sys.exit("Expected key=value in map option, got: %s" % part)
        k, v = part.split("=", 1)
        k, v = norm_env(k), norm_env(v)
        if k and v:
            out[k] = v
    return out or dict(default)


def parse_prod_values(raw):
    vals = {norm_env(v) for v in str(raw or "").split(",") if norm_env(v)}
    return vals or set(DEFAULT_PROD_VALUES)


def _split_csv(raw):
    return [x.strip().lower() for x in str(raw or "").split(",") if x.strip()]


def cluster_filter_from_args(args):
    return {
        "exact": set(_split_csv(getattr(args, "cluster", None))
                     + _split_csv(getattr(args, "cluster_id", None))),
        "prefix": set(_split_csv(getattr(args, "cluster_prefix", None))),
        "contains": set(_split_csv(getattr(args, "cluster_contains", None))),
    }


def should_prompt_scope(args):
    return not any(getattr(args, name, None) for name in (
        "all", "subs", "env", "nonprod", "cluster", "cluster_id",
        "cluster_prefix", "cluster_contains", "resource_group",
    ))


def configure_scope_runtime(args):
    """Set per-process scope options from common CLI arguments."""
    global PROD_VALUES, ENV_CODE_MAP, ENV_NAME_INFERENCE
    global CLUSTER_FILTER, PROMPT_CLUSTER_FILTER
    PROD_VALUES = parse_prod_values(getattr(args, "prod_values", None))
    ENV_CODE_MAP = parse_kv_map(getattr(args, "env_code_map", None), DEFAULT_ENV_CODE_MAP)
    ENV_NAME_INFERENCE = not getattr(args, "no_name_env", False)
    CLUSTER_FILTER = cluster_filter_from_args(args)
    PROMPT_CLUSTER_FILTER = should_prompt_scope(args)


def infer_env_from_name(*names):
    """Infer env from name tokens: aks-dev-01, aks-s-01, app-r01, etc."""
    if not ENV_NAME_INFERENCE:
        return ""
    words = sorted(ENV_WORDS, key=len, reverse=True)
    for name in names:
        text = norm_env(name)
        if not text:
            continue
        tokens = [t for t in TOKEN_RE.split(text) if t]
        for token in tokens:
            if token in ENV_WORDS:
                return ENV_WORDS[token]
            for word in words:
                if token.startswith(word) and token[len(word):].isdigit():
                    return ENV_WORDS[word]
            if token[0] in ENV_CODE_MAP and (len(token) == 1 or token[1:].isdigit()):
                return ENV_CODE_MAP[token[0]]
    return ""


def resolve_env_detail(cluster_tags, rg_tags, sub_env, keys=None, names=()):
    """Environment precedence: cluster tags -> RG tags -> names -> CSV value."""
    keys = keys or DEFAULT_ENV_TAG_KEYS
    for label, src in (("cluster_tag", cluster_tags or {}), ("resource_group_tag", rg_tags or {})):
        low = {str(k).strip().lower(): str(v) for k, v in src.items() if v is not None}
        for k in keys:
            if low.get(k):
                return norm_env(low[k]), "%s:%s" % (label, k)
    inferred = infer_env_from_name(*names)
    if inferred:
        return inferred, "name"
    if norm_env(sub_env):
        return norm_env(sub_env), "subscription_csv"
    return "", ""


def resolve_env(cluster_tags, rg_tags, sub_env, keys=None, names=()):
    env, _src = resolve_env_detail(cluster_tags, rg_tags, sub_env, keys, names)
    return env


def env_match(env, env_filter, include_unknown=False):
    if env_filter is None:
        return True
    e = norm_env(env)
    if env_filter == "nonprod":
        if not e or e == "(unknown)":
            return include_unknown
        return not is_prod(e)
    return e == env_filter


def parse_selection(raw, count):
    idx, tokens = set(), set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and all(x.strip().isdigit() for x in part.split("-", 1)):
            a, b = part.split("-", 1)
            idx.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            idx.add(int(part))
        else:
            tokens.add(part.lower())
    bad = [i for i in idx if i < 1 or i > count]
    if bad:
        sys.exit("Selection out of range: %s" % ", ".join(map(str, bad)))
    return idx, tokens


def pick_scope(subs, args):
    """Returns (selected_subs, env_filter). Blank dimensions mean all."""
    configure_scope_runtime(args)

    if not should_prompt_scope(args):
        sel = subs
        if getattr(args, "subs", None):
            want = {x.strip().lower() for x in args.subs.split(",") if x.strip()}
            sel = [s for s in subs
                   if s["subscription_id"] in want or s["subscription_name"].lower() in want]
            if not sel:
                sys.exit("--subs matched nothing in the CSV")
        if getattr(args, "nonprod", False):
            return sel, "nonprod"
        if getattr(args, "env", None):
            return sel, norm_env(args.env)
        return sel, None

    print("\nScope step 1/3 - subscription")
    print("Press Enter for all %d included subscriptions, or choose numbers/names/ids." % len(subs))
    for i, s in enumerate(subs, 1):
        print("  %2d) %-45s env=%s  %s" % (i, s["subscription_name"] or "(unnamed)",
                                           s["environment"] or "?", s["subscription_id"]))
    raw = input("Subscriptions [all]: ").strip()
    if raw:
        idx, tokens = parse_selection(raw, len(subs))
        sel = [s for i, s in enumerate(subs, 1)
               if i in idx or s["subscription_id"] in tokens
               or s["subscription_name"].lower() in tokens]
        if not sel:
            sys.exit("Subscription selection matched nothing")
    else:
        sel = subs

    print("\nScope step 2/3 - environment")
    print("Press Enter for all environments. Use 'nonprod' for anything not in --prod-values.")
    print("CSV environments in selected subscriptions: %s" %
          (", ".join(sorted({s["environment"] for s in sel if s["environment"]})) or "(none)"))
    print("Name inference is %s; short-code map: %s" %
          ("on" if ENV_NAME_INFERENCE else "off",
           ", ".join("%s=%s" % (k, v) for k, v in sorted(ENV_CODE_MAP.items()))))
    e = norm_env(input("Environment [all]: "))
    if e == "nonprod":
        return sel, "nonprod"
    if e:
        return sel, e
    return sel, None


def cluster_filter_empty():
    return not any(CLUSTER_FILTER.values())


def parse_cluster_filter(raw):
    filt = {"exact": set(), "prefix": set(), "contains": set()}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low.startswith("prefix:"):
            val = low.split(":", 1)[1].strip()
            if val:
                filt["prefix"].add(val)
        elif low.startswith("contains:"):
            val = low.split(":", 1)[1].strip()
            if val:
                filt["contains"].add(val)
        else:
            filt["exact"].add(low)
    return filt


def prompt_cluster_filter(clusters):
    global CLUSTER_FILTER
    if not PROMPT_CLUSTER_FILTER or not cluster_filter_empty():
        return
    print("\nScope step 3/3 - cluster")
    print("Press Enter for all %d clusters in scope." % len(clusters))
    print("Type exact names/ids, 'prefix:aks-d', 'contains:payments', or '?' to list.")
    raw = input("Cluster filter [all]: ").strip()
    if raw == "?":
        for i, c in enumerate(clusters, 1):
            print("  %3d) %-45s %-28s env=%s" %
                  (i, c["cluster"], c["subscription"][:28], c["environment"]))
        raw = input("Cluster filter [all]: ").strip()
    if raw:
        CLUSTER_FILTER = parse_cluster_filter(raw)


def cluster_in_scope(cluster):
    if cluster_filter_empty():
        return True
    name = norm_env(cluster.get("cluster"))
    rid = norm_env(cluster.get("id"))
    if name in CLUSTER_FILTER["exact"] or rid in CLUSTER_FILTER["exact"]:
        return True
    if any(name.startswith(p) for p in CLUSTER_FILTER["prefix"]):
        return True
    if any(p in name or p in rid for p in CLUSTER_FILTER["contains"]):
        return True
    return False


def cluster_filter_label():
    if cluster_filter_empty():
        return "none"
    parts = []
    for k in ("exact", "prefix", "contains"):
        if CLUSTER_FILTER[k]:
            parts.append("%s=%s" % (k, ",".join(sorted(CLUSTER_FILTER[k]))))
    return "; ".join(parts)


def sanitize_scope_part(value):
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip())
    return value.strip("-")[:60] or "all"


def scope_suffix(env_filter):
    parts = ["all" if env_filter is None else sanitize_scope_part(env_filter)]
    if not cluster_filter_empty():
        parts.append(sanitize_scope_part(cluster_filter_label()))
    return "_".join(parts)


def base_parser(desc):
    p = argparse.ArgumentParser(description=desc,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--csv", default="subscriptions.csv", help="input subscription list")
    p.add_argument("--out", default="reports", help="output directory")
    p.add_argument("--all", action="store_true",
                   help="all subscriptions, environments and clusters; no prompt")
    p.add_argument("--subs", help="comma-separated subscription ids or names; omitted means all")
    p.add_argument("--env", help="only clusters of this environment (e.g. dev, sit, dr)")
    p.add_argument("--nonprod", action="store_true",
                   help="only environments not listed in --prod-values")
    p.add_argument("--cluster", help="comma-separated exact cluster names or full ARM ids")
    p.add_argument("--cluster-id", help="comma-separated full cluster ARM resource ids")
    p.add_argument("--cluster-prefix", help="comma-separated cluster name prefixes")
    p.add_argument("--cluster-contains", help="comma-separated substrings in cluster name/id")
    p.add_argument("--include-unknown-env", action="store_true",
                   help="with --nonprod, also include clusters whose env cannot be determined")
    p.add_argument("--env-tag-keys", default=",".join(DEFAULT_ENV_TAG_KEYS),
                   help="tag keys (in priority order) used to detect a cluster's environment")
    p.add_argument("--env-code-map", default=",".join(
        "%s=%s" % (k, v) for k, v in sorted(DEFAULT_ENV_CODE_MAP.items())),
                   help="name-token environment codes, e.g. d=dev,s=sit,r=dr")
    p.add_argument("--no-name-env", action="store_true",
                   help="do not infer environment from cluster/RG/subscription names")
    p.add_argument("--prod-values", default=",".join(sorted(DEFAULT_PROD_VALUES)),
                   help="environment values treated as prod by --nonprod")
    return p


def out_path(args, stem, env_filter):
    os.makedirs(args.out, exist_ok=True)
    name = "%s_%s_%s.xlsx" % (stem, scope_suffix(env_filter),
                              dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    return os.path.join(args.out, name)
