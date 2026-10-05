"""Private signed interfaces; credentials remain inside OpenAlgo."""
from flask import Blueprint, current_app, jsonify, request
from services.kotak_automation import authorize, ensure, status

kotak_automation_bp = Blueprint("kotak_automation", __name__, url_prefix="/internal/automation/kotak")

@kotak_automation_bp.route("/session", methods=["GET"])
@kotak_automation_bp.route("/session/ensure", methods=["POST"])
def broker_session():
    try:
        config = authorize(request)
        if request.method == "GET":
            result = status(config)
        else:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or set(payload) != {"operation_id"}:
                return jsonify(error="Only operation_id is accepted"), 400
            result = ensure(current_app._get_current_object(), config, payload["operation_id"])
        return jsonify(result), 202 if result.get("state") == "running" else 200
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
    except Exception:
        return jsonify(error="Automation storage or broker status unavailable"), 503
