"""Create offline before/after pages for the owner's 8 October PR #18 feedback.

By default use the minimal fixture extracted from the owner's attached HTML.
Pass --source to repair a copy of an existing archive instead. No requests are
made and downloaded assets are left intact.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import ForumParser


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--source', type=Path, help='Existing archive directory to copy')
    cli.add_argument('--output', type=Path, default=Path('issue-assets/archive-repair'))
    args = cli.parse_args()
    for state in ('before', 'after'):
        folder = args.output / state
        folder.mkdir(parents=True, exist_ok=True)
        if args.source:
            shutil.copytree(args.source, folder, dirs_exist_ok=True)
        else:
            fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/legacy-topic.html'
            shutil.copyfile(fixture, folder / 'viewtopic__f=3&t=1571.html')
            (folder / 'viewtopic__f=3&t=1571&start=20.html').write_text(
                '<!doctype html><meta charset="utf-8"><h1 id="p19537">Сообщение #p19537</h1>',
                encoding='utf-8')
        if state == 'after':
            archiver = ForumParser(str(folder))
            try:
                archiver.repair_archive()
            finally:
                archiver.session.close()
        print(state, folder)


if __name__ == '__main__':
    main()
