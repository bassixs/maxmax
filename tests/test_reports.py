import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path

from reports import (
    MOSCOW_TZ,
    ComplaintStore,
    address_top,
    create_report_xlsx,
    parse_date_range,
)


class ReportTests(unittest.TestCase):
    def test_parse_inclusive_period(self):
        start, end = parse_date_range("01.06.2026 - 25.06.2026")
        self.assertEqual(start, date(2026, 6, 1))
        self.assertEqual(end, date(2026, 6, 25))

    def test_invalid_period_order(self):
        with self.assertRaises(ValueError):
            parse_date_range("25.06.2026 - 01.06.2026")

    def test_store_top_and_xlsx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ComplaintStore(root / "complaints.db")
            timestamp = datetime(2026, 6, 25, 10, 30, tzinfo=MOSCOW_TZ)

            store.add(1, "Иван", "Описание", "Салтыкова-Щедрина", timestamp)
            store.add(2, "Анна", "", "Салтыкова-Щедрина", timestamp)
            store.add(3, "Пётр", "", "салтыковка", timestamp)

            rows = store.get_period(date(2026, 6, 25), date(2026, 6, 25))
            self.assertEqual(len(rows), 3)
            self.assertEqual(address_top(rows)[0], ("Салтыкова-Щедрина", 2))

            output = create_report_xlsx(
                rows,
                date(2026, 6, 25),
                date(2026, 6, 25),
                root / "report.xlsx",
            )
            self.assertTrue(output.exists())
            with zipfile.ZipFile(output) as workbook:
                self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
                sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("Салтыкова-Щедрина", sheet)
                self.assertIn("салтыковка", sheet)

            self.assertEqual(store.clear(), 3)
            self.assertEqual(
                store.get_period(date(2026, 6, 25), date(2026, 6, 25)),
                [],
            )


if __name__ == "__main__":
    unittest.main()
