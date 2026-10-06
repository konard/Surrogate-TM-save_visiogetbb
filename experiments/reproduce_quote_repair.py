"""Create a minimal, offline before/after reproduction of PR #18's quote repair.

The quote and post ids come from the owner's attached HTML. The target is a
small fixture representing the quoted post becoming available in the archive;
this experiment does not claim to recover the current live forum content.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from parser import ForumParser

PAGE = 'viewtopic__f=2&t=1129&start=20.html'
TARGET = 'viewtopic__f=2&t=1129.html'
SOURCE = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Цитата в сообщении #p13012</title>
<style>body { font: 18px sans-serif; margin: 40px; max-width: 950px; }
.quotecontent { background: #eee; padding: 16px; margin-top: 12px; }
pre { white-space: pre-wrap; font-size: 15px; padding: 16px; border: 1px solid #aaa; }
</style><h1>Сообщение #p13012</h1><a name="p13012"></a>
<div class="quotetitle">nbelyh в сообщении
<a class="postlink" data-archive-post="12277"
href="https://visio.getbb.ru/viewtopic.php?p=12277#p12277">#12277</a> писал(а):</div>
<div class="quotecontent">на русском вообще видео про него кто делал? А то тул крутой, а мужики-то и не знают</div>
<p>Адрес ссылки на цитируемое сообщение:</p><pre id="destination"></pre></html>'''


def show_destination(path):
    soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    soup.find(id='destination').string = soup.find('a', class_='postlink')['href']
    path.write_text(str(soup), encoding='utf-8')


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--output', default='issue-assets/quote-repair')
    args = cli.parse_args()
    output = Path(args.output)
    for state in ('before', 'after'):
        folder = output / state
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / PAGE
        page.write_text(SOURCE, encoding='utf-8')
        (folder / TARGET).write_text('<!doctype html><meta charset="utf-8"><title>Страница цитируемого сообщения</title>'
                                    '<h1 id="p12277">Сообщение #p12277</h1><p>Локальная страница с нужным якорем.</p>',
                                    encoding='utf-8')
        if state == 'after':
            ForumParser(str(folder)).resolve_post_links()
        show_destination(page)
        print(state, page)


if __name__ == '__main__':
    main()
