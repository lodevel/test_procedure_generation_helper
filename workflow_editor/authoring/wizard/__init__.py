"""Pure leaf modules for the DCDC batch wizard (no Qt, no I/O).

Each module here is a dependency-free function the wizard dialog (assembled
separately) composes. They are deliberately side-effect-free so they can be
unit-tested headlessly and reused off the UI thread.
"""
