"""
data/flow_check.py
====================

Script de UNA sola pasada, pensado para correr como cron job (GitHub
Actions) cada 15 minutos durante el horario de mercado. No mantiene
conexión persistente: en cada ejecución sondea la cadena de opciones vía
REST (`FlowDetector.poll`), evalúa señales y manda alertas.

Como cada ejecución es un proceso nuevo, el estado de "qué señales ya
avisé" se persiste en un fichero JSON (`--state-file`, por defecto
`flow_state.json`) para no reenviar la misma alerta en cada pasada del cron
mientras el contrato siga con vol/oi alto.

Uso:
    python data/flow_check.py --tickers AAPL,TSLA,NVDA,SPY --catalysts-csv catalysts.csv
    python data/flow_check.py --check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("flow_check")

from flow_detector import (  # noqa: E402
    ContractSelector,
    FlowDetector,
    OptionsChainSnapshot,
    load_catalysts_csv,
)
from notifier import AlertNotifier  # noqa: E402
from universe import get_universe  # noqa: E402

try:
    from events_calendar import FinnhubEarningsProvider  # type: ignore
    from flow_detector import catalyst_from_earnings_event  # noqa: E402
except ImportError:
    FinnhubEarningsProvider = None

try:
    import pandas_market_calendars as mcal

    _NYSE = mcal.get_calendar("NYSE")
except ImportError:
    _NYSE = None

NY_TZ = ZoneInfo("America/New_York")


def is_market_open(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(NY_TZ)
    if _NYSE is not None:
        sched = _NYSE.schedule(start_date=now.date(), end_date=now.date())
        if sched.empty:
            return False
        open_t = sched.iloc[0]["market_open"].tz_convert(NY_TZ)
        close_t = sched.iloc[0]["market_close"].tz_convert(NY_TZ)
        return open_t <= now <= close_t
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


# --------------------------------------------------------------------------- #
#  Estado persistido entre ejecuciones (dedup de señales + último refresco)
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    if not path.exists():
        return {"already_signaled": [], "last_catalyst_refresh": None}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Estado corrupto o ilegible (%s), empezando de cero: %s", path, exc)
        return {"already_signaled": [], "last_catalyst_refresh": None}


def save_state(path: Path, detector: FlowDetector, catalyst_refresh_date: Optional[str]) -> None:
    state = {
        "already_signaled": sorted(detector._already_signaled),  # noqa: SLF001
        "last_catalyst_refresh": catalyst_refresh_date,
    }
    path.write_text(json.dumps(state, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Chequeo puntual de flow (para cron)")
    parser.add_argument("--tickers", type=str, default=None, help="Lista fija, ej. AAPL,TSLA. Ignorado si usas --universe.")
    parser.add_argument(
        "--universe",
        type=str,
        default=None,
        choices=["sp500", "nasdaq100", "both"],
        help="Vigila un índice completo en vez de una lista fija (se cruza con el calendario de catalizadores).",
    )
    parser.add_argument("--universe-cache", type=str, default="universe_cache.json")
    parser.add_argument("--min-vol-oi", type=float, default=2.0)
    parser.add_argument("--min-premium", type=float, default=50_000.0)
    parser.add_argument("--event-window-days", type=int, default=5)
    parser.add_argument("--catalysts-csv", type=str, default=None)
    parser.add_argument("--state-file", type=str, default="flow_state.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Para pruebas manuales")
    args = parser.parse_args()

    if args.universe:
        indices = {"sp500", "nasdaq100"} if args.universe == "both" else {args.universe}
        tickers = get_universe(indices, cache_path=args.universe_cache)
        logger.info("Universo '%s': %d tickers en vigilancia.", args.universe, len(tickers))
    else:
        raw = args.tickers or "AAPL,TSLA,NVDA,SPY"
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    state_path = Path(args.state_file)

    if args.check:
        api_key = os.getenv("POLYGON_API_KEY")
        if not api_key:
            print("Falta POLYGON_API_KEY en el entorno.", file=sys.stderr)
            return 1
        from polygon import RESTClient

        chain = OptionsChainSnapshot(RESTClient(api_key))
        sample = chain.fetch(tickers[0])
        print(f"OK: {len(sample)} contratos obtenidos para {tickers[0]}.")
        return 0

    now = datetime.now(NY_TZ)
    if not args.ignore_market_hours and not is_market_open(now):
        logger.info("Mercado cerrado (%s ET). Salgo sin hacer nada.", now.strftime("%Y-%m-%d %H:%M"))
        return 0

    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        print("Falta POLYGON_API_KEY en el entorno.", file=sys.stderr)
        return 1
    from polygon import RESTClient

    chain = OptionsChainSnapshot(RESTClient(api_key))
    detector = FlowDetector(
        chain=chain,
        min_vol_oi_ratio=args.min_vol_oi,
        min_premium=args.min_premium,
        event_window_days=args.event_window_days,
    )
    selector = ContractSelector(chain=chain)
    notifier = AlertNotifier()

    # -- restaura estado (evita re-alertar en cada pasada del cron) --------
    state = load_state(state_path)
    detector._already_signaled = set(state.get("already_signaled", []))  # noqa: SLF001

    # -- refresca catalizadores solo una vez al día -------------------------
    today_str = date.today().isoformat()
    if state.get("last_catalyst_refresh") != today_str:
        logger.info("Primer chequeo del día: refrescando catalizadores...")
        if FinnhubEarningsProvider is not None:
            try:
                provider = FinnhubEarningsProvider()
                events = provider.fetch(
                    tickers, date.today(), date.today() + timedelta(days=args.event_window_days)
                )
                detector.register_catalysts(catalyst_from_earnings_event(e) for e in events)
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudieron cargar earnings de Finnhub: %s", exc)
        if args.catalysts_csv:
            detector.register_catalysts(load_catalysts_csv(args.catalysts_csv))
    else:
        # catalizadores ya cargados hoy en una pasada anterior del cron:
        # los recargamos igualmente porque este proceso es nuevo (no hay
        # memoria entre ejecuciones) y la llamada es barata.
        if FinnhubEarningsProvider is not None:
            try:
                provider = FinnhubEarningsProvider()
                events = provider.fetch(
                    tickers, date.today(), date.today() + timedelta(days=args.event_window_days)
                )
                detector.register_catalysts(catalyst_from_earnings_event(e) for e in events)
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudieron cargar earnings de Finnhub: %s", exc)
        if args.catalysts_csv:
            detector.register_catalysts(load_catalysts_csv(args.catalysts_csv))

    # -- sondeo y evaluación (poll() ya devuelve las señales ordenadas por score) --
    signals = detector.poll(tickers)
    for i, signal in enumerate(signals, start=1):
        candidate = selector.pick(signal)
        score = detector.score_signal(signal)
        rank = f"{i}/{len(signals)} de esta pasada" if len(signals) > 1 else None
        notifier.notify(signal, candidate, score=score, rank=rank)

    save_state(state_path, detector, today_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
