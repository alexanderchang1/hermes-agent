"""
Email platform adapter for the Hermes gateway.

Allows users to interact with Hermes by sending emails.
Uses IMAP to receive and SMTP to send messages.

Environment variables:
    EMAIL_IMAP_HOST     — IMAP server host (e.g., imap.gmail.com)
    EMAIL_IMAP_PORT     — IMAP server port (default: 993)
    EMAIL_SMTP_HOST     — SMTP server host (e.g., smtp.gmail.com)
    EMAIL_SMTP_PORT     — SMTP server port (default: 587)
    EMAIL_ADDRESS       — Email address for the agent
    EMAIL_PASSWORD      — Email password or app-specific password
    EMAIL_POLL_INTERVAL — Seconds between mailbox checks (default: 15)
    EMAIL_ALLOWED_USERS — Comma-separated list of allowed sender addresses
"""

import asyncio
import email as email_lib
import imaplib
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import uuid
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.utils import formatdate
from email import encoders
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.config import Platform, PlatformConfig
from utils import env_int, env_bool

logger = logging.getLogger(__name__)
# Automated sender patterns — emails from these are silently ignored
_NOREPLY_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "automated@", "auto-confirm", "auto-reply", "automailer",
)

# RFC headers that indicate bulk/automated mail
_AUTOMATED_HEADERS = {
    "Auto-Submitted": lambda v: v.lower() != "no",
    "Precedence": lambda v: v.lower() in {"bulk", "list", "junk"},
    "X-Auto-Response-Suppress": lambda v: bool(v),
    "List-Unsubscribe": lambda v: bool(v),
}

# Gmail-safe max length per email body
MAX_MESSAGE_LENGTH = 50_000

SMTP_CONNECT_TIMEOUT = 30


def _create_ipv4_connection(
    host: str,
    port: int,
    timeout: float,
    source_address: Any = None,
) -> socket.socket:
    """Create a TCP connection using only IPv4 addresses.

    This mirrors ``socket.create_connection`` but constrains DNS resolution to
    ``AF_INET``.  It avoids mutating process-global socket functions, which
    matters because email sends run in executor threads.
    """
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"No IPv4 address found for {host}:{port}")


class _IPv4SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):  # type: ignore[override]
        return _create_ipv4_connection(
            host,
            port,
            timeout,
            source_address=self.source_address,
        )


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):  # type: ignore[override]
        raw_sock = _create_ipv4_connection(
            host,
            port,
            timeout,
            source_address=self.source_address,
        )
        return self.context.wrap_socket(
            raw_sock,
            server_hostname=getattr(self, "_host", host),
        )

# Supported image extensions for inline detection
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

def _send_imap_id(imap: "imaplib.IMAP4") -> None:
    """Send RFC 2971 IMAP ID command identifying this client.

    Required by 163/NetEase mailbox after LOGIN: without it, every UID
    SEARCH/FETCH returns ``BYE Unsafe Login`` and disconnects.  Other
    IMAP servers either honor it silently or reject the unknown command;
    we swallow failures so non-supporting servers keep working.
    """
    try:
        try:
            from hermes_cli import __version__ as _hermes_version
        except Exception:  # noqa: BLE001 — keep ID best-effort if import fails
            _hermes_version = "0"
        imap.xatom(
            "ID",
            f'("name" "hermes-agent" "version" "{_hermes_version}" '
            '"vendor" "NousResearch" '
            '"support-email" "noreply@nousresearch.com")',
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        logger.debug("[Email] IMAP ID command not accepted: %s", e)


def _is_automated_sender(address: str, headers: dict) -> bool:
    """Return True if this email is from an automated/noreply source."""
    addr = address.lower()
    if any(pattern in addr for pattern in _NOREPLY_PATTERNS):
        return True
    for header, check in _AUTOMATED_HEADERS.items():
        value = headers.get(header, "")
        if value and check(value):
            return True
    return False
    
def check_email_requirements() -> bool:
    """Check if email platform settings are available and non-blank.

    Treats blank/whitespace-only values as missing so an abandoned setup that
    left empty ``EMAIL_*`` keys in ``.env`` does not enable the platform (#40715).
    """
    addr = os.getenv("EMAIL_ADDRESS", "").strip()
    pwd = os.getenv("EMAIL_PASSWORD", "").strip()
    imap = os.getenv("EMAIL_IMAP_HOST", "").strip()
    smtp = os.getenv("EMAIL_SMTP_HOST", "").strip()
    return all([addr, pwd, imap, smtp])


def _decode_header_value(raw: str) -> str:
    """Decode an RFC 2047 encoded email header into a plain string."""
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_text_body(msg: email_lib.message.Message) -> str:
    """Extract the plain-text body from a potentially multipart email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            # Skip attachments
            if "attachment" in disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html and strip tags
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return _strip_html(html)
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
        return ""


def _strip_html(html: str) -> str:
    """Naive HTML tag stripper for fallback text extraction."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_email_address(raw: str) -> str:
    """Extract bare email address from 'Name <addr>' format."""
    match = re.search(r"<([^>]+)>", raw)
    if match:
        return match.group(1).strip().lower()
    return raw.strip().lower()


def _domain_of(address: str) -> str:
    """Return the lowercased domain part of an email address, or ''."""
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()


def _domains_aligned(a: str, b: str) -> bool:
    """Return True if two domains are equal or in an organizational
    parent/subdomain relationship (relaxed DMARC alignment).

    DMARC relaxed alignment treats ``mail.example.com`` as aligned with
    ``example.com``. We approximate organizational alignment by checking
    exact equality or that one domain is a dot-suffix of the other.
    """
    a = (a or "").strip().lower().rstrip(".")
    b = (b or "").strip().lower().rstrip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("." + b) or b.endswith("." + a)


# Match a single "method=result" token in an Authentication-Results header,
# e.g. ``dmarc=pass`` or ``spf=fail``.
_AUTH_METHOD_RE = re.compile(
    r"\b(dmarc|dkim|spf)\s*=\s*([a-z]+)", re.IGNORECASE
)
# Match a property value like ``header.from=example.com`` or
# ``smtp.mailfrom=user@example.com``.
_AUTH_PROP_RE = re.compile(
    r"\b(header\.from|header\.d|smtp\.mailfrom|smtp\.from|envelope-from)\s*=\s*([^\s;]+)",
    re.IGNORECASE,
)


def _verify_sender_authentication(
    msg: email_lib.message.Message,
    from_addr: str,
    *,
    authserv_id: str = "",
) -> Tuple[bool, str]:
    """Verify that the message's ``From:`` domain is authenticated.

    The ``From:`` header is attacker-controlled and is never authenticated by
    IMAP delivery, so an allowlist keyed on ``From:`` alone is trivially
    spoofable (GHSA-rxqh-5572-8m77). The only trustworthy signal is the
    ``Authentication-Results`` header that the *receiving* mail server (the one
    we IMAP into) stamps after running SPF/DKIM/DMARC. That header is prepended
    by our own server, so the topmost instance is the one we trust; any
    ``Authentication-Results`` an attacker injected into the body of their
    message sorts below it.

    Returns ``(authenticated, reason)``. ``authenticated`` is True when:
      * a DMARC pass is recorded for the From domain, OR
      * an SPF pass aligned with the From domain, OR
      * a DKIM pass aligned (``header.d``) with the From domain.

    When no ``Authentication-Results`` header is present at all, we return
    ``(False, "no Authentication-Results header")`` — fail-closed. Operators
    whose mail server does not stamp this header can opt out of the check
    (see ``EmailAdapter._require_authenticated_sender``).
    """
    from_domain = _domain_of(from_addr)
    if not from_domain:
        return False, "missing From domain"

    # get_all preserves header order; the receiving server prepends its result,
    # so the FIRST Authentication-Results is the trusted one. We pin to the
    # configured authserv-id when provided to defend against an injected header
    # that happens to sort first.
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False, "no Authentication-Results header"

    trusted = None
    for raw in headers:
        value = " ".join(str(raw).split())
        if authserv_id:
            # authserv-id is the first token before the first ';'
            serv = value.split(";", 1)[0].strip().lower()
            if not _domains_aligned(serv, authserv_id) and serv != authserv_id.lower():
                continue
        trusted = value
        break
    if trusted is None:
        return False, "no Authentication-Results from trusted authserv-id"

    methods = {m.lower(): r.lower() for m, r in _AUTH_METHOD_RE.findall(trusted)}
    props = {p.lower(): v.strip().strip('"') for p, v in _AUTH_PROP_RE.findall(trusted)}

    # 1) DMARC pass is the strongest signal — DMARC already enforces From
    #    alignment, so a pass means the From domain is authenticated.
    if methods.get("dmarc") == "pass":
        return True, "dmarc=pass"

    # 2) SPF pass aligned with the From domain (the envelope/MAIL FROM domain
    #    must match the From domain).
    if methods.get("spf") == "pass":
        spf_domain = _domain_of(props.get("smtp.mailfrom", "")) or props.get(
            "smtp.from", ""
        ) or props.get("envelope-from", "")
        spf_domain = _domain_of(spf_domain) if "@" in spf_domain else spf_domain
        if _domains_aligned(spf_domain, from_domain):
            return True, "spf=pass aligned"

    # 3) DKIM pass aligned with the From domain (the signing domain header.d
    #    must align with the From domain).
    if methods.get("dkim") == "pass":
        dkim_domain = props.get("header.d", "") or _domain_of(props.get("header.from", ""))
        if _domains_aligned(dkim_domain, from_domain):
            return True, "dkim=pass aligned"

    return False, f"authentication failed ({trusted[:120]})"


def _extract_attachments(
    msg: email_lib.message.Message,
    skip_attachments: bool = False,
) -> List[Dict[str, Any]]:
    """Extract attachment metadata and cache files locally.

    When *skip_attachments* is True, all attachment/inline parts are ignored
    (useful for malware protection or bandwidth savings).
    """
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if skip_attachments and ("attachment" in disposition or "inline" in disposition):
            continue
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        # Skip text/plain and text/html body parts
        content_type = part.get_content_type()
        if content_type in {"text/plain", "text/html"} and "attachment" not in disposition:
            continue

        filename = part.get_filename()
        if filename:
            filename = _decode_header_value(filename)
        else:
            ext = part.get_content_subtype() or "bin"
            filename = f"attachment.{ext}"

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        ext = Path(filename).suffix.lower()
        if ext in _IMAGE_EXTS:
            try:
                cached_path = cache_image_from_bytes(payload, ext)
            except ValueError:
                logger.debug("Skipping non-image attachment %s (invalid magic bytes)", filename)
                continue
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "image",
                "media_type": content_type,
            })
        else:
            cached_path = cache_document_from_bytes(payload, filename)
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "document",
                "media_type": content_type,
            })

    return attachments


class EmailAdapter(BasePlatformAdapter):
    """Email gateway adapter using IMAP (receive) and SMTP (send)."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.EMAIL)

        # Resolve connection settings from the env vars first, then fall back to
        # PlatformConfig.extra (address/imap_host/smtp_host) — the canonical dict
        # gateway.config populates and that the "connected" check, the
        # send-helper, and `hermes config show` already read. Without the
        # fallback a config.yaml-only setup left these empty. Host/address values
        # are stripped: a stray space or newline made IMAP4_SSL raise the
        # misleading ``[Errno 8] nodename nor servname`` (an unresolvable name)
        # instead of an obvious "host not set" error.
        extra = config.extra or {}
        self._address = (os.getenv("EMAIL_ADDRESS", "") or extra.get("address", "")).strip()
        self._password = os.getenv("EMAIL_PASSWORD", "")
        self._imap_host = (os.getenv("EMAIL_IMAP_HOST", "") or extra.get("imap_host", "")).strip()
        self._imap_port = env_int("EMAIL_IMAP_PORT", 993)
        self._smtp_host = (os.getenv("EMAIL_SMTP_HOST", "") or extra.get("smtp_host", "")).strip()
        self._smtp_port = env_int("EMAIL_SMTP_PORT", 587)
        self._poll_interval = env_int("EMAIL_POLL_INTERVAL", 15)

        # Skip attachments — configured via config.yaml:
        #   platforms:
        #     email:
        #       skip_attachments: true
        self._skip_attachments = extra.get("skip_attachments", False)

        # Require the sender's From: domain to be authenticated (SPF/DKIM/DMARC)
        # before trusting it for authorization. The From: header is
        # attacker-controlled and unauthenticated by IMAP, so an allowlist keyed
        # on it alone is spoofable (GHSA-rxqh-5572-8m77). Default ON (fail-closed).
        #
        # Operators whose receiving mail server does not stamp an
        # Authentication-Results header can opt out via config.yaml:
        #   platforms:
        #     email:
        #       require_authenticated_sender: false
        # or the EMAIL_TRUST_FROM_HEADER=true env mirror (parity with the other
        # EMAIL_* access-control vars). When allow-all is in effect the operator
        # has already chosen to accept any sender, so the check is moot and the
        # gate below is skipped.
        if "require_authenticated_sender" in extra:
            self._require_authenticated_sender = bool(extra["require_authenticated_sender"])
        elif env_bool("EMAIL_TRUST_FROM_HEADER", False):
            self._require_authenticated_sender = False
        else:
            self._require_authenticated_sender = True

        # Optional authserv-id to pin Authentication-Results to the operator's
        # own receiving server (defends against an injected header that sorts
        # first). Defaults to the From-domain of the agent's own address.
        self._authserv_id = (
            extra.get("authserv_id", "") or os.getenv("EMAIL_AUTHSERV_ID", "")
        ).strip().lower()

        # Persistent store of successfully dispatched message UIDs.
        # On restart, only messages whose UIDs are in this store are skipped —
        # everything else (including messages marked SEEN by a failed dispatch)
        # will be re-fetched and re-processed.
        cache_dir = os.path.expanduser("~/.hermes/cache")
        os.makedirs(cache_dir, exist_ok=True)
        self._completed_uids_path = os.path.join(cache_dir, "email-completed-uids.json")

        # ── Email-triggered workflows ───────────────────────────────────────
        # Each workflow defines a subject_prefix, allowed_senders, and a
        # structured prompt that tells the agent what skill pipeline to run.
        # When an incoming email subject matches a workflow prefix AND the
        # sender is allow-listed, the raw email body is replaced with the
        # workflow prompt (plus attachment context) so the agent session
        # starts with the right instructions instead of a free-form chat.
        #
        # Config schema (under platforms.email.extra.workflows):
        #   citation-review:
        #     subject_prefix: "citation-review"
        #     description: "Verify citations in an attached manuscript"
        #     allowed_senders:
        #       - user@example.com
        #     skill: citation-review   # skill name to load via /skill-name
        #     prompt: |                # optional override; default auto-generated
        #
        # Allowed-sender is checked here AND at the EMAIL_ALLOWED_USERS gate
        # above — workflow senders MUST also be in EMAIL_ALLOWED_USERS.
        raw_workflows = extra.get("workflows", {}) or {}
        self._workflows: Dict[str, Dict[str, Any]] = {}
        for wf_id, wf_cfg in raw_workflows.items():
            if not isinstance(wf_cfg, dict):
                continue
            prefix = (wf_cfg.get("subject_prefix") or wf_id).strip().lower()
            allowed = {
                a.strip().lower()
                for a in wf_cfg.get("allowed_senders", [])
                if isinstance(a, str) and a.strip()
            }
            if not allowed:
                continue
            self._workflows[prefix] = {
                "id": wf_id,
                "subject_prefix": prefix,
                "allowed_senders": allowed,
                "skill": wf_cfg.get("skill", wf_id),
                "description": wf_cfg.get("description", ""),
                "prompt": wf_cfg.get("prompt", ""),
                "reply_subject": wf_cfg.get("reply_subject", ""),
            }
        if self._workflows:
            wf_names = ", ".join(
                f"{v['subject_prefix']} ({len(v['allowed_senders'])} senders)"
                for v in self._workflows.values()
            )
            logger.info("[Email] Loaded %d workflows: %s", len(self._workflows), wf_names)

        # Track message IDs we've already processed to avoid duplicates
        self._seen_uids: set = set()
        self._seen_uids_max: int = 2000   # cap to prevent unbounded memory growth
        self._poll_task: Optional[asyncio.Task] = None

        # Map chat_id (sender email) -> last subject + message-id for threading
        self._thread_context: Dict[str, Dict[str, str]] = {}

        # ── Batched-send state ──────────────────────────────────────────────
        # Email cannot edit messages — each intermediate segment would create
        # a separate email.  Instead, buffer all segment content and send the
        # accumulated response as one email after a debounce period.  A short
        # ack is sent immediately so the user knows the request was received.
        self._send_buffer: list[str] = []
        self._ack_sent = False
        self._first_reply_to: Optional[str] = None
        self._chat_id_for_flush: Optional[str] = None
        self._flush_delay: float = 30.0          # seconds of quiet before flush
        self._flush_timer: Optional[asyncio.TimerHandle] = None
        # Attachments to include in the aggregated flush email.
        # Each entry: (file_path, file_name|None)
        self._pending_attachments: list[tuple[str, Optional[str]]] = []
        # UID of the message currently being processed.  Saved on dispatch and
        # only promoted to "completed" after the final response email is sent
        # (inside _flush_send_buffer).  If the session fails before sending,
        # this UID stays uncompleted and the restart catch-up re-dispatches it.
        self._pending_msg_uid: Optional[bytes] = None

        logger.info("[Email] Adapter initialized for %s", self._address)

    def _trim_seen_uids(self) -> None:
        """Keep only the most recent UIDs to prevent unbounded memory growth.

        IMAP UIDs are monotonically increasing integers. When the set grows
        beyond the cap, we keep only the highest half — old UIDs are safe to
        drop because new messages always have higher UIDs and IMAP's UNSEEN
        flag prevents re-delivery regardless.
        """
        if len(self._seen_uids) <= self._seen_uids_max:
            return
        try:
            # UIDs are bytes like b'1234' — sort numerically and keep top half
            sorted_uids = sorted(self._seen_uids, key=lambda u: int(u))
            keep = self._seen_uids_max // 2
            self._seen_uids = set(sorted_uids[-keep:])
            logger.debug("[Email] Trimmed seen UIDs to %d entries", len(self._seen_uids))
        except (ValueError, TypeError):
            # Fallback: just clear old entries if sort fails
            self._seen_uids = set(list(self._seen_uids)[-self._seen_uids_max // 2:])

    def _load_completed_uids(self) -> set:
        """Load the set of fully-dispatch-completed message UIDs from disk."""
        try:
            with open(self._completed_uids_path) as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _append_completed_uid(self, uid: bytes) -> None:
        """Record a message UID as fully dispatched (not just fetched)."""
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        completed = self._load_completed_uids()
        completed.add(uid_str)
        # Keep the last 5000 to bound file size
        sorted_uids = sorted(completed, key=int)[-5000:]
        try:
            os.makedirs(os.path.dirname(self._completed_uids_path), exist_ok=True)
            with open(self._completed_uids_path, "w") as f:
                json.dump(sorted_uids, f)
        except Exception as e:
            logger.warning("[Email] Failed to persist completed UID: %s", e)

    def _connect_smtp(self) -> smtplib.SMTP:
        """Create an SMTP connection, selecting the correct protocol for the port.

        Port 465 uses implicit TLS (``SMTP_SSL``).  All other ports use
        ``SMTP`` + ``STARTTLS``.

        When the host resolves to an IPv6 address that is unreachable
        (common on networks without IPv6 routing), the default connection can
        hang until the socket timeout expires.  We retry connection-level
        failures through an IPv4-only socket path, without mutating global
        resolver state.  TLS verification errors are not retried.

        Returns a connected SMTP object with TLS established — callers
        can proceed directly to ``login()``.
        """
        ctx = ssl.create_default_context()
        host = self._smtp_host
        port = self._smtp_port

        def _connect(*, ipv4_only: bool = False) -> smtplib.SMTP:
            """Attempt one SMTP connection."""
            smtp_cls = _IPv4SMTP if ipv4_only else smtplib.SMTP
            smtp_ssl_cls = _IPv4SMTP_SSL if ipv4_only else smtplib.SMTP_SSL
            if port == 465:
                return smtp_ssl_cls(host, port, timeout=SMTP_CONNECT_TIMEOUT, context=ctx)
            smtp = smtp_cls(host, port, timeout=SMTP_CONNECT_TIMEOUT)
            try:
                smtp.starttls(context=ctx)
            except Exception:
                smtp.close()
                raise
            return smtp

        try:
            return _connect()
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, ssl.SSLError):
                raise
            # Connection-level failure (may be unreachable IPv6).
            # Retry with IPv4 only.
            return _connect(ipv4_only=True)

    async def connect(self, is_reconnect: bool = False) -> bool:
        """Connect to the IMAP server, seed seen-UIDs from completed store,
        and catch-up any messages that were never completed (e.g. from a
        previous gateway crash or downtime)."""
        # ── 1. Load completed UIDs from persistent store ──
        completed_uids = self._load_completed_uids()
        logger.info("[Email] Loaded %d completed message UIDs", len(completed_uids))
        self._seen_uids = {u.encode() if isinstance(u, str) else u for u in completed_uids}
        self._trim_seen_uids()

        try:
            # ── 2. Test IMAP connection + discover uncompleted messages ──
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            imap.login(self._address, self._password)
            _send_imap_id(imap)
            imap.select("INBOX")

            status, data = imap.uid("search", None, "ALL")
            catchup_uids: list = []
            if status == "OK" and data and data[0]:
                for uid in data[0].split():
                    if uid not in self._seen_uids:
                        catchup_uids.append(uid)
            imap.logout()

            logger.info(
                "[Email] IMAP connection test passed. %d completed skipped, "
                "%d uncompleted messages for catch-up.",
                len(self._seen_uids), len(catchup_uids),
            )
        except Exception as e:
            logger.error("[Email] IMAP connection failed: %s", e)
            return False

        try:
            # Test SMTP connection
            smtp = self._connect_smtp()
            try:
                smtp.login(self._address, self._password)
            finally:
                smtp.quit()
            logger.info("[Email] SMTP connection test passed.")
        except Exception as e:
            logger.error("[Email] SMTP connection failed: %s", e)
            return False

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

        # ── 3. Catch-up: dispatch any messages found on the server that
        #    were never marked completed (arrived during downtime, or a
        #    previous dispatch failed mid-way).
        if catchup_uids:
            loop = asyncio.get_running_loop()
            catchup_msgs = await loop.run_in_executor(
                None, self._fetch_by_uids, catchup_uids
            )
            for msg_data in catchup_msgs:
                await self._dispatch_message(msg_data)

        print(f"[Email] Connected as {self._address}")
        return True

    async def disconnect(self) -> None:
        """Stop polling and disconnect."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("[Email] Disconnected.")

    async def _poll_loop(self) -> None:
        """Poll IMAP for new messages at regular intervals."""
        while self._running:
            try:
                await self._check_inbox()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Email] Poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _check_inbox(self) -> None:
        """Check INBOX for unseen messages and dispatch them."""
        # Run IMAP operations in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        messages = await loop.run_in_executor(None, self._fetch_new_messages)
        for msg_data in messages:
            await self._dispatch_message(msg_data)

    def _fetch_by_uids(self, uids: list) -> List[Dict[str, Any]]:
        """Fetch messages by specific UIDs. Runs in executor thread.

        Models after _fetch_new_messages but fetches specific UIDs
        instead of scanning for UNSEEN messages.
        """
        results = []
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            try:
                imap.login(self._address, self._password)
                _send_imap_id(imap)
                imap.select("INBOX")

                uid_str = ",".join(u.decode() if isinstance(u, bytes) else str(u) for u in uids)
                status, data = imap.uid("fetch", uid_str, "(RFC822)")
                if status != "OK" or not data or not data[0]:
                    return results

                for entry in data:
                    if not isinstance(entry, tuple) or len(entry) != 2:
                        continue
                    msg_id, raw_segment = entry
                    raw_email = raw_segment
                    msg = email_lib.message_from_bytes(raw_email)

                    sender_raw = msg.get("From", "")
                    sender_addr = _extract_email_address(sender_raw)
                    sender_name = _decode_header_value(sender_raw)
                    if "<" in sender_name:
                        sender_name = sender_name.split("<")[0].strip().strip('"')

                    # Find the UID for this message from the FETCH response
                    # The UID is embedded in the FETCH response; extract from the original uid list
                    # Map by iterating the UID list — IMAP returns them in the same order
                    # but we need to match. Use the response data's UID attribute.
                    msg_uid = b""
                    if isinstance(msg_id, bytes):
                        # Parse: "1 (UID 25 RFC822 ...)" or similar
                        parts = msg_id.split()
                        for i, p in enumerate(parts):
                            if p.upper() == b"UID" and i + 1 < len(parts):
                                msg_uid = parts[i + 1]
                                break

                    subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                    message_id = msg.get("Message-ID", "")
                    in_reply_to = msg.get("In-Reply-To", "")
                    msg_headers = dict(msg.items())

                    if _is_automated_sender(sender_addr, msg_headers):
                        continue

                    sender_authenticated, auth_reason = _verify_sender_authentication(
                        msg, sender_addr, authserv_id=self._authserv_id
                    )

                    body = _extract_text_body(msg)
                    attachments = _extract_attachments(msg, skip_attachments=self._skip_attachments)

                    results.append({
                        "uid": msg_uid,
                        "sender_addr": sender_addr,
                        "sender_name": sender_name,
                        "subject": subject,
                        "message_id": message_id,
                        "in_reply_to": in_reply_to,
                        "body": body,
                        "attachments": attachments,
                        "date": msg.get("Date", ""),
                        "sender_authenticated": sender_authenticated,
                        "auth_reason": auth_reason,
                    })
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("[Email] IMAP fetch_by_uids error: %s", e)
        return results

    def _fetch_new_messages(self) -> List[Dict[str, Any]]:
        """Fetch new (unseen) messages from IMAP. Runs in executor thread."""
        results = []
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            try:
                imap.login(self._address, self._password)
                _send_imap_id(imap)
                imap.select("INBOX")

                status, data = imap.uid("search", None, "ALL")
                if status != "OK" or not data or not data[0]:
                    return results

                for uid in data[0].split():
                    if uid in self._seen_uids:
                        continue
                    self._seen_uids.add(uid)
                    # Trim periodically to prevent unbounded memory growth
                    if len(self._seen_uids) > self._seen_uids_max:
                        self._trim_seen_uids()

                    status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw_email)

                    sender_raw = msg.get("From", "")
                    sender_addr = _extract_email_address(sender_raw)
                    sender_name = _decode_header_value(sender_raw)
                    # Remove email from name if present
                    if "<" in sender_name:
                        sender_name = sender_name.split("<")[0].strip().strip('"')

                    subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                    message_id = msg.get("Message-ID", "")
                    in_reply_to = msg.get("In-Reply-To", "")
                    # Skip automated/noreply senders before any processing
                    msg_headers = dict(msg.items())
                    if _is_automated_sender(sender_addr, msg_headers):
                        logger.debug("[Email] Skipping automated sender: %s", sender_addr)
                        continue

                    # Verify the From: domain is authenticated (SPF/DKIM/DMARC)
                    # while the raw message — and its trusted
                    # Authentication-Results header — is still in scope. The
                    # verdict is consumed at dispatch where authorization is
                    # decided. From: is attacker-controlled, so this is the only
                    # place a spoof can be caught (GHSA-rxqh-5572-8m77).
                    sender_authenticated, auth_reason = _verify_sender_authentication(
                        msg, sender_addr, authserv_id=self._authserv_id
                    )

                    body = _extract_text_body(msg)
                    attachments = _extract_attachments(msg, skip_attachments=self._skip_attachments)

                    results.append({
                        "uid": uid,
                        "sender_addr": sender_addr,
                        "sender_name": sender_name,
                        "subject": subject,
                        "message_id": message_id,
                        "in_reply_to": in_reply_to,
                        "body": body,
                        "attachments": attachments,
                        "date": msg.get("Date", ""),
                        "sender_authenticated": sender_authenticated,
                        "auth_reason": auth_reason,
                    })
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("[Email] IMAP fetch error: %s", e)
        return results

    @staticmethod
    def _allow_all_senders() -> bool:
        """Return True when the operator opted into accepting any sender.

        Mirrors the gateway authz allow-all resolution: the per-platform
        EMAIL_ALLOW_ALL_USERS flag or the global GATEWAY_ALLOW_ALL_USERS flag.
        When either is set, sender identity is moot, so the From: authentication
        gate is skipped.
        """
        truthy = {"true", "1", "yes"}
        return (
            os.getenv("EMAIL_ALLOW_ALL_USERS", "").strip().lower() in truthy
            or os.getenv("GATEWAY_ALLOW_ALL_USERS", "").strip().lower() in truthy
        )

    @staticmethod
    def _allowlist_in_effect() -> bool:
        """Return True when a sender allowlist gates email access.

        Authorization keys on the From: address only when an allowlist is
        configured — the per-platform EMAIL_ALLOWED_USERS or the global
        GATEWAY_ALLOWED_USERS. When neither is set the gateway default-denies
        every sender regardless, so the spoofable From: identity grants nothing
        and the authentication gate is unnecessary.
        """
        return bool(
            os.getenv("EMAIL_ALLOWED_USERS", "").strip()
            or os.getenv("GATEWAY_ALLOWED_USERS", "").strip()
        )

    async def _dispatch_message(self, msg_data: Dict[str, Any]) -> None:
        """Convert a fetched email into a MessageEvent and dispatch it."""
        sender_addr = msg_data["sender_addr"]

        # Skip self-messages
        if sender_addr == self._address.lower():
            return

        # Never reply to automated senders
        if _is_automated_sender(sender_addr, {}):
            logger.debug("[Email] Dropping automated sender at dispatch: %s", sender_addr)
            return

        # Gate 1: Reject senders not in EMAIL_ALLOWED_USERS with an
        # automatic reply so they know they reached a managed agent.
        allowed_raw = os.getenv("EMAIL_ALLOWED_USERS", "").strip()
        if not allowed_raw:
            if os.getenv("EMAIL_ALLOW_ALL_USERS", "").strip().lower() not in {"true", "1", "yes"} and (
                os.getenv("GATEWAY_ALLOW_ALL_USERS", "").strip().lower() not in {"true", "1", "yes"}
            ):
                logger.debug(
                    "[Email] Dropping sender at dispatch — EMAIL_ALLOWED_USERS is unset "
                    "and open access is not opted in: %s",
                    sender_addr,
                )
                return
        else:
            allowed = {addr.strip().lower() for addr in allowed_raw.split(",") if addr.strip()}
            if sender_addr.lower() not in allowed:
                logger.debug("[Email] Rejecting non-allowlisted sender: %s", sender_addr)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self._send_email,
                    sender_addr,
                    "You are not authorized to access this agent.",
                    msg_data.get("message_id"),
                )
                return

        # Reject spoofed senders. The allowlist (and the gateway's own authz)
        # key on sender_addr, which comes straight from the attacker-controlled
        # From: header — so an attacker can forge From: an-allowlisted@addr to
        # get authorized (GHSA-rxqh-5572-8m77). This only matters when an
        # allowlist is actually being used to GRANT access: if no allowlist is
        # configured the gateway default-denies everyone anyway, and if allow-all
        # is on the operator already accepts any sender. So enforce From:
        # authentication exactly when an allowlist is in effect and allow-all is
        # off. Fail-closed: an unauthenticated From: is dropped before it can be
        # matched against the allowlist.
        if (
            self._require_authenticated_sender
            and self._allowlist_in_effect()
            and not self._allow_all_senders()
            and not msg_data.get("sender_authenticated", False)
        ):
            logger.warning(
                "[Email] Dropping sender with unauthenticated From: %s (%s). "
                "If your mail server does not stamp Authentication-Results, set "
                "platforms.email.require_authenticated_sender: false (or "
                "EMAIL_TRUST_FROM_HEADER=true) to accept the risk.",
                sender_addr,
                msg_data.get("auth_reason", "no verdict"),
            )
            return

        subject = msg_data["subject"]
        body = msg_data["body"].strip()
        attachments = msg_data["attachments"]
        sender_addr_lower = sender_addr.lower()

        # ── Workflow dispatch ──────────────────────────────────────────────
        # If the subject matches a configured workflow prefix AND the sender
        # is on that workflow's allow-list, replace the email body with a
        # structured prompt so the agent session starts with the right
        # pipeline instructions instead of a free-form chat.
        workflow_text = None
        workflow_reply_subject = None
        subject_lower = subject.lower().strip()

        # Sort prefixes by length descending so more specific prefixes
        # (e.g. "citation-review add") match before shorter ones ("citation-review").
        sorted_prefixes = sorted(self._workflows.keys(), key=len, reverse=True)
        for prefix in sorted_prefixes:
            wf = self._workflows[prefix]
            if subject_lower.startswith(prefix) and sender_addr_lower in wf["allowed_senders"]:
                wf_id = wf["id"]
                description = wf["description"] or f"Execute the {wf_id} workflow"
                skill_name = wf["skill"]

                # Build the workflow prompt, including original email subject and
                # body so the agent can detect user intent (e.g. "add citations"
                # vs "review citations").
                att_names = ", ".join(a["filename"] for a in attachments) if attachments else "attached document"
                body_snippet = body[:2000].strip() if body else "(empty)"
                default_prompt = (
                    f"You are running the /{wf_id} workflow sent by {sender_addr}.\n\n"
                    f"Original subject: {subject}\n"
                    f"Original body: {body_snippet}\n\n"
                    f"{description}\n\n"
                    f"1. Load the `/{skill_name}` skill and follow its instructions exactly.\n"
                    f"2. Process the attached file(s): {att_names}\n"
                    f"3. When done, reply to this email with the result.\n"
                    f"   - If producing a revised file, include MEDIA:/path/to/output in your response.\n"
                    f"   - Include a brief summary of what was done in the email body.\n"
                    f"\n"
                    f"CRITICAL: Your response on each turn IS SENT as an email reply. Do NOT write\n"
                    f"chain-of-thought, reasoning, or intermediate status in your response. Only respond\n"
                    f"when you have a complete deliverable (the annotated file with attachments).\n"
                    f"Work silently throughout the pipeline — your first response should be the final result."
                )
                workflow_text = wf["prompt"] or default_prompt
                workflow_reply_subject = wf.get("reply_subject") or f"Re: {subject}"
                logger.info(
                    "[Email] Workflow match: '%s' from %s → %s",
                    wf_id, sender_addr, skill_name,
                )
                break

        if workflow_text:
            # Workflow matched — use structured prompt, not raw email body
            text = workflow_text
        else:
            # Gate 2: No workflow matched — reject with list of approved workflows.
            # This applies to both unrecognised subjects and senders whose
            # email is not on the matching workflow's allowed_senders list.
            if not self._workflows:
                text = body
                if subject and not subject.startswith("Re:"):
                    text = f"[Subject: {subject}]\n\n{body}"
            else:
                wf_list = "\n".join(
                    f"- {w['subject_prefix']}: {w['description']}"
                    for w in self._workflows.values()
                )
                rejection = (
                    f"I can't do that. Here are the approved workflows:\n\n"
                    f"{wf_list}\n\n"
                    f"To use a workflow, send an email with a subject line that "
                    f"starts with the workflow prefix shown above."
                )
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self._send_email,
                    sender_addr,
                    rejection,
                    msg_data.get("message_id"),
                )
                self._append_completed_uid(msg_data["uid"])
                return

        # Override thread subject for workflow replies
        if workflow_reply_subject:
            self._thread_context[sender_addr] = {
                "subject": workflow_reply_subject,
                "message_id": msg_data["message_id"],
            }

        # Determine message type and media
        media_urls = []
        media_types = []
        msg_type = MessageType.TEXT

        for att in attachments:
            media_urls.append(att["path"])
            media_types.append(att["media_type"])
            if att["type"] == "image" and msg_type == MessageType.TEXT:
                msg_type = MessageType.PHOTO
            elif att["type"] == "document":
                # Document wins over PHOTO for mixed attachments: run.py's
                # image handling keys off the per-path image/* mime type
                # regardless of message_type, but document-context injection
                # gates strictly on MessageType.DOCUMENT — so DOCUMENT is the
                # only classification that surfaces both.
                msg_type = MessageType.DOCUMENT

        # Store thread context for reply threading
        self._thread_context[sender_addr] = {
            "subject": subject,
            "message_id": msg_data["message_id"],
        }

        source = self.build_source(
            chat_id=sender_addr,
            chat_name=msg_data["sender_name"] or sender_addr,
            chat_type="dm",
            user_id=sender_addr,
            user_name=msg_data["sender_name"] or sender_addr,
        )

        event = MessageEvent(
            text=text or "(empty email)",
            message_type=msg_type,
            source=source,
            message_id=msg_data["message_id"],
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=msg_data["in_reply_to"] or None,
        )

        logger.info("[Email] New message from %s: %s", sender_addr, subject)
        await self.handle_message(event)
        # Don't mark as completed yet — the agent needs to finish processing
        # and send the response.  Completion is recorded in
        # _flush_send_buffer after the aggregated response email goes out.
        # This lets failed/crashed sessions be re-dispatched on restart.
        self._pending_msg_uid = msg_data["uid"]


    def _cancel_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    def _restart_flush_timer(self) -> None:
        self._cancel_flush_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_timer = loop.call_later(
            self._flush_delay,
            self._on_flush_timer_expired,
        )

    def _on_flush_timer_expired(self) -> None:
        """Called when the debounce timer fires — flush the buffer."""
        self._flush_timer = None
        asyncio.ensure_future(self._flush_send_buffer())

    async def _flush_send_buffer(self) -> None:
        """Send buffered content + attachments as one email, then clear."""
        if not self._send_buffer and not self._pending_attachments:
            return
        to_addr = self._chat_id_for_flush
        if not to_addr:
            self._send_buffer.clear()
            self._pending_attachments.clear()
            return
        full_body = "\n\n".join(self._send_buffer)
        self._send_buffer.clear()
        try:
            loop = asyncio.get_running_loop()
            if self._pending_attachments:
                file_paths = [p for p, _ in self._pending_attachments]
                await loop.run_in_executor(
                    None,
                    self._send_email_with_attachments_flush,
                    to_addr, full_body, file_paths, self._first_reply_to,
                )
                self._pending_attachments.clear()
            else:
                await loop.run_in_executor(
                    None, self._send_email, to_addr, full_body, self._first_reply_to,
                )
            # Agent finished and response sent — mark the source message as
            # completed so restart catch-up doesn't re-dispatch it.
            if self._pending_msg_uid is not None:
                self._append_completed_uid(self._pending_msg_uid)
                self._pending_msg_uid = None
        except Exception as e:
            logger.error("[Email] Flush send failed to %s: %s", to_addr, e)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Buffer segment text and deliver in batch.  Only the ack is sent
        immediately; the full response is emailed once the agent finishes."""
        try:
            loop = asyncio.get_running_loop()

            # Always buffer the content for the final aggregated flush.
            self._send_buffer.append(content)

            if reply_to is not None and not self._ack_sent:
                # First segment: send a short acknowledgment immediately so
                # the user knows the request was received, then buffer the
                # rest for the aggregated final email.
                self._ack_sent = True
                self._first_reply_to = reply_to
                self._chat_id_for_flush = chat_id
                ack_text = (
                    "Acknowledged. Once the work is complete, "
                    "the full response will be delivered in a follow-up email."
                )
                ack_id = await loop.run_in_executor(
                    None, self._send_email, chat_id, ack_text, reply_to,
                )
                self._restart_flush_timer()
                return SendResult(success=True, message_id=ack_id)

            if reply_to is not None:
                # Intermediate segment: buffer already appended.  Restart the
                # debounce timer so the aggregated send waits for quiet.
                self._restart_flush_timer()
                return SendResult(success=True, message_id=f"buf-{id(self)}")

            # reply_to is None — from _send_fallback_final or (gated) tail-
            # flush.  The debounce timer will handle the aggregated flush;
            # no need to send separately here.
            return SendResult(success=True, message_id="batched")

        except Exception as e:
            logger.error("[Email] Send failed to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    def _send_email(
        self,
        to_addr: str,
        body: str,
        reply_to_msg_id: Optional[str] = None,
    ) -> str:
        """Send an email via SMTP. Runs in executor thread."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        # Thread context for reply
        ctx = self._thread_context.get(to_addr, {})
        subject = ctx.get("subject", "Hermes Agent")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        # Threading headers
        original_msg_id = reply_to_msg_id or ctx.get("message_id")
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            msg["References"] = original_msg_id

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp = self._connect_smtp()
        try:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent reply to %s (subject: %s)", to_addr, subject)
        return msg_id

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Email has no typing indicator — no-op."""

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image URL as part of an email body.

        ``metadata`` is accepted to honor the base-class contract; the
        email body send doesn't use it.
        """
        text = caption or ""
        text += f"\n\nImage: {image_url}"
        return await self.send(chat_id, text.strip(), reply_to)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single email with multiple MIME attachments.

        Local files are attached directly. URL images have their URL
        appended to the body (email adapter does not download remote
        images). No hard cap — email clients handle dozens of
        attachments fine, subject to SMTP message size limits.
        """
        if not images:
            return

        from urllib.parse import unquote as _unquote

        body_parts: List[str] = []
        local_paths: List[str] = []
        for image_url, alt_text in images:
            if alt_text:
                body_parts.append(alt_text)
            if image_url.startswith("file://"):
                local_path = _unquote(image_url[7:])
                if Path(local_path).exists():
                    local_paths.append(local_path)
                else:
                    logger.warning("[Email] Skipping missing image: %s", local_path)
            else:
                # Remote URLs just get linked in the body (parity with send_image)
                body_parts.append(f"Image: {image_url}")

        if not local_paths and not body_parts:
            return

        body = "\n\n".join(body_parts)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._send_email_with_attachments,
                chat_id,
                body,
                local_paths,
            )
        except Exception as e:
            logger.error("[Email] Multi-image send failed, falling back: %s", e, exc_info=True)
            await super().send_multiple_images(chat_id, images, metadata, human_delay)

    def _send_email_with_attachments(
        self,
        to_addr: str,
        body: str,
        file_paths: List[str],
    ) -> str:
        """Send an email with multiple file attachments via SMTP."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._thread_context.get(to_addr, {})
        subject = ctx.get("subject", "Hermes Agent")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        original_msg_id = ctx.get("message_id")
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            msg["References"] = original_msg_id

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        if body:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        for file_path in file_paths:
            p = Path(file_path)
            try:
                with open(p, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={p.name}")
                    msg.attach(part)
            except Exception as e:
                logger.warning("[Email] Failed to attach %s: %s", file_path, e)

        smtp = self._connect_smtp()
        try:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent multi-attachment email to %s (%d files)", to_addr, len(file_paths))
        return msg_id

    def _send_email_with_attachments_flush(
        self,
        to_addr: str,
        body: str,
        file_paths: List[str],
        reply_to_msg_id: Optional[str] = None,
    ) -> str:
        """Send aggregated text + multiple attachments in one email (flush path).

        Like ``_send_email_with_attachments`` but accepts an explicit
        ``reply_to_msg_id`` for In-Reply-To threading against the ack email.
        """
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._thread_context.get(to_addr, {})
        subject = ctx.get("subject", "Hermes Agent")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        # Use provided reply_to_msg_id for threading against the ack,
        # falling back to the thread context.
        original_msg_id = reply_to_msg_id or ctx.get("message_id")
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            msg["References"] = original_msg_id

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        if body:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        for file_path in file_paths:
            p = Path(file_path)
            try:
                with open(p, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={p.name}")
                    msg.attach(part)
            except Exception as e:
                logger.warning("[Email] Failed to attach %s: %s", file_path, e)

        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        try:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent flush email to %s (%d attachments)", to_addr, len(file_paths))
        return msg_id

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file as an email attachment.

        When the batched-send buffer is active (ack already sent), the
        attachment is queued for the aggregated flush email instead of
        being sent immediately as a separate message.
        """
        # If we're in buffered mode, queue this attachment for the flush.
        if self._ack_sent:
            self._pending_attachments.append((file_path, file_name))
            if caption:
                # Also buffer caption text so it appears in the final body.
                self._send_buffer.append(caption)
            logger.debug(
                "[Email] Queued attachment %s for flush buffer (%d pending)",
                file_path, len(self._pending_attachments),
            )
            return SendResult(success=True, message_id=f"att-{id(self)}")
        # No buffered send in progress — send immediately as a standalone email.
        try:
            loop = asyncio.get_running_loop()
            message_id = await loop.run_in_executor(
                None,
                self._send_email_with_attachment,
                chat_id,
                caption or "",
                file_path,
                file_name,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[Email] Send document failed: %s", e)
            return SendResult(success=False, error=str(e))

    def _send_email_with_attachment(
        self,
        to_addr: str,
        body: str,
        file_path: str,
        file_name: Optional[str] = None,
    ) -> str:
        """Send an email with a file attachment via SMTP."""
        msg = MIMEMultipart()
        msg["From"] = self._address
        msg["To"] = to_addr

        ctx = self._thread_context.get(to_addr, {})
        subject = ctx.get("subject", "Hermes Agent")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"
        msg["Subject"] = subject

        original_msg_id = ctx.get("message_id")
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            msg["References"] = original_msg_id

        msg["Date"] = formatdate(localtime=True)
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{self._address.split('@')[1]}>"
        msg["Message-ID"] = msg_id

        if body:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        # Attach file
        p = Path(file_path)
        fname = file_name or p.name
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={fname}")
            msg.attach(part)

        smtp = self._connect_smtp()
        try:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        return msg_id

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the email chat."""
        ctx = self._thread_context.get(chat_id, {})
        return {
            "name": chat_id,
            "type": "dm",
            "chat_id": chat_id,
            "subject": ctx.get("subject", ""),
        }


# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the Email adapter moved from gateway/platforms/email.py into this
# bundled plugin. register() exposes the platform via the registry, replacing
# the Platform.EMAIL elif in gateway/run.py, the _PLATFORM_CONNECTED_CHECKERS
# entry in gateway/config.py, the _PLATFORMS["email"] static dict in
# hermes_cli/gateway.py, and the _send_email dispatch in
# tools/send_message_tool.py. EMAIL_* env→PlatformConfig seeding stays in core.
# ──────────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process Email delivery via SMTP (one-shot). Implements the
    standalone_sender_fn contract; replaces the legacy _send_email helper."""
    import smtplib
    import ssl as _ssl
    from email.mime.text import MIMEText
    from email.utils import formatdate

    extra = getattr(pconfig, "extra", {}) or {}
    address = extra.get("address") or os.getenv("EMAIL_ADDRESS", "")
    password = os.getenv("EMAIL_PASSWORD", "")
    smtp_host = extra.get("smtp_host") or os.getenv("EMAIL_SMTP_HOST", "")
    try:
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
    except (ValueError, TypeError):
        smtp_port = 587

    if not all([address, password, smtp_host]):
        return {"error": "Email not configured (EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_HOST required)"}

    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = address
        msg["To"] = chat_id
        msg["Subject"] = "Hermes Agent"
        msg["Date"] = formatdate(localtime=True)

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls(context=_ssl.create_default_context())
        server.login(address, password)
        server.send_message(msg)
        server.quit()
        return {"success": True, "platform": "email", "chat_id": chat_id}
    except Exception as e:
        try:
            from tools.send_message_tool import _error as _e
            return _e(f"Email send failed: {e}")
        except Exception:
            return {"error": f"Email send failed: {e}"}


def _is_connected(config) -> bool:
    """Email is connected when an address is configured (in PlatformConfig.extra
    or via EMAIL_ADDRESS). Mirrors the legacy
    _PLATFORM_CONNECTED_CHECKERS[Platform.EMAIL] = bool(extra.get('address'))."""
    extra = getattr(config, "extra", {}) or {}
    if extra.get("address"):
        return True
    import hermes_cli.gateway as gateway_mod
    return bool((gateway_mod.get_env_value("EMAIL_ADDRESS") or "").strip())


def _build_adapter(config):
    """Factory wrapper that constructs EmailAdapter from a PlatformConfig."""
    return EmailAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="email",
        label="Email",
        adapter_factory=_build_adapter,
        check_fn=check_email_requirements,
        is_connected=_is_connected,
        required_env=["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST"],
        install_hint="Email uses the Python stdlib (smtplib/imaplib) — no extra deps",
        allowed_users_env="EMAIL_ALLOWED_USERS",
        allow_all_env="EMAIL_ALLOW_ALL_USERS",
        cron_deliver_env_var="EMAIL_HOME_ADDRESS",
        standalone_sender_fn=_standalone_send,
        max_message_length=50_000,
        pii_safe=True,
        emoji="📧",
        allow_update_command=True,
    )
