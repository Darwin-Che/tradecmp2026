"""Conservative CAD liquidation value for the current portfolio."""


def _marked_value(position, book):
    """Value a position at the price available to close it now."""
    if position == 0:
        return 0.0
    if book is None:
        return None
    price = book.best_bid if position > 0 else book.best_ask
    if price is None:
        return None
    return position * price


def liquidation_value_cad(positions, books):
    """Return cash plus marked securities in CAD, or None without a needed quote.

    Long positions use bids and short positions use asks. USD assets and
    liabilities are then converted on the corresponding side of USD/CAD.
    Commissions are included because the simulator deducts them from cash.
    """
    total = float(positions.get("CAD", 0))

    for ticker in ("BULL", "BEAR"):
        position = positions.get(ticker, 0)
        value = _marked_value(position, books.get(ticker)) if position else 0.0
        if value is None:
            return None
        total += value

    usd_value = float(positions.get("USD", 0))
    ritc_position = positions.get("RITC", 0)
    if ritc_position:
        ritc_value = _marked_value(ritc_position, books.get("RITC"))
        if ritc_value is None:
            return None
        usd_value += ritc_value

    if usd_value:
        usd_book = books.get("USD")
        if usd_book is None:
            return None
        rate = usd_book.best_bid if usd_value > 0 else usd_book.best_ask
        if rate is None:
            return None
        total += usd_value * rate
    return total
