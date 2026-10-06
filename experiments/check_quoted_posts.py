"""Inspect a finite set of quoted-post pages without downloading their assets."""
import argparse
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import BASE_URL, _page_post_ids


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--output', default='issue-assets/quote-probe')
    args = cli.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        for query in ('f=2&t=1129', 'p=12277'):
            url = BASE_URL + '/viewtopic.php?' + query
            try:
                response = session.get(url, timeout=(5, 10))
                response.raise_for_status()
            except requests.RequestException as exc:
                print(url, type(exc).__name__, str(exc))
                continue
            path = output / (query.replace('&', '_') + '.html')
            path.write_text(response.text, encoding='utf-8')
            soup = BeautifulSoup(response.text, 'html.parser')
            print(url, '->', response.url, 'posts:', sorted(_page_post_ids(soup)))


if __name__ == '__main__':
    main()
