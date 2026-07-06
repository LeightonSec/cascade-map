import argparse
import sys

from cascade_map import __version__
from cascade_map.analysis import render_report
from cascade_map.engine import load_graph, propagate, render_timeline


def _add_graph_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("graph", help="path to a YAML dependency graph")
    p.add_argument(
        "--inject", action="append", required=True, metavar="NODE_ID",
        help="node to fail at t=0 (repeatable)",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cascade-map",
        description="Dependency-reachability model for critical infrastructure.",
    )
    ap.add_argument("--version", action="version", version=f"cascade-map {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("timeline", help="deterministic failure timeline")
    _add_graph_args(t)

    a = sub.add_parser("analyze", help="full analysis report (SPOF, NIS2, Monte-Carlo)")
    _add_graph_args(a)
    a.add_argument("--runs", type=int, default=2000, help="Monte-Carlo runs")
    a.add_argument("--seed", type=int, default=0, help="Monte-Carlo RNG seed")
    a.add_argument("--no-monte", action="store_true", help="skip Monte-Carlo")

    args = ap.parse_args(argv)
    g = load_graph(args.graph)

    if args.cmd == "timeline":
        print("cascade-map — failure timeline (dependency-reachability model)")
        print(f"graph: {args.graph}   |   injected: {', '.join(args.inject)}")
        print()
        print(render_timeline(propagate(g, args.inject), len(g.nodes)))
    else:
        print(
            render_report(
                g, args.inject, runs=args.runs, seed=args.seed,
                monte=not args.no_monte,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
