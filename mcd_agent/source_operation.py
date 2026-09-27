"""Keep the installed source stable throughout an executing operation."""
from functools import wraps
import fcntl


def stable_source_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from mcd_agent.self_update import _lock_path
        path = _lock_path(kwargs["config"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_SH)
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
    return guarded
