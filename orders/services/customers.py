"""Customer names and the placeholder accounts."""

from __future__ import annotations

from typing import Tuple

from orders.constants import FSA_FSM_NAMES


def split_name(full_name: str) -> Tuple[str, str]:
    """Capitalized (first, last) from a full name; last is "" for a one-word name."""
    parts = full_name.split()
    first = parts[0].capitalize() if parts else ""
    last = parts[1].capitalize() if len(parts) > 1 else ""
    return first, last


def customer_label(customer_name: str) -> str:
    """Who a message is about, safe to show on a dashboard: "First L." (never the full name)."""
    first, last = split_name(customer_name)
    label = f"{first} {last[0]}." if last else first
    return label or "customer"


def is_fsa_fsm(customer_name: str) -> bool:
    """Is this the "Frank Sinatra Adult"/"Frank Sinatra Medical" placeholder account? It is
    either the second order of a legal-limit split, or an unrelated walk-in sale at 3rd St
    (see dispatch_queue.link_orders)."""
    return customer_name.strip().lower() in FSA_FSM_NAMES
