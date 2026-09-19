"""One execution summary per completed tender or arbitrage bundle."""


def log_completed_bundles(state):
    for bundle in state.bundles.values():
        if bundle.status != "FILLED" or bundle.completion_logged:
            continue
        legs = []
        fees = {"CAD": 0.0, "USD": 0.0}
        missing_prices = False
        unknown_fees = False
        for intent_id in bundle.intent_ids:
            intent = state.intents[intent_id]
            orders = [state.orders[order_id] for order_id in intent.order_ids
                      if order_id in state.orders]
            priced = sum(order.filled for order in orders if order.vwap is not None)
            notional = sum(order.filled * order.vwap for order in orders
                           if order.vwap is not None)
            filled = intent.filled_quantity
            if priced != filled:
                missing_prices = True
            if sum(order.filled for order in orders) != filled:
                unknown_fees = True
            vwap = f"{notional / priced:.4f}" if priced == filled and priced else "?"
            legs.append(f"{intent.ticker}:{intent.action}:{filled}@{vwap}")
            currency = "USD" if intent.ticker == "RITC" else "CAD"
            for order in orders:
                if order.filled and order.fee_per_share is None:
                    unknown_fees = True
                elif order.filled:
                    fees[currency] += order.filled * order.fee_per_share

        prefix = (
            "TENDER COMPLETE" if bundle.reason.startswith("TENDER_")
            else "BUNDLE COMPLETE"
        )
        tender_detail = ""
        if prefix == "TENDER COMPLETE":
            tender_id = int(bundle.bundle_id.split("-")[1])
            tender = state.tenders.get(tender_id)
            if tender is not None:
                tender_detail = (
                    f" tender={tender_id} {tender.action} "
                    f"{tender.quantity}@{tender.price:.4f}USD"
                )
        fee_text = (
            "unknown" if unknown_fees else
            f"{fees['CAD']:.2f}CAD+{fees['USD']:.2f}USD"
        )
        bull = state.positions.get("BULL", 0)
        bear = state.positions.get("BEAR", 0)
        ritc = state.positions.get("RITC", 0)
        portfolio_gross = abs(bull) + abs(bear) + 2 * abs(ritc)
        unfilled = sum(abs(state.intents[item].remaining)
                       for item in bundle.intent_ids)
        print(
            f"{prefix} | id={bundle.bundle_id}{tender_detail} "
            f"reason={bundle.reason} tick={state.case_tick} "
            f"planned_net={bundle.expected_profit_cad:+.2f}CAD "
            f"offset={bundle.offset_quantity} opening={bundle.open_quantity} "
            f"legs={','.join(legs)} fee_est={fee_text} "
            f"unfilled_legs={unfilled} "
            f"portfolio=BULL:{bull:+d},BEAR:{bear:+d},RITC:{ritc:+d} "
            f"portfolio_gross={portfolio_gross} "
            f"prices_complete={not missing_prices}"
        )
        bundle.completion_logged = True
