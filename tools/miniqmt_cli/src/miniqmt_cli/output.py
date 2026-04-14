"""Output formatting for miniqmt-cli. Works with list[dict] or pandas DataFrame."""
from __future__ import annotations

import io
import json
from typing import Any, Iterable, List, Sequence, Union

import pandas as pd
from rich.console import Console
from rich.table import Table

RowsLike = Union[pd.DataFrame, List[dict], dict]


def _to_df(data: RowsLike) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data
    if isinstance(data, dict):
        return pd.DataFrame([data])
    if isinstance(data, list):
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    raise TypeError(f"unsupported data type: {type(data)!r}")


def format_output(data: RowsLike, fmt: str) -> str:
    df = _to_df(data)
    if fmt == "json":
        if df.empty:
            return "[]"
        return df.to_json(orient="records", force_ascii=False, indent=2)
    if fmt == "csv":
        return df.to_csv(index=False)
    if fmt == "table":
        return _to_rich_table(df)
    raise ValueError(f"unknown format: {fmt!r}; choose table, json, or csv")


def format_row(row: dict, fmt: str) -> str:
    """Format a single streaming row."""
    if fmt == "json":
        return json.dumps(row, ensure_ascii=False)
    if fmt == "csv":
        # csv: header once is caller's job; emit values only
        return ",".join(str(v) for v in row.values())
    if fmt == "table":
        # compact key=value rendering for streams
        return " ".join(f"{k}={v}" for k, v in row.items())
    raise ValueError(f"unknown format: {fmt!r}")


def _to_rich_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(no data)"
    table = Table(show_header=True, header_style="bold cyan")
    for col in df.columns:
        table.add_column(str(col))
    for _, row in df.iterrows():
        table.add_row(*[str(v) for v in row])
    buf = io.StringIO()
    console = Console(file=buf, highlight=False)
    console.print(table)
    return buf.getvalue()
