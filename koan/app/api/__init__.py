"""Kōan REST API — Flask application factory.

Disabled by default. Enable via instance/config.yaml:
    api:
      enabled: true
      port: 8420

Authentication: Bearer token via KOAN_API_TOKEN env var or api.token in config.
"""

import logging
import os
import sys
import time
from pathlib import Path

from flask import Flask, g, jsonify, request

log = logging.getLogger("koan.api")


def create_app(koan_root: Path = None, instance_dir: Path = None) -> Flask:
    """Create and configure the Flask API application.

    Args:
        koan_root: Override for testing (defaults to env KOAN_ROOT).
        instance_dir: Override for testing (defaults to koan_root/instance).
    """
    if koan_root is None:
        koan_root = Path(os.environ["KOAN_ROOT"])
    if instance_dir is None:
        instance_dir = koan_root / "instance"

    app = Flask(__name__)
    app.config["KOAN_ROOT"] = koan_root
    app.config["INSTANCE_DIR"] = instance_dir
    app.config["JSON_SORT_KEYS"] = False

    # Register blueprints
    from app.api.routes_status import bp as status_bp
    from app.api.routes_missions import bp as missions_bp
    from app.api.routes_projects import bp as projects_bp
    from app.api.routes_admin import bp as admin_bp
    from app.api.routes_observability import bp as observability_bp

    app.register_blueprint(status_bp)
    app.register_blueprint(missions_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(observability_bp)

    # Health endpoint — unauthenticated liveness probe
    from app.api.openapi_metadata import openapi_operation

    @app.route("/v1/health")
    @openapi_operation(
        mcp=True,
        mcp_description=(
            "Use this only to check REST API reachability. Use `koan_status` for "
            "normal orientation and agent readiness."
        ),
    )
    def health():
        """Check whether the REST API is alive.

        This public endpoint returns the service name and version without
        checking authentication or agent execution state.
        """
        try:
            from app import __version__
            version = __version__
        except (ImportError, AttributeError):
            version = "unknown"
        return jsonify({"status": "ok", "name": "koan", "version": version})

    # Uniform JSON error responses
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": {"code": "not_found", "message": "Resource not found"}}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": {"code": "method_not_allowed", "message": "Method not allowed"}}), 405

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"error": {"code": "internal_error", "message": "Internal server error"}}), 500

    # Per-request audit logging (no token logged)
    log_dir = koan_root / "logs"
    log_dir.mkdir(exist_ok=True)
    _audit_log_path = log_dir / "api.log"

    @app.after_request
    def _audit_log(response):
        if not app.config.get("TESTING"):
            client = request.remote_addr or "-"
            line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {client} {request.method} {request.path} {response.status_code}\n"
            try:
                with open(_audit_log_path, "a") as fh:
                    fh.write(line)
            except OSError as e:
                log.warning("audit log write failed (%s): %s", _audit_log_path, e)
        return response

    return app
