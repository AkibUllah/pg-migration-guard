import importlib.util
from html.parser import HTMLParser
from pathlib import Path

from migration_guard.model import RULES

SCRIPT = Path(__file__).parent.parent / "scripts" / "build_site.py"


def build(tmp_path):
    spec = importlib.util.spec_from_file_location("build_site", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stats = module.build(tmp_path)
    return stats, (tmp_path / "index.html").read_text(encoding="utf-8")


def test_page_lists_every_rule(tmp_path):
    _, page = build(tmp_path)
    assert all(code in page for code in RULES)


def test_no_placeholder_is_left(tmp_path):
    _, page = build(tmp_path)
    assert "@@" not in page


def test_page_is_well_nested(tmp_path):
    _, page = build(tmp_path)

    class Checker(HTMLParser):
        VOID = {"meta", "link", "br", "img"}
        stack: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in self.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack.pop() == tag

    checker = Checker()
    checker.feed(page)
    assert checker.stack == []


def test_counts_cover_all_four_servers(tmp_path):
    stats, _ = build(tmp_path)
    assert stats["checked"] == 34 * 4


def test_evidence_is_published_next_to_the_page(tmp_path):
    build(tmp_path)
    assert (tmp_path / "evidence" / "pg18.json").exists()
