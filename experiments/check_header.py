"""Apply issue #17 header cleanup to real topic pages saved in experiments/."""
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from parser import _remove_forum_chrome

for name in sys.argv[1:]:
    soup = BeautifulSoup(open(name, encoding="utf-8").read(), "html.parser")
    _remove_forum_chrome(soup)
    print([str(t) for t in soup.select("#pagecontent td.gensmall b")][:1])
    img = soup.find("img", src=lambda x: x and "icon_post_target" in x)
    print(img.parent.parent)
