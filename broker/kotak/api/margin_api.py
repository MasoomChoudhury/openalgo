import json
import urllib.parse

from broker.kotak.mapping.margin_data import (
    parse_batch_margin_response,
    parse_margin_response,
    transform_margin_position,
)
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def calculate_single_margin(position_data, auth_token):
    """
    Calculate margin for a single position using Kotak API.

    Args:
        position_data: Transformed position data in Kotak format
        auth_token: Authentication token (session_token:::session_sid:::base_url:::access_token)

    Returns:
        Tuple of (response, parsed_response_data)
    """
    # Parse auth token
    session_token, session_sid, base_url, access_token = auth_token.split(":::")[:4]

    # Debug logging for baseUrl
    logger.debug("Kotak single-order margin request")

    # Get the shared httpx client with connection pooling
    client = get_httpx_client()

    # Prepare headers
    headers = {
        "accept": "application/json",
        "Sid": session_sid,
        "Auth": session_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Prepare payload in Kotak format (URL-encoded with jData parameter)
    json_string = json.dumps(position_data)
    payload = f"jData={urllib.parse.quote(json_string)}"



    # Construct full URL
    url = f"{base_url}/quick/user/check-margin"

    try:
        # Make the request
        response = client.post(url, headers=headers, content=payload)

        # Add status attribute for compatibility
        response.status = response.status_code

        # Parse the JSON response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            logger.error("Invalid Kotak margin JSON")
            error_response = {"status": "error", "message": "Invalid response from broker API"}
            return response, error_response



        # Parse and standardize the response
        standardized_response = parse_margin_response(response_data) if response.status_code == 200 else {"status":"error","message":"Kotak margin HTTP failure"}

        return response, standardized_response

    except Exception as e:
        logger.error("Kotak margin transport failed: %s", type(e).__name__)
        error_response = {"status": "error", "message": "Kotak margin transport failed"}

        # Create a mock response object
        class MockResponse:
            status_code = 500
            status = 500

        return MockResponse(), error_response


def calculate_margin_api(positions, auth):
    """All-leg integrity with explicit standalone-only calculation provenance."""
    import hashlib
    from datetime import datetime, timezone
    from types import SimpleNamespace

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def fail(message, status=502, records=None):
        return SimpleNamespace(status_code=status, status=status), {
            "status":"error", "message":message,
            "calculation":{"version":1,"complete":False,"legs":records or []}}

    if not isinstance(positions, list) or not positions or any(not isinstance(p, dict) for p in positions):
        return fail("Invalid margin positions",400)
    try:
        request_hash=digest(positions)
        transformed=[transform_margin_position(p) for p in positions]
    except (ValueError, TypeError, KeyError):
        return fail("Invalid margin positions",400)
    if any(not p for p in transformed):
        return fail("At least one margin leg could not be resolved",400)
    results=[]; records=[]
    for index, (original, position) in enumerate(zip(positions,transformed)):
        response, parsed=calculate_single_margin(position,auth)
        status=getattr(response,"status_code",None)
        valid=status==200 and isinstance(parsed,dict) and parsed.get("status")=="success"
        record={"index":index,"request_sha256":digest(original),"http_status":status,
                "status":"success" if valid else "error","observed_at":datetime.now(timezone.utc).isoformat()}
        if valid:
            data=parsed.get("data",{})
            record.update({k:data.get(k) for k in ("total_margin_required","rms_validation","rms_approved","insufficient_funds")})
        records.append(record)
        if not valid:
            return fail("Margin provider failed for leg " + str(index),status if isinstance(status,int) and status>=400 else 502,records)
        results.append(parsed)
    aggregate=parse_batch_margin_response(results)
    if aggregate.get("status")!="success":return fail("Invalid margin aggregate",502,records)
    aggregate["data"]["calculation"]={
        "version":1,"method":"standalone_order_sum","scope":"independent_orders",
        "amount_semantics":"sum_of_net_order_requirements","complete":True,
        "verification_supported":False,"requested_count":len(positions),"result_count":len(results),
        "request_sha256":request_hash,"measured_at":datetime.now(timezone.utc).isoformat(),"legs":records,
    }
    return SimpleNamespace(status_code=200,status=200),aggregate
