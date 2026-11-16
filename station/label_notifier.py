"""
label_notifier.py — Tell a student, by email, that their print has ended and ask
how it turned out.   **FUTURE FEATURE: built but disabled.**

Human labels currently come from the website's "Label my print" page (students
open it themselves). This module is the architecture for the *reminder layer*
that will nudge them with an email once the lab has a Cornell sender address:

    print ends (MQTT) -> main event loop -> LabelNotifier.notify_print_end(event)
        -> look up WHO printed it (job -> upload join, station/matching.py)
        -> POST {job_id, netid, ...} to the website's /api/notify-print-end
        -> the website signs a link to the label page and sends the email
        -> the student clicks the link, confirms Succeeded / Failed on the page
        -> /api/label-webhook (source="email")

It is OFF unless config.json has ``labels.email_enabled: true``. Even then NOTHING
is sent until consent is recorded — see ``_has_consent``.

``notify_print_end`` never raises and returns a short status string so tests (and
the log) can see exactly what happened.

To turn it on later (all four are required):
  1. Add a consent checkbox to the upload form and store it on the uploads row,
     then implement ``_has_consent``.
  2. A Cornell sender address + a Resend API key (Vercel env RESEND_API_KEY,
     EMAIL_FROM).
  3. Vercel env EMAIL_LABELING_ENABLED=true and LABEL_LINK_SECRET.
  4. config.json: ``"labels": {"email_enabled": true, "notify_url": "<site>/api/notify-print-end"}``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Optional

import database as db
from mqtt_listener import PrintEndEvent
from station.matching import EXACT, NORMALIZED, normalize_netid

log = logging.getLogger(__name__)

# Cornell NetIDs: 2-4 letters followed by 1-5 digits (e.g. "ab123", "abc1234").
# The upload form accepts free text, so only a NetID-shaped value ever becomes an
# email address.
_NETID_RE = re.compile(r"^[a-z]{2,4}[0-9]{1,5}$")

# Outcome codes returned by notify_print_end.
DISABLED = "disabled"
MISCONFIGURED = "misconfigured"
NO_JOB = "no-job"
NO_SUBMISSION = "no-submission"
AMBIGUOUS = "ambiguous"
INVALID_NETID = "invalid-netid"
NO_CONSENT = "no-consent"
SENT = "sent"
FAILED = "failed"


def _http_post(url: str, *, json: dict[str, Any], headers: dict[str, Any], timeout: float) -> int:
    import httpx  # imported lazily: the disabled path must not need the network stack

    return httpx.post(url, json=json, headers=headers, timeout=timeout).status_code


class LabelNotifier:
    def __init__(
        self,
        enabled: bool = False,
        notify_url: str = "",
        secret: Optional[str] = None,
        email_domain: str = "cornell.edu",
        post: Callable[..., int] = _http_post,
        timeout: float = 10.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.notify_url = (notify_url or "").strip()
        self.secret = secret or ""
        self.email_domain = (email_domain or "cornell.edu").strip().lstrip("@")
        self._post = post
        self._timeout = timeout

    @classmethod
    def from_config(cls, full_config: dict[str, Any], **kwargs: Any) -> LabelNotifier:
        labels = full_config.get("labels") or {}
        web = full_config.get("web") or {}
        return cls(
            enabled=bool(labels.get("email_enabled", False)),
            notify_url=labels.get("notify_url", ""),
            secret=web.get("webhook_secret"),
            email_domain=labels.get("email_domain", "cornell.edu"),
            **kwargs,
        )

    # ------------------------------------------------------------------

    def _has_consent(self, upload: Any) -> bool:
        """
        Did the student agree to be emailed about this print?

        NOT IMPLEMENTED YET: the upload form has no consent checkbox and the
        uploads table has no consent column, so no consent exists and nothing is
        ever sent. Implement this together with the checkbox (see module docstring).
        """
        return False

    def notify_print_end(self, event: PrintEndEvent) -> str:
        try:
            return self._notify(event)
        except Exception as exc:  # noqa: BLE001 — a notifier must never stop the daemon
            log.exception("LabelNotifier failed for job %s: %s", event.job_id, exc)
            return FAILED

    def _notify(self, event: PrintEndEvent) -> str:
        job = db.get_job(event.job_id)
        match = db.find_upload_for_job(job["subtask_name"], job["start_time"]) if job else None
        netid = normalize_netid(match.student_name) if match is not None and match.upload is not None else ""

        if not self.enabled:
            log.info(
                "Email labeling disabled; job %s ended (%s) netid=%s match=%s",
                event.job_id, event.final_state, netid or "-", match.confidence if match else "none",
            )
            return DISABLED

        if not self.notify_url or not self.secret:
            log.warning("Email labeling enabled but labels.notify_url / web.webhook_secret missing; skipping.")
            return MISCONFIGURED
        if job is None:
            log.warning("Email labeling: job %s not found in the database.", event.job_id)
            return NO_JOB
        if match is None or match.upload is None or match.confidence not in (EXACT, NORMALIZED):
            level = match.confidence if match else "none"
            log.info("Email labeling: job %s has no unambiguous submission (%s); skipping.", event.job_id, level)
            return AMBIGUOUS if level == "ambiguous" else NO_SUBMISSION
        if not _NETID_RE.match(netid):
            log.info("Email labeling: %r is not a NetID; skipping job %s.", netid, event.job_id)
            return INVALID_NETID
        if not self._has_consent(match.upload):
            log.info("Email labeling: no recorded consent for %s; not emailing (job %s).", netid, event.job_id)
            return NO_CONSENT

        payload = {
            "job_id": event.job_id,
            "netid": netid,
            "filename": match.upload["renamed_filename"],
            "printer_name": event.printer_name,
        }
        status = self._post(
            self.notify_url,
            json=payload,
            headers={"x-station-key": self.secret},
            timeout=self._timeout,
        )
        if 200 <= status < 300:
            log.info("Email labeling: requested email for %s (job %s).", netid, event.job_id)
            return SENT
        log.warning("Email labeling: website answered HTTP %s for job %s.", status, event.job_id)
        return FAILED
