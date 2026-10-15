"""inspect-excel and flexible headings, on a FAKE workbook shaped like the school's real layout."""
from dataclasses import replace

from openpyxl import Workbook

from schoolops_sync.config import Classifier
from schoolops_sync.inspect import format_report, inspect_folder, suggest_toml
from schoolops_sync.reader import read_workbook


def fake_real_layout(folder, disc_heading):
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in ("1 Peacock", "1 Swan"):
        ws = wb.create_sheet(sheet)
        ws.append(["FAKE SCHOOL NAME 2026-27"])  # row 1: title
        ws.append(["Sr. No.", "Student Name", "Class", "Contact", "Total Amt", disc_heading, "Paid Amt", "Balance",
                   None, "2500", "old fees pending"])
        for i in range(1, 4):
            ws.append([i, f"Child {sheet} {i}", "1st", "9876500000", 20000, 1000, 5000, 14000, "x", 2500, 0])
    p = folder / f"Std 1 ({disc_heading}).xlsx"
    wb.save(p)
    return p


def test_inspect_proposes_mapping_and_flags_unknown_columns(tmp_path):
    fake_real_layout(tmp_path, "Disc. Amt")
    fake_real_layout(tmp_path, "Disc. Amount")
    rep = inspect_folder(tmp_path)
    assert rep["files"] == 2 and rep["class_sheets"] == 4 and rep["heading_rows"] == {2: 4}
    assert set(rep["mapping"]["discount"]) == {"Disc. Amt", "Disc. Amount"}
    assert set(rep["needs_decision"]) == {"2500", "old fees pending"}
    assert rep["unnamed_columns_with_values"] == {"column 9 (no heading)": 12}
    text = format_report(rep) + suggest_toml(rep)
    assert "Child" not in text and "98765" not in text and "14000" not in text  # no student data in the report
    assert "mapping_confirmed = false" in text and "#   2500" in text


def test_reader_accepts_alternative_headings_and_fixed_medium(tmp_path, cfg):
    p = fake_real_layout(tmp_path, "Disc. Amount")
    c = replace(cfg, column_alts={**cfg.column_alts, "discount": ["Disc. Amt", "Disc. Amount"]},
                medium=Classifier(source="value", regex="English"))
    pf = read_workbook(p, c)
    assert not pf.fatal and len(pf.rows) == 6
    r = pf.rows[0]
    assert r.discount == 1000 and r.medium == "English" and r.std == "1" and r.section == "Peacock"
