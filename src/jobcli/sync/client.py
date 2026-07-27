import os
import requests
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from datetime import date

if TYPE_CHECKING:
    from jobcli.profile.schemas import Config

logger = logging.getLogger(__name__)

_DEFAULT_WBL_API_BASE = "https://api.whitebox-learning.com/api"

# Candidate URLs for probing the API when the saved config is missing/invalid.
# Probed in order — local dev should ideally be overridden via `wboxcli config`,
# but `login` will test these sequentially if no config is present.
# NOTE: whitebox-learning.com is the Next.js frontend; the FastAPI backend
# is deployed on the api.whitebox-learning.com subdomain.
WBL_API_CANDIDATES = [
    "https://api.whitebox-learning.com/api",
    "http://localhost:8000/api",
    "http://127.0.0.1:8000/api",
]


def _looks_like_missing_cli_window(response: Optional[requests.Response]) -> bool:
    """Detect "production hasn't deployed /positions/cli_window yet".

    Older backends only have ``/positions/{position_id}`` and the literal
    ``cli_window`` path segment is routed there, failing as an integer
    parse error::

        {"detail":[{"type":"int_parsing","loc":["path","position_id"],
                    "input":"cli_window"}]}

    A naive ``status == 422`` check would over-match (the new backend also
    returns 422 for genuinely invalid query params). We tighten the match
    to the exact path-int-parse pattern above so a legitimate validation
    failure still propagates as an error.
    """
    if response is None or response.status_code != 422:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    for entry in body.get("detail") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "int_parsing" \
                and entry.get("loc") == ["path", "position_id"] \
                and entry.get("input") == "cli_window":
            return True
    return False


# Login error classification kinds. Used so callers (CLI commands) can render
# remediation hints instead of dumping raw stack-trace style strings.
LOGIN_ERR_BAD_CREDENTIALS = "bad_credentials"
LOGIN_ERR_ACCOUNT_LOCKED = "account_locked"
LOGIN_ERR_RATE_LIMIT = "rate_limit"
LOGIN_ERR_SSL = "ssl"
LOGIN_ERR_NETWORK = "network"
LOGIN_ERR_HTTP = "http"
LOGIN_ERR_OTHER = "other"


def _classify_login_failure(base: str, exc: Exception) -> Dict[str, str]:
    """Inspect a login exception and return a ``{base, kind, detail}`` dict.

    The WBL backend returns ``404`` with ``detail="Invalid username or password."``
    for bad credentials (instead of 401). We unwrap that here so the user sees
    "invalid credentials" rather than a misleading "Not Found" message.
    """
    # SSL/TLS first – very common on Windows when the corporate trust store is
    # missing the public root chain.
    if isinstance(exc, requests.exceptions.SSLError):
        return {"base": base, "kind": LOGIN_ERR_SSL, "detail": "TLS certificate verification failed"}

    # HTTP errors carry a response we can introspect.
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        resp = exc.response
        body_detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                body_detail = str(body.get("detail") or "").strip()
        except Exception:
            body_detail = (resp.text or "").strip()[:200]

        status = resp.status_code
        lower = body_detail.lower()
        if status in (401, 404) and ("invalid" in lower and ("username" in lower or "password" in lower)):
            return {"base": base, "kind": LOGIN_ERR_BAD_CREDENTIALS, "detail": "invalid username or password"}
        if status in (401, 403) and "inactive" in lower:
            return {"base": base, "kind": LOGIN_ERR_ACCOUNT_LOCKED, "detail": body_detail or f"HTTP {status}"}
        if status == 429:
            return {"base": base, "kind": LOGIN_ERR_RATE_LIMIT, "detail": body_detail or "rate limit hit (HTTP 429)"}
        if status == 404:
            # Genuine "route missing" (no JSON detail or different message).
            return {"base": base, "kind": LOGIN_ERR_HTTP, "detail": f"HTTP 404 — /login route not found ({body_detail or 'no detail'})"}
        return {"base": base, "kind": LOGIN_ERR_HTTP, "detail": f"HTTP {status} — {body_detail or 'no detail'}"}

    # Connection / timeout / DNS / refused.
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return {"base": base, "kind": LOGIN_ERR_NETWORK, "detail": "connection failed (server unreachable)"}

    return {"base": base, "kind": LOGIN_ERR_OTHER, "detail": str(exc) or exc.__class__.__name__}


def _format_login_errors(errors: List[Dict[str, str]]) -> str:
    """Build a friendly multi-line summary of every candidate's failure."""
    if not errors:
        return "no candidates were attempted"
    lines = ["Authentication failed against all WBL API candidates:"]
    for err in errors:
        lines.append(f"  - {err['base']}  →  {err['detail']}")

    kinds = {e["kind"] for e in errors}
    hints: list[str] = []
    if LOGIN_ERR_BAD_CREDENTIALS in kinds:
        hints.append("invalid credentials — re-run 'jobcli login' with the correct WBL email/password")
    if LOGIN_ERR_ACCOUNT_LOCKED in kinds:
        hints.append("account inactive/disabled — contact Recruiting")
    if LOGIN_ERR_SSL in kinds:
        hints.append(
            "TLS verification failed — set JOBCLI_SSL_CA_BUNDLE=<path-to-ca.pem> "
            "(preferred) or JOBCLI_INSECURE_TLS=1 to skip verification (insecure)"
        )
    if LOGIN_ERR_NETWORK in kinds:
        hints.append("server unreachable — check VPN/network or start the local backend")
    if LOGIN_ERR_RATE_LIMIT in kinds:
        hints.append("rate limited — wait a minute and try again")
    if hints:
        lines.append("")
        lines.append("Next step:")
        for h in hints:
            lines.append(f"  • {h}")
    return "\n".join(lines)


def probe_wbl_api_detailed(
    username: str, password: str, timeout: float = 6.0
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Probe every WBL API candidate. Return ``(winning_url, errors)``.

    ``winning_url`` is the first base that authenticated, or ``None``. ``errors``
    is the per-candidate failure list (empty for the winner, populated for the
    others up to and including the first success). Callers (e.g. ``jobcli
    login``) use this to warn about clearly-broken credentials without printing
    raw endpoint-detection chatter.
    """
    errors: List[Dict[str, str]] = []
    if not username or not password:
        return None, errors
    for base in WBL_API_CANDIDATES:
        norm = _normalize_wbl_api_base(base)
        try:
            r = requests.post(
                f"{norm}/login",
                data={"username": username, "password": password},
                timeout=timeout,
                verify=_requests_verify(),
            )
            if r.status_code == 200 and r.json().get("access_token"):
                return norm, errors
            # Synthesize a requests.HTTPError so we can reuse the classifier.
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as http_err:
                errors.append(_classify_login_failure(norm, http_err))
            else:
                errors.append({"base": norm, "kind": LOGIN_ERR_OTHER, "detail": f"HTTP {r.status_code} without access_token"})
        except Exception as e:
            errors.append(_classify_login_failure(norm, e))
            continue
    return None, errors


def probe_wbl_api(username: str, password: str, timeout: float = 6.0) -> Optional[str]:
    """Backwards-compatible wrapper around :func:`probe_wbl_api_detailed`."""
    winning, _ = probe_wbl_api_detailed(username, password, timeout=timeout)
    return winning

_api_suffix_warned = False


def _normalize_wbl_api_base(url: str) -> str:
    """Ensure base URL ends with ``/api`` (WBL mounts ``/login``, ``/positions/*`` there).

    If ``sync_server_url`` is saved without ``/api``, requests would otherwise
    hit ``/login`` and return 404; the real route is ``/api/login``.
    """
    global _api_suffix_warned
    u = url.strip().rstrip("/")
    if not u:
        return u
    if u.endswith("/api"):
        return u
    if not _api_suffix_warned:
        logger.info(
            "API base URL did not end with /api — appending /api (WBL routes live under /api)."
        )
        _api_suffix_warned = True
    return f"{u}/api"


def _format_knowledge_sync_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map local extractor keys to the WBL ``UploadPayload`` schema.

    The extractor uses ``success_count`` / ``failure_count``; the API expects
    ``total_success`` / ``total_failure``. Without this transform the server
    returns HTTP 422 and blocks analytics flush during ``wboxcli sync``.
    """
    field_answers_out: List[Dict[str, Any]] = []
    for fa in payload.get("field_answers") or []:
        if not isinstance(fa, dict):
            continue
        normalized = (fa.get("normalized_label") or fa.get("field_label") or "").strip()
        value = fa.get("value")
        if not normalized or value is None:
            continue
        total_success = int(fa.get("total_success", fa.get("success_count", 0)) or 0)
        total_failure = int(fa.get("total_failure", fa.get("failure_count", 0)) or 0)
        field_answers_out.append(
            {
                "ats_type": str(fa.get("ats_type") or "unknown"),
                "normalized_label": normalized,
                "value": str(value),
                "total_success": total_success,
                "total_failure": total_failure,
                "confidence": float(fa.get("confidence", 0.0) or 0.0),
            }
        )

    locators_out: List[Dict[str, Any]] = []
    for loc in payload.get("locators") or []:
        if not isinstance(loc, dict):
            continue
        selector = (loc.get("selector") or "").strip()
        purpose = (loc.get("purpose") or "").strip()
        if not selector or not purpose:
            continue
        total_success = int(loc.get("total_success", loc.get("success_count", 0)) or 0)
        total_failure = int(loc.get("total_failure", loc.get("failure_count", 0)) or 0)
        locators_out.append(
            {
                "ats_type": str(loc.get("ats_type") or "unknown"),
                "purpose": purpose,
                "selector": selector,
                "selector_type": str(loc.get("selector_type") or "css"),
                "domain_pattern": loc.get("domain_pattern"),
                "total_success": total_success,
                "total_failure": total_failure,
                "confidence": float(
                    loc.get("confidence", loc.get("confidence_score", 0.0)) or 0.0
                ),
            }
        )

    return {"field_answers": field_answers_out, "locators": locators_out}


def _requests_verify() -> Any:
    """Delegate to the shared TLS configuration (see ``jobcli.utils.tls``)."""
    from jobcli.utils.tls import requests_verify

    return requests_verify()


class SyncClient:
    """HTTP client for WBL API sync and discovery.

    When ``config`` is provided, credentials and base URL come from the explicit
    ``Config``. Otherwise values are resolved lazily via ``get_config()`` (which
    reads only the local SQLite store — no ``.env`` is loaded).
    """

    def __init__(self, config: Optional["Config"] = None) -> None:
        self._config = config
        self.token: Optional[str] = None
        self.candidate_id = None
        self.job_types: List[Dict[str, Any]] = []
        # Populated by ``login()`` on failure so callers can render a friendly,
        # remediation-aware message (creds vs TLS vs network). Each entry has
        # ``{base, kind, detail}``.
        self.last_login_errors: List[Dict[str, str]] = []

    def _resolve_config(self) -> "Config":
        if self._config is not None:
            return self._config
        from jobcli.cli.main import get_config

        return get_config()

    @property
    def base_url(self) -> str:
        return self._get_server_url()

    def _get_server_url(self) -> str:
        """Resolve WBL API base URL.

        Uses the saved ``sync_server_url`` when present, otherwise falls back
        to the production WBL API base.
        """
        cfg = self._resolve_config()
        configured = (cfg.sync_server_url or "").strip()
        if configured:
            return _normalize_wbl_api_base(configured)
        return _normalize_wbl_api_base(_DEFAULT_WBL_API_BASE)

    def login(self) -> bool:
        """Authenticate with the WBL API and store token/candidate_id.

        Strategy:
          1. If a base URL is already cached in config, try it first.
          2. Otherwise (or on failure), walk ``WBL_API_CANDIDATES`` in order
             (production → localhost) and use the first that returns a token.
          3. Persist the winning base URL back to local config so subsequent
             calls go straight to it.
        """
        cfg = self._resolve_config()
        username = (cfg.job_board_username or os.getenv("JOBCLI_USERNAME") or "").strip()
        password = cfg.job_board_password or os.getenv("JOBCLI_PASSWORD") or ""

        if not username or not password:
            logger.warning("WBL username/password missing (config or JOBCLI_* env). Sync will be limited.")
            return False

        candidates: list[str] = []
        saved = (cfg.sync_server_url or "").strip()
        if saved:
            norm_saved = _normalize_wbl_api_base(saved)
            if norm_saved not in candidates:
                candidates.append(norm_saved)
        for c in WBL_API_CANDIDATES:
            norm = _normalize_wbl_api_base(c)
            if norm not in candidates:
                candidates.append(norm)

        errors: List[Dict[str, str]] = []
        for base in candidates:
            try:
                response = requests.post(
                    f"{base}/login",
                    data={"username": username, "password": password},
                    timeout=10,
                    verify=_requests_verify(),
                )
                response.raise_for_status()
                token_data = response.json()
                token = token_data.get("access_token")
                if not token:
                    errors.append({"base": base, "kind": LOGIN_ERR_OTHER, "detail": "no access_token in response"})
                    continue

                self.token = token
                self.candidate_id = token_data.get("candidate_id")
                self.last_login_errors = []
                logger.info(
                    f"Successfully authenticated as {username} via {base} "
                    f"(Candidate ID: {self.candidate_id})"
                )

                # Cache the winning base URL back to local config so we skip the
                # probe on subsequent operations.
                if (cfg.sync_server_url or "").strip().rstrip("/") != base.rstrip("/"):
                    try:
                        from jobcli.cli.main import save_config

                        cfg.sync_server_url = base
                        save_config(cfg)
                    except Exception:
                        pass
                return True
            except Exception as e:
                err = _classify_login_failure(base, e)
                errors.append(err)
                logger.debug(f"WBL login attempt failed for {base}: {err['kind']} — {err['detail']}")
                continue

        self.last_login_errors = errors
        # Use ``debug`` (not ``error``) here: callers render the classified,
        # multi-line message via ``last_login_errors`` / the raised exception so
        # the user doesn't see the same block twice.
        logger.debug(_format_login_errors(errors))
        return False

    def get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers. Logs in if token is missing."""
        if not self.token:
            self.login()

        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def fetch_cli_window_listings(
        self,
        days: int = 0,
        page_size: int = 5000,
        status: str = "open",
        offset: int = 0,
    ) -> dict:
        """GET /positions/cli_window — canonical WBL listings for JobCLI discovery.

        ``days=0`` requests no ``created_at`` lower bound (full listing set with
        job URLs). Use ``offset`` + ``page_size`` to page; responses include
        ``total_in_window`` for the full matching row count.

        Production-API fallback
        -----------------------
        The current production deployment at ``api.whitebox-learning.com``
        is running an older backend that does **not** have the
        ``/cli_window`` route mounted. Hitting it lands on the generic
        ``/positions/{position_id}`` route, which FastAPI tries to parse
        as an integer — the resulting 422 body is::

            {"detail":[{"type":"int_parsing","loc":["path","position_id"],
                        "input":"cli_window"}]}

        When we detect that exact signature we transparently fall back to
        the deployed-everywhere ``/positions/paginated`` endpoint and
        shape its response to match what callers expect from
        ``cli_window``. Once production redeploys the new backend this
        fallback becomes inert.
        """
        headers = self.get_auth_headers()
        if not headers:
            raise RuntimeError("WBL authentication failed: missing username/password or invalid credentials")
        url = f"{self._get_server_url()}/positions/cli_window"
        params: dict[str, Any] = {
            "days": days,
            "page_size": page_size,
            "offset": max(0, int(offset)),
        }
        if status and status.lower() not in ("all", "any"):
            params["status"] = status
        response = None
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=120,
                verify=_requests_verify(),
            )
            response.raise_for_status()
        except requests.HTTPError as e:
            # Detect the "production hasn't deployed cli_window yet" case
            # and fall back to /positions/paginated instead of erroring out.
            if _looks_like_missing_cli_window(response):
                logger.info(
                    "cli_window endpoint not available on this backend; "
                    "falling back to /positions/paginated."
                )
                return self._fetch_listings_via_paginated(
                    headers=headers,
                    requested_offset=int(offset or 0),
                    requested_page_size=int(page_size),
                    status=status,
                )
            # Surface the server's actual rejection reason (FastAPI 422
            # validation errors put a JSON ``detail`` array in the body —
            # without printing it the user just sees the useless
            # "Client Error: unknown for url" line). Include the body
            # truncated to ~600 chars so the error is loud but bounded.
            body_text = ""
            try:
                if response is not None:
                    if response.headers.get("content-type", "").startswith("application/json"):
                        body_text = response.text
                    else:
                        body_text = response.text[:600]
            except Exception:
                pass
            http_status = response.status_code if response is not None else "?"
            extra = f" — body: {body_text[:600]}" if body_text else ""
            raise RuntimeError(
                f"cli_window request failed: HTTP {http_status} for {url}?"
                f"{requests.compat.urlencode(params)}{extra}"
            ) from e
        except requests.RequestException as e:
            raise RuntimeError(f"cli_window request failed: {e}") from e
        try:
            data = response.json()
        except ValueError as e:
            raise RuntimeError("cli_window returned non-JSON body") from e
        if not isinstance(data, dict) or "data" not in data:
            raise RuntimeError("cli_window response missing 'data' array")
        return data

    def _fetch_listings_via_paginated(
        self,
        *,
        headers: Dict[str, str],
        requested_offset: int,
        requested_page_size: int,
        status: Optional[str],
    ) -> dict:
        """Fallback for backends without the ``/cli_window`` route.

        Walks ``/positions/paginated`` until either all rows are fetched or
        ``requested_page_size`` rows have been collected, applies the same
        status filter client-side, and returns a dict shaped like a
        ``cli_window`` response so the caller (``WboxDiscoverer``) doesn't
        need to branch.

        The paginated endpoint caps ``page_size`` at 500 server-side, so we
        loop through pages and aggregate. ``requested_offset`` is honored
        by skipping rows after status filtering — preserves caller
        semantics even though the upstream endpoint uses page numbers
        rather than row offsets.
        """
        base = f"{self._get_server_url()}/positions/paginated"
        # Server caps paginated page_size at 500 (see backend
        # routes/job_listing.py). Always request its maximum so we
        # minimize round-trips.
        upstream_page_size = 500
        want_status = (status or "").strip().lower()
        keep_all_statuses = want_status in ("", "all", "any")

        collected: List[dict] = []
        total_records: Optional[int] = None
        page = 1
        # Hard cap loop iterations so a buggy server can't trap us forever.
        max_pages = 200
        while page <= max_pages:
            resp = requests.get(
                base,
                headers=headers,
                params={"page": page, "page_size": upstream_page_size},
                timeout=120,
                verify=_requests_verify(),
            )
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError as e:
                raise RuntimeError("paginated returned non-JSON body") from e
            if not isinstance(payload, dict):
                raise RuntimeError("paginated returned unexpected body shape")
            if total_records is None:
                try:
                    total_records = int(payload.get("total_records") or 0)
                except (TypeError, ValueError):
                    total_records = 0
            batch = payload.get("data") or []
            for row in batch:
                if keep_all_statuses or (str(row.get("status") or "").lower() == want_status):
                    collected.append(row)
            if not payload.get("has_next"):
                break
            if len(batch) < upstream_page_size:
                break
            page += 1

        # Apply the caller's offset/page_size window over the filtered rows.
        if requested_offset > 0:
            collected = collected[requested_offset:]
        if requested_page_size > 0:
            collected = collected[:requested_page_size]

        return {
            "data": collected,
            "days": 0,
            "page_size": requested_page_size,
            "offset": requested_offset,
            # ``total_in_window`` is the WBL/cli_window contract; populate it
            # from the count of returned rows so the discoverer's paging
            # loop terminates after one call (we already paginated above).
            "total_in_window": len(collected),
            "returned_count": len(collected),
            "sort": "id_desc",  # paginated returns newest-first by id desc
        }

    def fetch_job_types(self) -> List[Dict[str, Any]]:
        """Fetch available job types from the server for mapping."""
        headers = self.get_auth_headers()
        if not headers:
            return []

        url = f"{self._get_server_url()}/job-types"
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=_requests_verify())
            response.raise_for_status()
            self.job_types = response.json()
            return self.job_types
        except Exception as e:
            logger.error(f"Failed to fetch job types: {str(e)}")
            return []

    def map_job_to_type_id(self, job_title: str) -> Optional[int]:
        """Try to map a job title to an existing job_type_id."""
        if not self.job_types:
            self.fetch_job_types()

        if not self.job_types:
            return None

        # 1. Exact match
        for jt in self.job_types:
            if jt["name"].lower() == job_title.lower():
                return jt["id"]

        # 2. Partial match
        for jt in self.job_types:
            if jt["name"].lower() in job_title.lower() or job_title.lower() in jt["name"].lower():
                return jt["id"]

        # 3. Default to "Automation" if it exists
        for jt in self.job_types:
            if "automation" in jt["name"].lower():
                return jt["id"]

        # 4. Just return the first one as fallback if any exist
        return self.job_types[0]["id"] if self.job_types else None

    def upload_knowledge(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Upload field answers and locators to the central server."""
        url = f"{self._get_server_url()}/sync_cli/knowledge_sync"
        headers = self.get_auth_headers()
        headers["Content-Type"] = "application/json"
        body = _format_knowledge_sync_payload(payload)

        response = requests.post(
            url, json=body, headers=headers, timeout=15, verify=_requests_verify()
        )
        response.raise_for_status()
        return response.json()

    def download_updates(self, current_version: str) -> Dict[str, Any]:
        """Download the latest aggregated locators and field answers."""
        url = f"{self._get_server_url()}/sync_cli/knowledge_updates"
        params = {"current_version": current_version}

        response = requests.get(url, params=params, timeout=15, verify=_requests_verify())
        response.raise_for_status()
        return response.json()

    def upload_activity_logs(self, raw_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Upload job activity logs to the central server."""
        if not raw_logs:
            return {"status": "skipped", "message": "No logs to upload"}

        headers = self.get_auth_headers()
        if "Authorization" not in headers:
            return {"status": "error", "message": "Authentication required for activity sync"}

        if not self.candidate_id:
            return {"status": "error", "message": "Candidate ID not found for current user"}

        formatted_logs = []
        for log in raw_logs:
            job_type_id = self.map_job_to_type_id(log.get("title", ""))
            if not job_type_id:
                logger.warning(f"Could not map job '{log.get('title')}' to any backend job type. Skipping.")
                continue

            formatted_logs.append(
                {
                    "job_id": job_type_id,
                    "candidate_id": self.candidate_id,
                    "activity_date": date.today().isoformat(),
                    "activity_count": 1,
                    "notes": f"Applied via JobCLI: {log.get('title')} at {log.get('company')}. Status: {log.get('status')}",
                }
            )

        if not formatted_logs:
            return {"status": "skipped", "message": "No valid logs after mapping"}

        url = f"{self._get_server_url()}/job_activity_logs/bulk"
        headers["Content-Type"] = "application/json"

        payload = {"logs": formatted_logs}

        response = requests.post(url, json=payload, headers=headers, timeout=20, verify=_requests_verify())
        response.raise_for_status()
        return response.json()

    def upload_usage_events(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Upload usage analytics events in bulk."""
        if not events:
            return {"status": "skipped", "ingested": 0}
        headers = self.get_auth_headers()
        if "Authorization" not in headers:
            return {"status": "error", "message": "Authentication required for analytics sync"}
        headers["Content-Type"] = "application/json"
        url = f"{self._get_server_url()}/analytics/usage_events/bulk"
        response = requests.post(
            url,
            json={"events": events},
            headers=headers,
            timeout=20,
            verify=_requests_verify(),
        )
        if not response.ok:
            detail = response.text[:600] if response.text else f"HTTP {response.status_code}"
            logger.error(
                "Analytics bulk upload failed (%s %s): %s",
                response.status_code,
                url,
                detail,
            )
            response.raise_for_status()
        return response.json()


# Singleton for legacy callers that do not pass an explicit Config (uses lazy get_config()).
_client: Optional[SyncClient] = None


def get_client(config: Optional["Config"] = None) -> SyncClient:
    """Return a SyncClient. Pass ``config`` for explicit merged settings; otherwise a process-wide singleton."""
    global _client
    if config is not None:
        return SyncClient(config)
    if _client is None:
        _client = SyncClient(None)
    return _client


def reset_sync_client_singleton() -> None:
    """Clear cached SyncClient (e.g. after full DB reset)."""
    global _client
    _client = None


# Legacy function wrappers for backward compatibility
def upload_knowledge(payload: Dict[str, Any]) -> Dict[str, Any]:
    return get_client().upload_knowledge(payload)


def download_updates(current_version: str) -> Dict[str, Any]:
    return get_client().download_updates(current_version)


def upload_activity_logs(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return get_client().upload_activity_logs(logs)
