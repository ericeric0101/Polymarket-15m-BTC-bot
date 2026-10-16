"""Explicit retention maintenance. Default invocation performs NO mutations."""
from pathlib import Path
import argparse
import json
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitoring.storage_retention import RetentionManager


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--protect', action='append', default=[])
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--pin', help='snapshot name; classification requires --apply')
    group.add_argument('--temporary', help='explicitly classify an offline snapshot with a fresh 7-day TTL')
    parser.add_argument('--unpin', action='store_true', help='explicit consent required with --temporary for a pinned snapshot')
    args = parser.parse_args(argv)
    manager = RetentionManager(args.root)
    if args.unpin and not args.temporary: parser.error('--unpin requires --temporary')
    if args.pin or args.temporary:
        print('CLASSIFICATION', args.pin or args.temporary, 'PINNED' if args.pin else 'TEMPORARY', 'APPLY' if args.apply else 'DRY_RUN')
        if args.apply: manager.classify_snapshot(args.pin or args.temporary, pinned=bool(args.pin), unpin=args.unpin)
        return
    plan = manager.plan(protect=args.protect)
    print(json.dumps({'mode': 'APPLY' if args.apply else 'DRY_RUN', 'policy': manager.policy.__dict__,
                      'candidate_count': len(plan), 'candidate_bytes': sum(row['identity'][2] for row in plan),
                      'candidates': plan}, indent=2))
    if args.apply: manager.apply(protect=args.protect)


if __name__ == '__main__': main()
