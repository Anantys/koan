"""Log rotation for Kōan process log files.

Provides backup-on-startup rotation: when a log file is opened, the previous
log is preserved as a numbered backup (.log.1, .log.2, etc.) before truncation.
Old backups are compressed with gzip. This prevents log loss on restart and
bounds disk usage for long-running instances.

Default: 3 backups, 50MB max per backup, gzip compression for .log.2+.
Configurable via instance/config.yaml under `logs:` key.
"""

import fcntl
import gzip
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

# Defaults — overridable via config.yaml `logs:` section
DEFAULT_MAX_BACKUPS = 3
DEFAULT_MAX_SIZE_MB = 50
DEFAULT_COMPRESS = True


def get_log_config(config: Optional[dict] = None) -> dict:
    """Extract log rotation settings from config dict.

    Returns dict with keys: max_backups, max_size_bytes, compress.
    """
    if config is None:
        config = {}
    logs_cfg = config.get("logs") or {}
    
    # Validate and clamp values
    max_backups = int(logs_cfg.get("max_backups", DEFAULT_MAX_BACKUPS))
    max_backups = max(1, min(max_backups, 100))  # Clamp to reasonable range
    
    max_size_mb = int(logs_cfg.get("max_size_mb", DEFAULT_MAX_SIZE_MB))
    max_size_mb = max(1, min(max_size_mb, 10240))  # Clamp to 1MB-10GB
    
    return {
        "max_backups": max_backups,
        "max_size_bytes": max_size_mb * 1024 * 1024,
        "compress": bool(logs_cfg.get("compress", DEFAULT_COMPRESS)),
    }


def rotate_log(log_path: Path, max_backups: int = DEFAULT_MAX_BACKUPS,
               compress: bool = DEFAULT_COMPRESS) -> None:
    """Rotate a log file before truncation.

    Shifts existing backups (.log.1 → .log.2, etc.), moves the current
    log to .log.1, and compresses older backups with gzip.

    Does nothing if the log file doesn't exist or is empty.
    
    Thread-safe: Uses atomic operations and proper error handling for
    concurrent access. Verifies paths are not symlinks to prevent security issues.
    """
    try:
        # Security check: don't follow symlinks
        if log_path.exists() and log_path.is_symlink():
            return
        
        if not log_path.exists() or log_path.stat().st_size == 0:
            return
    except (OSError, IOError):
        # Permission denied or other filesystem error
        return

    # Use flock for concurrent rotation protection (consistent with project conventions)
    lock_path = log_path.with_suffix(f"{log_path.suffix}.lock")
    lock_fh = None
    try:
        lock_fh = open(lock_path, "w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        # Another process is rotating — skip
        if lock_fh:
            lock_fh.close()
        return

    try:
        # Re-check file still exists after acquiring lock
        if not log_path.exists():
            return

        # Shift existing backups (highest first to avoid overwrite)
        for i in range(max_backups, 0, -1):
            src = _backup_path(log_path, i)
            if i == max_backups:
                # Delete the oldest backup (and its compressed form)
                _remove_backup(src)
                continue
            dst = _backup_path(log_path, i + 1)
            _rename_backup(src, dst)

        # Move current log → .log.1 atomically
        backup_1 = log_path.with_suffix(f"{log_path.suffix}.1")
        try:
            shutil.move(str(log_path), str(backup_1))
        except (OSError, IOError):
            # Permission denied or disk full - abort rotation
            return

        # Compress backups .log.2+ (not .log.1 — keep it readable for quick inspection)
        if compress:
            for i in range(2, max_backups + 1):
                plain = _backup_path(log_path, i)
                if plain.exists() and plain.suffix != ".gz":
                    _compress_file(plain)
    finally:
        if lock_fh:
            try:
                lock_fh.close()  # close() releases the flock
            except (OSError, ValueError):
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _backup_path(log_path: Path, index: int) -> Path:
    """Return backup path for a given index (e.g., run.log.2 or run.log.2.gz)."""
    plain = log_path.with_suffix(f"{log_path.suffix}.{index}")
    gz = Path(str(plain) + ".gz")
    # Return whichever exists, preferring compressed
    if gz.exists():
        return gz
    return plain


def _remove_backup(path: Path) -> None:
    """Remove a backup file (plain or compressed)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    
    # Also remove the other form (plain vs compressed)
    try:
        if path.suffix == ".gz":
            # Remove the plain version
            plain = Path(str(path)[:-3])  # Strip .gz
            plain.unlink(missing_ok=True)
        else:
            # Remove the compressed version
            gz = Path(str(path) + ".gz")
            gz.unlink(missing_ok=True)
    except OSError:
        pass


def _rename_backup(src: Path, dst: Path) -> None:
    """Rename a backup, handling compressed/plain forms."""
    if not src.exists():
        return
    
    # Remove destination if it exists (both forms)
    _remove_backup(dst)
    
    try:
        # Rename: if src is compressed, ensure dst is also compressed
        if src.suffix == ".gz":
            dst_gz = dst if dst.suffix == ".gz" else Path(str(dst) + ".gz")
            shutil.move(str(src), str(dst_gz))
        else:
            shutil.move(str(src), str(dst))
    except (OSError, IOError):
        # Permission denied or disk full - skip this backup
        pass


def _compress_file(path: Path) -> None:
    """Compress a file with gzip and remove the original.
    
    If compression fails, keeps the uncompressed file and cleans up partial output.
    Uses atomic write (write to temp, then rename) to prevent corruption.
    """
    # Security check: don't compress symlinks
    if path.is_symlink():
        return
    
    gz_path = Path(str(path) + ".gz")
    temp_gz = None
    
    try:
        # Write to temp file first (atomic operation)
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".log_compress_", suffix=".tmp")
        temp_gz = Path(temp_path)
        
        with os.fdopen(fd, "wb") as temp_out:
            with open(path, "rb") as f_in, gzip.open(temp_out, "wb") as gz_out:
                shutil.copyfileobj(f_in, gz_out)
        
        # Atomic rename
        temp_gz.rename(gz_path)
        temp_gz = None  # Successfully renamed, don't clean up
        
        # Remove original only after successful compression
        path.unlink()
    except (OSError, IOError):
        # Compression failed — keep uncompressed file, clean up partial gz
        if temp_gz and temp_gz.exists():
            try:
                temp_gz.unlink()
            except OSError:
                pass
        
        # Also clean up any partial gz file at final location
        try:
            if gz_path.exists() and gz_path.stat().st_size == 0:
                gz_path.unlink()
        except OSError:
            pass


def cleanup_old_backups(log_dir: Path, process_name: str,
                        max_backups: int = DEFAULT_MAX_BACKUPS) -> None:
    """Remove backup files beyond the max_backups limit."""
    base = log_dir / f"{process_name}.log"
    for i in range(max_backups + 1, max_backups + 10):
        path = _backup_path(base, i)
        if path.exists():
            try:
                # Also remove compressed form
                _remove_backup(path)
            except OSError:
                pass
        else:
            break
