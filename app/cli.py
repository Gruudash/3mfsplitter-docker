#!/usr/bin/env python3
"""
Command-line interface for the 3MF splitter.

Usage examples:
    python -m app.cli model.3mf
    python -m app.cli model.3mf -o ./parts -c magnet -n 4
    python -m app.cli model.3mf -c dovetail
"""

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a .3mf file by color into individually printable STL parts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Input .3mf file")
    parser.add_argument(
        "-o", "--output",
        default="output",
        help="Output directory for STL files (default: ./output)",
    )
    parser.add_argument(
        "-c", "--connector",
        choices=["none", "magnet", "peg", "dovetail"],
        default="none",
        help=(
            "Connector type to add at split boundaries:\n"
            "  none     - no connectors\n"
            "  magnet   - cylindrical holes for 5x3mm disc magnets\n"
            "  peg      - cylindrical peg+socket (Steg/Zapfen)\n"
            "  dovetail - trapezoidal plug+pocket (Schwalbenschwanz)"
        ),
    )
    parser.add_argument(
        "-n", "--n-connectors",
        type=int,
        default=3,
        metavar="N",
        help="Number of connectors per boundary (default: 3)",
    )
    parser.add_argument(
        "--merge-gap-mm",
        type=float,
        default=None,
        metavar="MM",
        help="Same-color mesh islands within this distance (mm) are fused into "
             "one part; islands farther apart stay separate parts (default: 2.0)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Error: '{args.input}' not found")
    if not args.input.lower().endswith('.3mf'):
        sys.exit("Error: input must be a .3mf file")

    # Deferred imports so CLI stays fast when not splitting
    from app.core.connectors import ConnectorType, apply_connectors
    from app.core.exporter import export_parts_as_stl
    from app.core.parser import parse_3mf
    from app.core.splitter import DEFAULT_MERGE_GAP_MM, split_by_color

    print(f"Parsing {args.input} ...")
    mesh_list = parse_3mf(args.input)
    if not mesh_list:
        sys.exit("Error: no mesh objects found in the 3MF file")

    merge_gap_mm = args.merge_gap_mm if args.merge_gap_mm is not None else DEFAULT_MERGE_GAP_MM

    all_parts = []
    for md in mesh_list:
        parts = split_by_color(md, merge_gap_mm=max(0.0, merge_gap_mm))
        print(f"  Object '{md.name}': {len(parts)} colour part(s) "
              f"[{', '.join(p.color_hex for p in parts)}]")
        all_parts.extend(parts)

    if not all_parts:
        sys.exit("Error: no coloured parts could be extracted")

    conn_type = ConnectorType(args.connector)
    if conn_type != ConnectorType.NONE:
        print(f"Adding {conn_type.value} connectors ({args.n_connectors} per boundary) ...")
        all_parts = apply_connectors(all_parts, conn_type, args.n_connectors)

    print(f"Exporting {len(all_parts)} part(s) to '{args.output}/' ...")
    paths = export_parts_as_stl(all_parts, args.output)
    for p in paths:
        print(f"  -> {p}")

    print("Done.")


if __name__ == "__main__":
    main()
