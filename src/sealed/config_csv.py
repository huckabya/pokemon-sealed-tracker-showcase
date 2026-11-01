"""One reader for the hand-edited config CSVs."""
import csv
from collections.abc import Sequence
from pathlib import Path


class ConfigCsvError(ValueError):
    """A config CSV that cannot be trusted: wrong header, a row with the wrong field count, or an empty file."""

    def __init__(self, file: str, reason: str, row: int | None = None, header: list[str] | None = None):
        super().__init__(f"{file}: row {row} {reason}" if row else f"{file}: {reason}")
        self.file, self.reason, self.row, self.header = file, reason, row, header

    def as_loader_text(self, label: str, columns: Sequence[str]) -> str:
        """The message in a loader's own words: "<label> row N, column <last>: <reason> [<file>]"."""
        if self.row:
            return f"{label} row {self.row}, column {columns[-1]}: {self.reason} [{self.file}]"
        missing = [c for c in columns if c not in (self.header or [])]
        if missing:
            return f"{label} header is missing column(s) {missing} [{self.file}]"
        return f"{label} {self.reason} [{self.file}]"


def rows(path: Path, columns: Sequence[str], *, ordered: bool = False) -> list[dict[str, str]]:
    """Rows of a hand-edited config CSV, read as utf-8-sig, after checking that every one of `columns`
    is in the header (any order and extra header columns ignored, unless `ordered`, which requires the header to
    equal `columns` exactly). A row with more fields than the header is refused; missing trailing
    fields read as "". Values are stripped; empty strings stay empty."""
    name = Path(path).name
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise ConfigCsvError(name, "no header")
        if any(c not in header for c in columns) or (ordered and header != list(columns)):
            raise ConfigCsvError(name, f"header is {header}, expected {list(columns)}", header=header)
        return [_row(name, n, header, columns, fields) for n, fields in enumerate((f for f in reader if f), 1)]


def _row(name: str, n: int, header: list[str], columns: Sequence[str], fields: list[str]) -> dict[str, str]:
    if len(fields) > len(header):
        raise ConfigCsvError(name, "has more fields than the header (unquoted comma?)", row=n)
    by_name = {h: v.strip() for h, v in zip(header, fields)}
    return {c: by_name.get(c, "") for c in columns}
