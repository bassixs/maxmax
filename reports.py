import re
import sqlite3
import zipfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def parse_date_range(value: str) -> tuple[date, date]:
    match = re.fullmatch(
        r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s*[-–—]\s*"
        r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s*",
        value,
    )
    if not match:
        raise ValueError("Используйте формат ДД.ММ.ГГГГ - ДД.ММ.ГГГГ")

    parts = [int(item) for item in match.groups()]
    start = date(parts[2], parts[1], parts[0])
    end = date(parts[5], parts[4], parts[3])
    if start > end:
        raise ValueError("Первая дата должна быть не позднее второй")
    return start, end


def _period_bounds(start: date, end: date) -> tuple[str, str]:
    start_dt = datetime.combine(start, time.min, tzinfo=MOSCOW_TZ)
    end_dt = datetime.combine(end, time.max, tzinfo=MOSCOW_TZ)
    return start_dt.isoformat(), end_dt.isoformat()


class ComplaintStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS complaints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        user_name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        address TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_complaints_created_at "
                    "ON complaints(created_at)"
                )

    def add(
        self,
        user_id: int,
        user_name: str,
        description: str,
        address: str,
        created_at: datetime | None = None,
    ) -> None:
        timestamp = created_at or now_moscow()
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO complaints (
                        created_at, user_id, user_name, description, address
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp.isoformat(),
                        user_id,
                        user_name,
                        description.strip(),
                        address.strip(),
                    ),
                )

    def get_period(self, start: date, end: date) -> list[sqlite3.Row]:
        start_iso, end_iso = _period_bounds(start, end)
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT created_at, description, address
                FROM complaints
                WHERE created_at BETWEEN ? AND ?
                ORDER BY created_at ASC, id ASC
                """,
                (start_iso, end_iso),
            ).fetchall()


def address_top(rows: list[sqlite3.Row], limit: int = 5) -> list[tuple[str, int]]:
    variants: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        address = " ".join(row["address"].split())
        variants[address.casefold()][address] += 1

    totals = [
        (counter.most_common(1)[0][0], sum(counter.values()))
        for counter in variants.values()
    ]
    return sorted(totals, key=lambda item: (-item[1], item[0].casefold()))[:limit]


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(reference: str, value: str, style: int = 0) -> str:
    escaped = escape(value)
    style_attr = f' s="{style}"' if style else ""
    return (
        f'<c r="{reference}" t="inlineStr"{style_attr}>'
        f"<is><t>{escaped}</t></is></c>"
    )


def create_report_xlsx(
    rows: list[sqlite3.Row],
    start: date,
    end: date,
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    table = [["Дата", "Описание", "Адрес"]]
    for row in rows:
        created_at = datetime.fromisoformat(row["created_at"]).astimezone(MOSCOW_TZ)
        table.append(
            [
                created_at.strftime("%d.%m.%Y %H:%M"),
                row["description"],
                row["address"],
            ]
        )

    sheet_rows = []
    for row_index, values in enumerate(table, start=1):
        cells = [
            _cell(
                f"{_column_name(column_index)}{row_index}",
                str(value),
                style=1 if row_index == 1 else 2 if column_index == 1 else 0,
            )
            for column_index, value in enumerate(values, start=1)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    max_row = max(1, len(table))
    title = f"Отчёт с {start:%d.%m.%Y} по {end:%d.%m.%Y}"
    worksheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>
  <dimension ref="A1:C{max_row}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>
    <col min="1" max="1" width="19" customWidth="1"/>
    <col min="2" max="2" width="55" customWidth="1"/>
    <col min="3" max="3" width="45" customWidth="1"/>
  </cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="A1:C{max_row}"/>
  <pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>
  <pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>
</worksheet>"""

    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF176B5B"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left/><right/><top/>
      <bottom style="thin"><color rgb="FFD9E2E0"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment vertical="top" horizontal="left"/>
    </xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Обращения" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    core = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title>
  <dc:creator>MAX Feedback Bot</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">{datetime.now().astimezone().isoformat()}</dcterms:created>
</cp:coreProperties>"""
    app = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>MAX Feedback Bot</Application>
</Properties>"""

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)

    return output
