"""
local_auth.py - Simple registration and login for the XML Insert tool.

Users register once (emp_id, name, email, dept, title).  Profiles are stored in the shared S3
bucket (users/<emp_id>.json -- see src/usage_tracker.py), NOT in a local file, so they survive pod
restarts and are the same on every replica.  Device details (computer name, Windows username) are
auto-captured from the OS.  No Azure/IT credentials required.

This module keeps the old import surface (register_user / login_user / _load_users / ...) so
existing callers keep working; it is now a thin layer over the S3-backed user store.
"""

import logging
import os
import socket

from src import usage_tracker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------
def get_device_info() -> dict:
    """Return computer name and Windows username from OS environment."""
    return {
        "computer_name": os.environ.get("COMPUTERNAME") or socket.gethostname(),
        "windows_user":  os.environ.get("USERNAME", ""),
    }


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
def _load_users() -> dict:
    """All registered users, keyed by emp_id (profile dicts: emp_id, name, email, dept, title)."""
    try:
        rows = usage_tracker.read_all_users()
    except Exception as exc:
        logger.error("Failed to load users: %s", exc)
        return {}
    return {
        str(r.get("emp_id", "")).strip(): usage_tracker._user_row_to_profile(r)
        for r in rows if str(r.get("emp_id", "")).strip()
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_all_emp_ids() -> list:
    """Return sorted list of registered employee IDs (for login dropdown)."""
    return sorted(_load_users().keys())


def is_registered(emp_id: str) -> bool:
    """Return True if emp_id is already registered."""
    return usage_tracker.lookup_user(emp_id.strip()) is not None


def register_user(emp_id: str, name: str, email: str,
                  dept: str = "", title: str = "") -> dict:
    """
    Register a new user.  Raises ValueError if emp_id already exists or is blank.
    Returns the saved profile dict.
    """
    emp_id = emp_id.strip()
    if not emp_id:
        raise ValueError("Employee ID is required.")
    if not name.strip():
        raise ValueError("Name is required.")
    if not email.strip():
        raise ValueError("Email is required.")

    if usage_tracker.lookup_user(emp_id) is not None:
        raise ValueError(
            f"Employee ID '{emp_id}' is already registered. "
            "Use the Sign In tab to log in."
        )

    usage_tracker.save_user(emp_id, name.strip(), email.strip(), dept.strip(), title.strip())
    logger.info("New user registered: emp_id=%s  name=%s", emp_id, name.strip())
    return {
        "emp_id": emp_id,
        "name":   name.strip(),
        "email":  email.strip(),
        "dept":   dept.strip(),
        "title":  title.strip(),
    }


def login_user(emp_id: str) -> dict:
    """
    Look up a registered user by emp_id.
    Raises ValueError if not found.
    Returns the profile dict.
    """
    emp_id = emp_id.strip()
    profile = usage_tracker.lookup_user(emp_id)
    if profile is None:
        raise ValueError(
            f"Employee ID '{emp_id}' not found. "
            "Please register first using the Register tab."
        )
    logger.info("User signed in: emp_id=%s", emp_id)
    return profile
