Title: urllib3-future .pth hook can leave urllib3 broken after installation

Description:

We observed environments where installing `urllib3-future` (or packages
that depend on it, such as `niquests`) leaves a `.pth` file in site-packages
that overrides `urllib3` in a way that removes expected attributes (e.g.:
`__version__`, `exceptions`) and causes import-time failures in downstream
projects (e.g., `No module named 'urllib3.exceptions'`).

Steps to reproduce (rough):

1. Install `urllib3` and `urllib3-future` (or a package that depends on it)
2. Observe that `import urllib3` may succeed but `urllib3.exceptions` or
   `urllib3.__version__` are missing, or `importlib.util.find_spec('urllib3.exceptions')`
   returns `None`.

Impact:

- Downstream packages that expect modern `urllib3` behavior break in subtle
  ways at import time.

Suggested actions for upstream:

- Avoid using a `.pth` that replaces or mutates the `urllib3` package in-place,
  or ensure it keeps the original `urllib3` semantics intact (i.e., do not create
  a namespace that hides core attributes/members).
- Provide clear upgrade/migration notes for hosts that may have mixed
  `urllib3` and `urllib3-future` installed.

Notes / local workaround:

- In our project we implemented a startup compatibility check and fail-fast
  guidance that suggests running:

  python -m pip uninstall urllib3-future -y
  python -m pip install --upgrade --force-reinstall urllib3
  python -m pip install niquests -U

and we added CI smoke tests and bootstrap verification so the problem is
caught during setup rather than later at runtime.

Please consider this a friendly bug report and feel free to ask for any
additional diagnostics or reproduction details.
