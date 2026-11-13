"""Line-limit assertions that follow the effective file_size_policy.

Under the default policy (advisory) a file over its limit is reported as a
warning. Only PIPELINE_FILE_SIZE_POLICY=enforced turns it back into a failing
assertion. The org/team layers are not consulted here: a test run reads the
environment only.
"""

import os
import warnings

FILE_SIZE_POLICY_ENV = "PIPELINE_FILE_SIZE_POLICY"


def assert_within_line_limit(count: int, max_allowed: int, label: str) -> None:
    message = f"{label} is {count} lines, over the {max_allowed}-line limit"
    if os.environ.get(FILE_SIZE_POLICY_ENV, "").strip() == "enforced":
        assert count <= max_allowed, message
    elif count > max_allowed:
        warnings.warn(message, stacklevel=2)
