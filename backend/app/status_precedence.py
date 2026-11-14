"""Status precedence for release lifecycle transitions (spec Part II, §12.6).

Feed re-polls used to overwrite deliberate statuses (``REQUESTED`` ->
``DISCOVERY``), silently emptying the wanted list. Every automated status write
must therefore pass through :func:`resolve_status`, which protects user intent:

1. a blank/``None`` incoming value never clears an existing status;
2. an unset existing value adopts the incoming one;
3. ``DISCOVERY`` / ``NEW`` are automation-owned and therefore recomputable;
4. ``REQUESTED`` / ``DOWNLOADED`` / ``IN_CALIBRE`` are never downgraded to a classification;
5. ``IGNORED`` / ``BLACKLISTED`` form a user-veto tier automation cannot lift.

Deliberate API transitions (the UI calling ``update_release_status``) pass
``enforce_precedence=False`` and are written verbatim; automated callers pass
``enforce_precedence=True``.

The helpers are dependency-free so both NovelTrax and MangaTrax can share them.
"""

from typing import Optional

# Statuses automation owns: they may be recomputed on every poll.
CLASSIFICATION_STATUSES = frozenset({"NEW", "DISCOVERY"})

# User intent / library fact statuses that a classification must never lower.
PROTECTED_STATUSES = frozenset({"REQUESTED", "DOWNLOADED", "IN_CALIBRE"})

# User-veto tier: only an explicit API action can lift these.
VETO_STATUSES = frozenset({"IGNORED", "BLACKLISTED"})

ALL_STATUSES = CLASSIFICATION_STATUSES | PROTECTED_STATUSES | VETO_STATUSES


def normalize_status(value: Optional[str]) -> str:
    """Uppercase/strip a status value; ``None`` becomes the empty string."""
    if value is None:
        return ""
    return str(value).strip().upper()


def resolve_status(
    existing: Optional[str],
    incoming: Optional[str],
    enforce_precedence: bool = True,
) -> str:
    """Resolve the status an automated write should persist.

    ``existing`` is the status currently stored on the release, ``incoming`` the
    one the caller wants to write. With ``enforce_precedence=False`` the incoming
    value is written verbatim (the UI/API contract); with the default
    precedence is applied per spec §12.6.
    """
    existing_n = normalize_status(existing)
    incoming_n = normalize_status(incoming)

    # Verbatim mode: deliberate API transitions are unconditional.
    if not enforce_precedence:
        return incoming_n

    # Rule 1: a blank incoming value never clears an existing status.
    if not incoming_n:
        return existing_n

    # Rule 2: an unset existing status adopts the incoming one.
    if not existing_n:
        return incoming_n

    # Rule 5: the user-veto tier cannot be lifted by automation.
    if existing_n in VETO_STATUSES:
        return existing_n

    # A new veto (e.g. the user blacklisted the series) is an explicit user
    # decision carried by the caller and may be applied over any status.
    if incoming_n in VETO_STATUSES:
        return incoming_n

    # Rule 3: NEW / DISCOVERY are automation-owned, so a fresh classification
    # (or an upgrade such as NEW -> REQUESTED) replaces them.
    if existing_n in CLASSIFICATION_STATUSES:
        return incoming_n

    # Rule 4: never downgrade user intent / a library fact to a classification.
    if existing_n == "REQUESTED":
        if incoming_n in CLASSIFICATION_STATUSES:
            return existing_n
        return incoming_n

    if existing_n == "DOWNLOADED":
        # A feed poll must not turn a fetched release back into Wanted.
        if incoming_n in CLASSIFICATION_STATUSES or incoming_n == "REQUESTED":
            return existing_n
        return incoming_n

    if existing_n == "IN_CALIBRE":
        if incoming_n in CLASSIFICATION_STATUSES:
            return existing_n
        # A feed poll must not resurrect a fulfilled request over a library fact.
        if incoming_n == "REQUESTED" or incoming_n == "DOWNLOADED":
            return existing_n
        return incoming_n

    # Unknown/legacy status: adopt the incoming value.
    return incoming_n
