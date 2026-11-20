"""Brief delivery choices; hosts own message identity and persistence."""

from typing import Literal

DeliveryStrategy = Literal["append", "request_only"]


def delivery_strategy(configuration) -> DeliveryStrategy:
    value = configuration.get("brief_delivery_strategy", "append")
    if value not in ("append", "request_only"):
        raise ValueError("brief_delivery_strategy must be append or request_only")
    return value
