"""Simple HTTP file server for serving files in web mode."""

import atexit
import os
import threading
import socket
import logging
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional
import mimetypes
import urllib.parse

logger = logging.getLogger(__name__)

_DNS_FALLBACK_HOST = os.environ.get("MM_DNS_SERVER", "8.8.8.8")
_DNS_FALLBACK_PORT = 80

# Global server instance
_file_server: Optional[ThreadingHTTPServer] = None
_file_server_thread: Optional[threading.Thread] = None
_server_port: int = 8001


class FileServerHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for file serving."""

    def do_GET(self):
        """Handle GET requests."""
        # Parse the path
        parsed_path = urllib.parse.urlparse(self.path)
        file_path = urllib.parse.unquote(parsed_path.path)

        # Remove leading slash
        if file_path.startswith("/"):
            file_path = file_path[1:]

        # Decode the file path (it's URL encoded)
        try:
            full_path = Path(file_path).resolve()

            # Security check: ensure the path is within allowed directories
            # For now, allow all paths (can be restricted later)

            if full_path.is_file() and full_path.exists():
                # Serve the file
                logger.debug(f"Serving file: {full_path}")

                # Determine content type
                content_type, _ = mimetypes.guess_type(str(full_path))
                if content_type is None:
                    content_type = "application/octet-stream"

                try:
                    with open(full_path, "rb") as f:
                        file_content = f.read()

                    self.send_response(200)
                    self.send_header("Content-type", content_type)
                    self.send_header("Content-Length", str(len(file_content)))
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{full_path.name}"'
                    )
                    self.end_headers()
                    self.wfile.write(file_content)
                    logger.info(f"Successfully served file: {full_path.name}")
                    return
                except Exception as e:
                    logger.error(f"Error serving file: {e}")
                    self.send_error(500, "Internal server error")
                    return
            else:
                logger.warning(f"File not found: {full_path}")
                self.send_error(404, "File not found")
                return

        except Exception as e:
            logger.error(f"Error handling request: {e}")
            self.send_error(400, "Bad request")

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.debug(format % args)


def get_local_ip() -> Optional[str]:
    """Get the local IP address that's accessible from other devices."""
    try:
        # Connect to a remote server to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((_DNS_FALLBACK_HOST, _DNS_FALLBACK_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.warning(f"Failed to determine local IP: {e}")
        return None


def start_file_server(port: int = 8001) -> Optional[str]:
    """Start the HTTP file server.

    Args:
        port: Port to serve on

    Returns:
        Server URL if successful, None otherwise
    """
    global _file_server, _server_thread, _server_port

    if _file_server is not None:
        logger.debug(f"File server already running on port {_server_port}")
        local_ip = get_local_ip()
        if local_ip:
            return f"http://{local_ip}:{_server_port}"
        return None

    try:
        _server_port = port

        # Create server
        server_address = ("", port)
        _file_server = ThreadingHTTPServer(server_address, FileServerHandler)

        # Start in daemon thread
        _server_thread = threading.Thread(
            target=_file_server.serve_forever,
            daemon=True
        )
        _server_thread.start()

        atexit.register(stop_file_server)

        logger.info(f"File server started on port {port}")

        # Get local IP
        local_ip = get_local_ip()
        if local_ip:
            server_url = f"http://{local_ip}:{port}"
            logger.info(f"File server accessible at: {server_url}")
            return server_url
        else:
            logger.warning("Could not determine local IP")
            return None

    except Exception as e:
        logger.error(f"Failed to start file server: {e}")
        _file_server = None
        _server_thread = None
        return None


def stop_file_server():
    """Stop the HTTP file server."""
    global _file_server, _server_thread

    if _file_server is not None:
        try:
            _file_server.shutdown()
            _file_server.server_close()
            logger.info("File server stopped")
        except Exception as e:
            logger.error(f"Error stopping file server: {e}")
        finally:
            _file_server = None
            _server_thread = None


def get_file_url(file_path: Path, server_url: Optional[str] = None) -> Optional[str]:
    """Get the HTTP URL for a file.

    Args:
        file_path: Path to the file
        server_url: Base server URL (gets determined if None)

    Returns:
        HTTP URL to the file, or None if server not running
    """
    if not file_path.exists():
        logger.warning(f"File does not exist: {file_path}")
        return None

    if server_url is None:
        local_ip = get_local_ip()
        if not local_ip:
            logger.error("Cannot determine local IP for file URL")
            return None
        server_url = f"http://{local_ip}:{_server_port}"

    # URL encode the file path
    encoded_path = urllib.parse.quote(str(file_path.resolve()))
    return f"{server_url}/{encoded_path}"
