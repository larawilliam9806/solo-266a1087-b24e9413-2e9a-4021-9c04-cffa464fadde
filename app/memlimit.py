"""Apply the process address-space limit on startup.

The limit mirrors the deployment constraint (a 192 MiB address space).  It is
applied from the WSGI entry point (:mod:`main`) and is a no-op unless
``SURVEY_MEMORY_LIMIT_MB`` is set, so importing the app for tests never caps
the test runner.
"""

import os
import sys

try:
    import resource
except ImportError:  # Windows: no address-space rlimit
    resource = None


def _limit_bytes_from_env():
    raw = os.environ.get("SURVEY_MEMORY_LIMIT_MB", "").strip()
    if not raw:
        return None
    try:
        megabytes = int(raw)
    except ValueError:
        return None
    if megabytes <= 0:
        return None
    return megabytes * 1024 * 1024


def apply_address_space_limit():
    limit = _limit_bytes_from_env()
    if limit is None or resource is None:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and hard < limit:
        limit = hard
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ValueError, OSError) as error:
        sys.stderr.write("无法设置地址空间上限：%s\n" % error)
        return None
    return limit
