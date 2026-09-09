"""SSL certificate bundle resolution helpers.

This module is intentionally lightweight (no httpx import) so it can be used by
providers that still rely on `requests` without paying the import cost of the
full HTTP client stack.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Union

logger = logging.getLogger(__name__)


def resolve_verify_value(verify_ssl: bool) -> Union[bool, str]:
    """Return the value suitable for `requests`/`httpx` verify parameters.

    - If verify_ssl is not True (False or a path-like string), it is returned.
    - Respects an existing SSL_CERT_FILE env var.
    - Tries optional helpers (`pip_system_certs`, `certifi_win32`).
    - Falls back to `certifi.where()`.
    - Otherwise returns True.
    """

    if verify_ssl is not True:
        return verify_ssl

    env_cert = os.environ.get("SSL_CERT_FILE")
    if env_cert:
        return env_cert

    def _try_module_bundle(mod_name: str) -> Optional[str]:
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                import importlib.util

                spec = importlib.util.find_spec(mod_name)
                if spec is None:
                    return None
                import importlib

                mod = importlib.import_module(mod_name)
            except Exception:
                return None

        for attr in ("where", "get_ca_bundle", "bundle_path", "get_bundle_path", "get_bundle"):
            fn = getattr(mod, attr, None)
            if callable(fn):
                try:
                    res = fn()
                    if res:
                        return str(res)
                except Exception:
                    continue
            elif isinstance(fn, str) and fn:
                return fn

        for call_attr in ("add_windows_store_certs", "add_system_certs", "merge_system_certs"):
            fn = getattr(mod, call_attr, None)
            if callable(fn):
                try:
                    fn()
                    try:
                        import certifi as _certifi  # type: ignore

                        res = _certifi.where()
                        if res:
                            return str(res)
                    except Exception:
                        logger.exception("Failed while probing certifi helper inner block")
                except Exception:
                    logger.exception("Failed while invoking cert helper function")
        return None

    for mod_name in ("pip_system_certs", "certifi_win32"):
        path = _try_module_bundle(mod_name)
        if path:
            try:
                os.environ["SSL_CERT_FILE"] = path
            except Exception:
                logger.exception("Failed to set SSL_CERT_FILE environment variable")
            logger.info(f"SSL_CERT_FILE not set; using bundle from {mod_name}: {path}")
            return path

    try:
        import certifi  # type: ignore

        path = certifi.where()
        if path:
            try:
                os.environ["SSL_CERT_FILE"] = path
            except Exception:
                logger.exception("Failed to set SSL_CERT_FILE environment variable during certifi fallback")
            logger.info(f"SSL_CERT_FILE not set; using certifi bundle: {path}")
            return str(path)
    except Exception:
        logger.exception("Failed to probe certifi for trust bundle")

    return True
