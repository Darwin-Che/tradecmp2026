"""Small controls for high-volume diagnostic output."""

import os


def verbose_orders():
    """Keep child-order chatter available without crowding normal runs."""
    return os.getenv("RIT_VERBOSE_ORDERS", "0").lower() in ("1", "true", "yes")


def order_detail(message):
    if verbose_orders():
        print(message)
