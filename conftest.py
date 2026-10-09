"""Apply the test memory ceiling before importing application fixtures on Linux."""

import os
import sys

if sys.platform == "linux":
    import resource

    memory_mib = int(os.environ.get("ONTOKIT_TEST_MEMORY_MIB", "6144"))
    if memory_mib <= 0:
        raise ValueError("ONTOKIT_TEST_MEMORY_MIB must be positive")
    memory_bytes = memory_mib * 1024 * 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    # Preserve stricter inherited limits, and make the ceiling hard for children too.
    ceiling = min(memory_bytes, hard) if hard != resource.RLIM_INFINITY else memory_bytes
    soft = min(soft, ceiling) if soft != resource.RLIM_INFINITY else ceiling
    resource.setrlimit(resource.RLIMIT_AS, (soft, ceiling))
