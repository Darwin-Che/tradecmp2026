"""Dependency-free live terminal dashboard with scrollable logs."""

from contextlib import contextmanager
from datetime import datetime
import shutil
import sys
from threading import RLock


class _DashboardWriter:
    """Line-buffered stdout/stderr adapter that sends text above the dashboard."""

    def __init__(self, dashboard, level):
        self.dashboard = dashboard
        self.level = level
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.dashboard.log(line, self.level)
        return len(text)

    def flush(self):
        if self.buffer:
            self.dashboard.log(self.buffer, self.level)
            self.buffer = ""
        self.dashboard.stream.flush()

    def isatty(self):
        return self.dashboard.stream.isatty()


class TerminalDashboard:
    """Keep current strategy state below ordinary, scrollable log output."""

    def __init__(self, state, stream=None, enabled=None, plain_detail="compact"):
        self.state = state
        self.stream = stream or sys.stdout
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.plain_detail = plain_detail
        self._rendered_lines = 0
        self._active = False
        self._lock = RLock()

    @contextmanager
    def capture_output(self):
        """Route print calls to the log area while this context is active."""
        stdout, stderr = sys.stdout, sys.stderr
        out_writer = _DashboardWriter(self, "INFO")
        err_writer = _DashboardWriter(self, "ERROR")
        sys.stdout, sys.stderr = out_writer, err_writer
        try:
            yield self
        finally:
            out_writer.flush()
            err_writer.flush()
            sys.stdout, sys.stderr = stdout, stderr

    def start(self):
        self._active = True
        self.refresh()

    def stop(self, clear=False):
        with self._lock:
            if clear and self.enabled:
                self._erase()
            self._active = False
            self._rendered_lines = 0
            self.stream.flush()

    def log(self, message, level="INFO"):
        """Write a timestamped log line and preserve it in terminal scrollback."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            if self.enabled:
                self._erase()
            self.stream.write(f"[{timestamp}] {level:<5} {message}\n")
            if self.enabled and self._active:
                self._draw()
            self.stream.flush()

    def refresh(self):
        """Redraw the dashboard from the latest TradingState."""
        with self._lock:
            if not self.enabled:
                self.stream.write(self.state.format_state(self.plain_detail) + "\n")
                self.stream.flush()
                return
            self._erase()
            self._draw()
            self.stream.flush()

    def _erase(self):
        if self._rendered_lines:
            self.stream.write(f"\x1b[{self._rendered_lines}F\x1b[J")
            self._rendered_lines = 0

    def _draw(self):
        lines = self._render_lines()
        self.stream.write("\n".join(lines) + "\n")
        self._rendered_lines = len(lines)

    @staticmethod
    def _price_level(level):
        return "-" if level is None else f"{level.price:.4f}x{level.quantity}"

    def _book_text(self, ticker):
        book = self.state.current_books.get(ticker)
        if book is None:
            return f"{ticker}: unavailable"
        bid = book.bids[0] if book.bids else None
        ask = book.asks[0] if book.asks else None
        return (
            f"{ticker}: {self._price_level(bid)} / "
            f"{self._price_level(ask)}"
        )

    def _render_lines(self):
        columns = shutil.get_terminal_size((100, 24)).columns
        width = max(30, columns - 1)
        inner_width = width - 4
        border = "+" + "-" * (width - 2) + "+"

        def row(text):
            text = str(text)
            if len(text) > inner_width:
                text = text[:max(0, inner_width - 3)] + "..."
            return f"| {text:<{inner_width}} |"

        edge = self.state.edge_history[-1] if self.state.edge_history else None
        edge_text = "Edges: unavailable"
        if edge is not None:
            edge_text = (
                f"Net top edges CAD/unit: buy ETF {edge.buy_etf_edge_cad:+.4f} | "
                f"sell ETF {edge.sell_etf_edge_cad:+.4f}"
            )

        positions = "  ".join(
            f"{ticker} {self.state.positions.get(ticker, 0):+d}"
            for ticker in ("BULL", "BEAR", "RITC", "USD", "CAD")
        )
        market_one = f"{self._book_text('BULL')}    {self._book_text('BEAR')}"
        market_two = f"{self._book_text('RITC')}    {self._book_text('USD')}"
        execution = (
            f"Orders {len(self.state.orders)} | Tenders {len(self.state.tenders)} "
            f"| Intents {len(self.state.active_intents())} "
            f"| Bundles {len(self.state.bundles)} "
            f"| Hedge {self.state.hedge_remaining or '{}'}"
        )
        open_lots = [
            bundle for bundle in self.state.bundles.values()
            if bundle.status == "FILLED" and bundle.open_quantity > 0
        ]
        convergence = "Convergence: no open arbitrage lots"
        if open_lots:
            latest = open_lots[-1]
            percent = "-" if latest.convergence is None else f"{latest.convergence:.1%}"
            round_trip = (
                "-" if latest.estimated_round_trip_cad is None
                else f"{latest.estimated_round_trip_cad:+.2f}CAD"
            )
            convergence = (
                f"Convergence: {len(open_lots)} lots / "
                f"{sum(item.open_quantity for item in open_lots)} shares | "
                f"latest {percent} | round trip {round_trip}"
            )

        return [
            border,
            row(
                f"RITC ETF ARBITRAGE | tick {self.state.case_tick} | "
                f"case {self.state.case_status} | {self.state.strategy_status}"
            ),
            row(f"Positions: {positions}"),
            row(
                f"Marked P&L CAD: {self.state.pnl_cad:+.2f} | "
                f"risk P&L {self.state.risk_pnl_cad if self.state.risk_pnl_cad is not None else 0:+.2f} | "
                f"risk high {self.state.risk_high_water_cad:+.2f} | "
                f"gross {self.state.gross_start_fraction:.0%}->"
                f"{self.state.gross_target_fraction:.0%} | "
                f"drawdown {self.state.pnl_drawdown_active} | "
                f"loss guard {self.state.loss_growth_guard_active}"
            ),
            row(edge_text),
            row(market_one),
            row(market_two),
            row(execution),
            row(convergence),
            border,
        ]
