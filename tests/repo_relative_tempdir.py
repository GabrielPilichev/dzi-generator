"""Temporary directories addressed by a repo-relative path.

The DZI image tools accept only relative ``--image-dir`` values (see
``validate_image_dir``). ``tempfile`` alone cannot provide one portably: for a
relative ``dir`` it returned a relative name before Python 3.12 and returns an
absolute name since 3.12. Fixtures for normal, successful tool runs therefore
derive the relative path explicitly instead of depending on the interpreter.
"""

import os
import tempfile


class RepoRelativeTemporaryDirectory(tempfile.TemporaryDirectory):
    """Created in the current directory; the ``with`` value is always relative.

    ``name`` keeps whatever ``tempfile`` returned (used for cleanup);
    ``relative_name`` is the path tests pass to the tools.
    """

    def __init__(self):
        super().__init__(dir=".")
        self.relative_name = os.path.relpath(self.name)

    def __enter__(self):
        return self.relative_name
