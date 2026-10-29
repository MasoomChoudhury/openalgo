# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Kotak Neo Margin API

from broker.kotak.mapping.transform_data import (
    map_order_type,
    map_product_type,
    reverse_map_exchange,
)
from database.token_db import get_token
from utils.logging import get_logger

logger = get_logger(__name__)


def transform_margin_position(position):
    """
    Transform a single OpenAlgo margin position to Kotak margin format.

    Note: Kotak margin API accepts only one order at a time, not a batch.

    Args:
        position: Position in OpenAlgo format

    Returns:
        Dict in Kotak margin format or None if transformation fails
    """
    try:
        # Get the token for the symbol
        token = get_token(position["symbol"], position["exchange"])

        if not token:
            logger.warning(
                f"Token not found for symbol: {position['symbol']} on exchange: {position['exchange']}"
            )
            return None

        # Map exchange segment
        exchange_segment = reverse_map_exchange(position["exchange"])
        if not exchange_segment:
            logger.warning(f"Invalid exchange: {position['exchange']}")
            return None

        # Map transaction type
        transaction_type = "B" if position["action"].upper() == "BUY" else "S"

        # Transform the position (all values must be strings for Kotak API)
        transformed = {
            "brkName": "KOTAK",
            "brnchId": "ONLINE",
            "exSeg": exchange_segment,
            "prc": str(position.get("price", "0")),
            "prcTp": map_order_type(position["pricetype"]),
            "prod": map_product_type(position["product"]),
            "qty": str(position["quantity"]),
            "tok": str(token),
            "trnsTp": transaction_type,
        }

        return transformed

    except Exception as e:
        logger.error(f"Error transforming position: {position}, Error: {e}")
        return None


def _amount(value):
    import math
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid margin amount")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("Margin amount must be finite and nonnegative")
    return amount


def parse_margin_response(response_data):
    """Preserve API, amount and RMS distinctions; missing is never zero."""
    if (not isinstance(response_data, dict) or response_data.get("stat") != "Ok"
            or response_data.get("stCode") != 200):
        return {"status": "error", "message": "Kotak margin API did not succeed"}
    try:
        required = _amount(response_data.get("reqdMrgn"))
        insufficient = _amount(response_data.get("insufFund"))
        rms = response_data.get("rmsVldtd")
        if rms not in {"OK", "NOT_OK"}:
            raise ValueError("RMS validation is unavailable")
        return {"status": "success", "data": {
            "total_margin_required": required,
            "rms_approved": rms == "OK" and insufficient == 0,
            "rms_validation": rms, "insufficient_funds": insufficient,
            "amount_semantics": "net_order_requirement",
        }}
    except (ValueError, TypeError, OverflowError):
        return {"status": "error", "message": "Kotak margin fields are missing or invalid"}


def parse_batch_margin_response(responses):
    """A complete standalone estimate; never asserts joint-basket verification."""
    if not responses or any(not isinstance(r, dict) or r.get("status") != "success" for r in responses):
        return {"status": "error", "message": "Incomplete margin calculation"}
    try:
        amounts = [_amount(r.get("data", {}).get("total_margin_required")) for r in responses]
        insufficient = [_amount(r.get("data", {}).get("insufficient_funds")) for r in responses]
        total = _amount(sum(amounts))
        return {"status": "success", "data": {
            "total_margin_required": total,
            "rms_approved": all(r["data"].get("rms_approved") is True for r in responses),
            "insufficient_funds": sum(insufficient),
        }}
    except (ValueError, TypeError, OverflowError, AttributeError):
        return {"status": "error", "message": "Invalid margin aggregate"}
