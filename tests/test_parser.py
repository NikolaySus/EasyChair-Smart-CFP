import pytest

from easychair import discover_areas, parse_cfps
from easychair import fetch_html

AREA = {"area_id": 1, "area": "Computing", "area_url": "https://easychair.org/cfp/area?area=1"}


def table(rows: str, count: int) -> str:
    return f'''<table class="ct_table"><thead><tr><td>
      <span id="ec:table1:rows">{count}</span></td></tr><tr>
      <th>Acronym</th><th>Name</th><th>Location</th>
      <th>Submission<br/>Deadline</th><th>Start Date</th><th>Topics</th>
      </tr></thead><tbody>{rows}</tbody></table>'''


ROW = '''<tr><td><a href="/cfp/Test2026">T26</a></td><td>Test conference</td>
  <td>Paris, France</td><td></td><td>October 20, 2026</td>
  <td><a href="/cfp/topic?t=1">data science</a></td></tr>'''


def test_area_links_are_discovered_and_deduplicated():
    html = '''<a href="/cfp/area?area=1">Computing</a>
      <a href="/cfp/area?area=23">Civil Engineering</a>
      <a href="/cfp/area?area=1">Computing</a><a href="/cfp/new">New CFPs</a>'''
    result = discover_areas(html)
    assert result["area_id"].tolist() == [23, 1]


def test_parser_keeps_missing_dates_url_topics_and_area():
    frame, count = parse_cfps(table(ROW, 1), AREA, "2026-10-09T10:00:00+00:00")
    assert count == 1
    row = frame.iloc[0]
    assert row["cfp_url"] == "https://easychair.org/cfp/Test2026"
    assert row["submission_deadline_raw"] == ""
    assert row["topics"] == ["data science"]
    assert row["area_id"] == 1


def test_partial_page_is_rejected_instead_of_exported():
    with pytest.raises(ValueError, match="пагинация"):
        parse_cfps(table(ROW, 100), AREA, "2026-10-09T10:00:00+00:00")


def test_empty_area_is_valid():
    frame, count = parse_cfps(table('<tr><td colspan="6">No entries</td></tr>', 0), AREA, "now")
    assert count == 0 and frame.empty


def test_login_or_changed_markup_is_rejected():
    with pytest.raises(ValueError, match="Таблица CFP"):
        parse_cfps("<html>Sign in</html>", AREA, "now")


def test_small_table_without_navigation_has_no_independent_count():
    html = table(ROW, 1).replace('<table class="ct_table">', '<table class="ct_table" data-navigation="false">')
    html = html.replace('<span id="ec:table1:rows">1</span>', '')
    frame, reported = parse_cfps(html, AREA, "now")
    assert len(frame) == 1 and reported is None


def test_empty_table_without_navigation_is_valid():
    html = table('<tr><td colspan="6">No entries</td></tr>', 0)
    html = html.replace('<table class="ct_table">', '<table class="ct_table" data-navigation="false">')
    html = html.replace('<span id="ec:table1:rows">0</span>', '')
    frame, reported = parse_cfps(html, AREA, "now")
    assert frame.empty and reported is None


def test_cache_reuses_original_download_time_and_detects_corruption(tmp_path):
    class Response:
        url = AREA["area_url"]
        text = table(ROW, 1)
        encoding = "utf-8"

        def raise_for_status(self):
            pass

    class Session:
        calls = 0

        def get(self, url, timeout):
            self.calls += 1
            return Response()

    session = Session()
    html, first = fetch_html(session, AREA["area_url"], tmp_path, "area_1", delay=0)
    again, cached = fetch_html(session, AREA["area_url"], tmp_path, "area_1", delay=0)
    assert session.calls == 1 and html == again
    assert cached["from_cache"] and cached["fetched_at_utc"] == first["fetched_at_utc"]
    (tmp_path / "area_1.html").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="контрольной"):
        fetch_html(session, AREA["area_url"], tmp_path, "area_1", delay=0)
