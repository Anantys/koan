"""Centralized signal file constants for Kōan.

All ``.koan-*`` file names used for inter-process signaling and state
tracking are defined here as constants. Import from this module instead
of hardcoding the file names — renaming or auditing signal files is then
a single-file change.

PID files use a parameterized helper since the process name varies.
"""

# -- Process lifecycle signals ------------------------------------------------

STOP_FILE = ".koan-stop"
SHUTDOWN_FILE = ".koan-shutdown"
RESTART_FILE = ".koan-restart"

# -- Pause / quota signals ----------------------------------------------------

PAUSE_FILE = ".koan-pause"
PAUSE_REASON_FILE = ".koan-pause-reason"
QUOTA_RESET_FILE = ".koan-quota-reset"

# -- Status / heartbeat -------------------------------------------------------

STATUS_FILE = ".koan-status"
HEARTBEAT_FILE = ".koan-heartbeat"
RUN_HEARTBEAT_FILE = ".koan-run-heartbeat"

# -- Mode flags ----------------------------------------------------------------

FOCUS_FILE = ".koan-focus"
VERBOSE_FILE = ".koan-verbose"

# -- Project tracking ----------------------------------------------------------

PROJECT_FILE = ".koan-project"

# -- Reports / logs ------------------------------------------------------------

DAILY_REPORT_FILE = ".koan-daily-report"
DEBUG_LOG_FILE = ".koan-debug.log"

# -- Misc ----------------------------------------------------------------------

ONBOARDING_FILE = ".koan-onboarding.json"
LAST_CLEANUP_FILE = ".koan-last-cleanup"


def pid_file(process_name: str) -> str:
    """Return the signal file name for a PID file, e.g. ``.koan-pid-run``."""
    return f".koan-pid-{process_name}"
