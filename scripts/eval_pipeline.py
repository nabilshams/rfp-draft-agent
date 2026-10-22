"""Score the RFP pipeline against the golden set.

    python scripts/eval_pipeline.py                      # replay recorded model output; needs no Azure access
    python scripts/eval_pipeline.py --check              # ... and exit 1 if a metric is worse than the baseline
    python scripts/eval_pipeline.py --mode live --record # call the real model (billable) and record its answers
    python scripts/eval_pipeline.py --mode live --degrade --record   # same, with a deliberately bad prompt
    python scripts/eval_pipeline.py --write-baseline     # make this run the committed baseline

Writes ``<report-dir>/<variant>.json`` and ``.md``. See README, "Evaluating quality".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evalkit import metrics, report  # noqa: E402
from app.evalkit.golden import GOLDEN_DIR  # noqa: E402
from app.evalkit.run import run_golden, save_recordings  # noqa: E402

BASELINE = GOLDEN_DIR.parent / "baseline.json"
BASELINE_REPORT = GOLDEN_DIR.parent / "baseline.md"
DEFAULT_TOLERANCE = 0.02


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument("--variant", default="baseline", help="name of the recordings to replay or write")
    parser.add_argument("--degrade", action="store_true", help="live only: add a deliberately bad instruction to every prompt")
    parser.add_argument("--record", action="store_true", help="live only: save the model's answers as recordings")
    parser.add_argument("--case", help="run one golden case only")
    parser.add_argument("--report-dir", type=Path, default=GOLDEN_DIR.parent / "reports")
    parser.add_argument("--check", action="store_true", help="exit 1 if a metric is worse than the baseline")
    parser.add_argument("--tolerance", type=float, help=f"allowed drop per metric (default: the baseline's, else {DEFAULT_TOLERANCE})")
    parser.add_argument("--write-baseline", action="store_true", help="store this run's metrics as the baseline")
    parser.add_argument("--input-price", type=float, help="USD per million input tokens, to estimate cost")
    parser.add_argument("--output-price", type=float, help="USD per million output tokens, to estimate cost")
    args = parser.parse_args(argv)

    variant = "degraded" if args.degrade else args.variant
    if args.degrade and args.mode != "live":
        parser.error("--degrade changes the prompt, so it needs --mode live (replay uses what was recorded)")
    if args.record and args.mode != "live":
        parser.error("--record needs --mode live")
    if args.write_baseline and (args.mode != "replay" or args.case):
        parser.error("--write-baseline needs a full replay run, so the committed baseline can be reproduced")

    if args.mode == "live":
        print("Live mode: this calls the real model and is billable.", file=sys.stderr)

    baseline = report.load_baseline(BASELINE)
    run = run_golden(mode=args.mode, variant=variant, only=args.case)
    if args.record:
        save_recordings(GOLDEN_DIR, variant, run.recordings)
        print(f"Recorded {sum(len(v) for v in run.recordings.values())} answers under evals/golden/recordings/{variant}/")

    written = report.write_report(
        run,
        args.report_dir,
        baseline=baseline["metrics"] if baseline else None,
        input_price=args.input_price,
        output_price=args.output_price,
    )
    print(report.to_markdown(written, baseline["metrics"] if baseline else None))
    print(f"Report: {args.report_dir / (variant + '.md')}")

    if args.write_baseline:
        tolerance = args.tolerance if args.tolerance is not None else DEFAULT_TOLERANCE
        report.write_baseline(written, BASELINE, tolerance)
        BASELINE_REPORT.write_text(report.to_markdown(written), encoding="utf-8")
        print(f"Baseline written to {BASELINE}")
        return 0

    if args.check:
        if baseline is None:
            print("No baseline to check against; run with --write-baseline first.", file=sys.stderr)
            return 2
        tolerance = args.tolerance if args.tolerance is not None else baseline.get("tolerance", DEFAULT_TOLERANCE)
        regressions = metrics.compare(written["metrics"], baseline["metrics"], tolerance)
        if regressions:
            print("\nREGRESSION against the baseline:", file=sys.stderr)
            for line in regressions:
                print(f"  - {line}", file=sys.stderr)
            return 1
        print("\nNo regression against the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
