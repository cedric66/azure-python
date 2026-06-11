"""Throttled, retrying HTTP session for Azure ARM REST calls.

Handles:
- Bearer token acquisition/refresh from any azure.identity credential
- 429 throttling: honors Retry-After and Cost Management QPU retry headers
- 5xx / connection errors: exponential backoff with jitter
- Optional client-side pacing (min interval between calls)
- nextLink pagination for GET and POST APIs
"""
import random
import threading
import time

import requests

ARM = "https://management.azure.com"
MAX_BACKOFF_S = 300.0


class AzureApiError(RuntimeError):
    def __init__(self, msg, status=None, body=None):
        super().__init__(msg)
        self.status = status
        self.body = body or ""


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def connect(min_interval=0.05):
    """DefaultAzureCredential works with `az login`, service principal env vars
    (AZURE_CLIENT_ID/SECRET/TENANT_ID) or managed identity - no code changes."""
    from azure.identity import DefaultAzureCredential
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return AzureSession(cred, min_interval=min_interval)


class AzureSession:
    def __init__(self, credential, scope="https://management.azure.com/.default",
                 min_interval=0.0, max_retries=8, timeout=120):
        self.cred = credential
        self.scope = scope
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.s = requests.Session()
        self.last_headers = {}
        self._tok = None
        self._exp = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def _token(self):
        if not self._tok or time.time() > self._exp - 300:
            t = self.cred.get_token(self.scope)
            self._tok, self._exp = t.token, t.expires_on
        return self._tok

    def _pace(self, min_interval=None):
        mi = self.min_interval if min_interval is None else min_interval
        if mi <= 0:
            return
        with self._lock:
            wait = self._last + mi - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    @staticmethod
    def _retry_after(resp):
        for h in ("x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
                  "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
                  "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
                  "Retry-After"):
            v = resp.headers.get(h)
            if v:
                try:
                    return min(float(v), MAX_BACKOFF_S)
                except ValueError:
                    pass
        return None

    def request(self, method, url, *, params=None, payload=None, ok404=False,
                min_interval=None):
        if url.startswith("/"):
            url = ARM + url
        last_err = None
        for attempt in range(self.max_retries + 1):
            self._pace(min_interval)
            try:
                r = self.s.request(method, url, params=params, json=payload,
                                   headers={"Authorization": "Bearer " + self._token()},
                                   timeout=self.timeout)
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(min(2 ** attempt, 30) + random.random())
                continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                wait = self._retry_after(r)
                if wait is None:
                    wait = min(2 ** attempt, 60) + random.random()
                log("  HTTP %d on %s; backing off %.0fs (attempt %d/%d)"
                    % (r.status_code, url.split("?")[0][-80:], wait, attempt + 1, self.max_retries))
                time.sleep(wait)
                last_err = "HTTP %d: %s" % (r.status_code, r.text[:300])
                continue
            if r.status_code >= 400:
                raise AzureApiError("%s %s -> HTTP %d: %s" % (method, url, r.status_code, r.text[:1500]),
                                    r.status_code, r.text)
            self.last_headers = r.headers
            return r.json() if r.text.strip() else {}
        raise AzureApiError("%s %s failed after %d attempts: %s"
                            % (method, url, self.max_retries + 1, last_err))

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def get_paged(self, url, params=None, value_key="value"):
        """Follow value[] + nextLink pagination for GET list APIs."""
        out = []
        while url:
            data = self.get(url, params=params)
            params = None
            out.extend(data.get(value_key) or [])
            url = data.get("nextLink") or data.get("@odata.nextLink")
        return out

    def post_paged(self, url, payload, value_key="value"):
        """Follow value[] + @odata.nextLink pagination for POST query APIs."""
        out = []
        while url:
            data = self.post(url, payload=payload)
            out.extend(data.get(value_key) or [])
            url = data.get("@odata.nextLink") or data.get("nextLink")
        return out
