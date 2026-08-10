from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Sequence, Tuple

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Dataset, DatasetVersion, Source
from ..schemas import DatasetColumn, DatasetQuery, DatasetQueryResult

MAX_DATASET_ROWS = 50_000
MAX_DATASET_COLUMNS = 200
MAX_CELL_CHARACTERS = 20_000
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024


class AnalyticsValidationError(ValueError):
    pass


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnalyticsValidationError("Dataset must be UTF-8 encoded") from exc


def _validate_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    text = str(value)
    if len(text) > MAX_CELL_CHARACTERS:
        raise AnalyticsValidationError(
            f"Dataset cell exceeds {MAX_CELL_CHARACTERS:,} characters"
        )
    return text


def _parse_csv(data: bytes) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(_decode(data)))
    if not reader.fieldnames:
        raise AnalyticsValidationError("CSV requires a header row")
    headers = [str(name or "").strip() for name in reader.fieldnames]
    if any(not header for header in headers) or len(set(headers)) != len(headers):
        raise AnalyticsValidationError("CSV headers must be non-empty and unique")
    if len(headers) > MAX_DATASET_COLUMNS:
        raise AnalyticsValidationError(
            f"Dataset exceeds the {MAX_DATASET_COLUMNS}-column limit"
        )
    rows: List[Dict[str, Any]] = []
    for index, row in enumerate(reader):
        if index >= MAX_DATASET_ROWS:
            raise AnalyticsValidationError(
                f"Dataset exceeds the {MAX_DATASET_ROWS:,}-row limit"
            )
        rows.append(
            {
                header: _validate_cell(row.get(original))
                # headers is built from fieldnames above, so lengths always match.
                for header, original in zip(headers, reader.fieldnames, strict=True)
            }
        )
    return rows


def _parse_json(data: bytes) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        raise AnalyticsValidationError("Dataset JSON is invalid") from exc
    if not isinstance(parsed, list):
        raise AnalyticsValidationError("Dataset JSON must be an array of objects")
    if len(parsed) > MAX_DATASET_ROWS:
        raise AnalyticsValidationError(
            f"Dataset exceeds the {MAX_DATASET_ROWS:,}-row limit"
        )
    if any(not isinstance(row, dict) for row in parsed):
        raise AnalyticsValidationError("Every dataset row must be a JSON object")
    columns: List[str] = []
    for row in parsed:
        for raw_name in row:
            name = str(raw_name)
            if not name.strip():
                raise AnalyticsValidationError("Dataset columns must be non-empty")
            if name not in columns:
                columns.append(name)
    if len(columns) > MAX_DATASET_COLUMNS:
        raise AnalyticsValidationError(
            f"Dataset exceeds the {MAX_DATASET_COLUMNS}-column limit"
        )
    return [
        {column: _validate_cell(row.get(column)) for column in columns}
        for row in parsed
    ]


def _coerce_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    lowered = stripped.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?(?:0|[1-9]\d*)", stripped):
        try:
            return int(stripped)
        except ValueError:
            pass
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?", stripped):
        try:
            number = float(stripped)
            return number if math.isfinite(number) else value
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        return parsed.isoformat()
    except ValueError:
        pass
    try:
        parsed_date = date.fromisoformat(stripped)
        return parsed_date.isoformat()
    except ValueError:
        return value


def _column_type(
    values: Sequence[Any],
) -> Literal["boolean", "integer", "number", "string", "date", "datetime"]:
    populated = [value for value in values if value is not None]
    if not populated:
        return "string"
    if all(isinstance(value, bool) for value in populated):
        return "boolean"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in populated):
        return "integer"
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in populated
    ):
        return "number"
    if all(
        isinstance(value, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        for value in populated
    ):
        return "date"
    if all(
        isinstance(value, str)
        and "T" in value
        and _is_datetime(value)
        for value in populated
    ):
        return "datetime"
    return "string"


def _is_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def parse_tabular_source(source: Source) -> Tuple[List[Dict[str, Any]], List[DatasetColumn]]:
    suffix = Path(source.filename).suffix.lower()
    data = Path(source.object_key).read_bytes()
    if suffix == ".csv":
        rows = _parse_csv(data)
    elif suffix == ".json":
        rows = _parse_json(data)
    else:
        raise AnalyticsValidationError("Datasets can only be created from CSV or JSON sources")
    if not rows:
        raise AnalyticsValidationError("Dataset contains no rows")
    columns = list(rows[0])
    for row in rows:
        for column in columns:
            row[column] = _coerce_scalar(row.get(column))
    schema = [
        DatasetColumn(
            name=column,
            type=_column_type([row.get(column) for row in rows]),
            nullable=any(row.get(column) is None for row in rows),
        )
        for column in columns
    ]
    return rows, schema


def create_dataset_version(
    db: Session,
    *,
    dataset: Dataset,
    source: Source,
    version: int,
) -> DatasetVersion:
    rows, schema = parse_tabular_source(source)
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(canonical) > MAX_SNAPSHOT_BYTES:
        raise AnalyticsValidationError(
            f"Normalized dataset exceeds the {MAX_SNAPSHOT_BYTES // 1024 // 1024} MB limit"
        )
    digest = hashlib.sha256(canonical).hexdigest()
    settings = get_settings()
    directory = settings.objects_dir / dataset.workspace_id / "datasets" / dataset.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"v{version}-{digest[:12]}.json"
    temporary = directory / f".{path.name}.tmp"
    temporary.write_bytes(canonical)
    temporary.replace(path)
    item = DatasetVersion(
        workspace_id=dataset.workspace_id,
        dataset_id=dataset.id,
        source_id=source.id,
        version=version,
        format="json",
        schema_json=json.dumps([column.model_dump() for column in schema]),
        row_count=len(rows),
        content_hash=digest,
        object_key=str(path),
    )
    db.add(item)
    db.flush()
    return item


def current_dataset_version(
    db: Session,
    *,
    workspace_id: str,
    dataset_id: str,
) -> Tuple[Dataset, DatasetVersion]:
    dataset = db.scalar(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.workspace_id == workspace_id,
        )
    )
    if dataset is None:
        raise AnalyticsValidationError("Dataset not found")
    version = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.workspace_id == workspace_id,
            DatasetVersion.version == dataset.current_version,
        )
    )
    if version is None:
        raise AnalyticsValidationError("Dataset version not found")
    return dataset, version


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_columns(version: DatasetVersion) -> List[DatasetColumn]:
    return [DatasetColumn.model_validate(item) for item in json.loads(version.schema_json)]


def _validate_query(query: DatasetQuery, schema: List[DatasetColumn]) -> None:
    columns = {column.name: column for column in schema}
    for item in query.filters:
        if item.field not in columns:
            raise AnalyticsValidationError(f"Unknown filter field: {item.field}")
    if query.group_by and query.group_by not in columns:
        raise AnalyticsValidationError(f"Unknown group field: {query.group_by}")
    for metric in query.metrics:
        if metric.operation != "count" and not metric.field:
            raise AnalyticsValidationError(f"{metric.operation} requires a field")
        if metric.field and metric.field not in columns:
            raise AnalyticsValidationError(f"Unknown metric field: {metric.field}")
        if (
            metric.operation in {"sum", "avg"}
            and metric.field
            and columns[metric.field].type not in {"integer", "number"}
        ):
            raise AnalyticsValidationError(
                f"{metric.operation} requires a numeric field: {metric.field}"
            )
    result_columns = set(columns)
    if query.group_by or query.metrics:
        result_columns = ({query.group_by} if query.group_by else set()) | {
            metric.label for metric in query.metrics
        }
        if query.group_by and not query.metrics:
            result_columns.add("count")
    if query.order_by and query.order_by not in result_columns:
        raise AnalyticsValidationError(f"Unknown order field: {query.order_by}")


def execute_dataset_query(
    db: Session,
    *,
    workspace_id: str,
    dataset_id: str,
    query: DatasetQuery,
) -> DatasetQueryResult:
    _dataset, version = current_dataset_version(
        db,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
    )
    schema = _schema_columns(version)
    _validate_query(query, schema)

    selected: List[str] = []
    group_by_sql = ""
    if query.group_by:
        selected.append(_quote_identifier(query.group_by))
        group_by_sql = " GROUP BY " + _quote_identifier(query.group_by)
    for metric in query.metrics:
        if metric.operation == "count" and not metric.field:
            expression = "COUNT(*)"
        else:
            expression = (
                metric.operation.upper()
                + "("
                + _quote_identifier(str(metric.field))
                + ")"
            )
        selected.append(expression + " AS " + _quote_identifier(metric.label))
    if query.group_by and not query.metrics:
        selected.append('COUNT(*) AS "count"')
    select_sql = ", ".join(selected) if selected else "*"

    where_parts: List[str] = []
    parameters: List[Any] = [version.object_key]
    operators = {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    for item in query.filters:
        field = _quote_identifier(item.field)
        if item.operator == "contains":
            where_parts.append(f"LOWER(CAST({field} AS VARCHAR)) LIKE ? ESCAPE '\\\\'")
            escaped = (
                str(item.value)
                .casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            parameters.append("%" + escaped + "%")
        else:
            where_parts.append(f"{field} {operators[item.operator]} ?")
            parameters.append(item.value)
    where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    order_sql = ""
    if query.order_by:
        order_sql = (
            " ORDER BY "
            + _quote_identifier(query.order_by)
            + " "
            + query.order_direction.upper()
        )
    sql = (
        f"SELECT {select_sql} FROM read_json_auto(?, format='array')"
        f"{where_sql}{group_by_sql}{order_sql} LIMIT {query.limit + 1}"
    )

    started = time.perf_counter()
    connection = duckdb.connect(database=":memory:", config={"threads": 1})
    try:
        cursor = connection.execute(sql, parameters)
        columns = [item[0] for item in cursor.description]
        raw_rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise AnalyticsValidationError("Dataset query could not be executed") from exc
    finally:
        connection.close()
    elapsed_ms = (time.perf_counter() - started) * 1000
    truncated = len(raw_rows) > query.limit
    raw_rows = raw_rows[: query.limit]
    rows = [
        {
            column: (
                value.isoformat()
                if isinstance(value, (date, datetime))
                else value
            )
            for column, value in zip(columns, row, strict=True)
        }
        for row in raw_rows
    ]
    return DatasetQueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=round(elapsed_ms, 3),
    )
