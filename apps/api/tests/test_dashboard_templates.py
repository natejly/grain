"""Parameterised dashboard templates, and the refusal that makes them worth having.

A template is a dashboard definition that declares the dataset shape it needs.
The point of every test here is *when* things fail. A template that reads a
column it never declared fails when it is written. A dataset that does not
satisfy a template fails when it is bound. Neither fails on the morning somebody
opens the chart, which is the only failure that costs anything.

The last test pins the other half of the promise: a bind refusal and a workflow
compile refusal are the same shape, on purpose.
"""
from __future__ import annotations

import uuid

from test_dashboards import key, make_dataset, unique

# The template's own vocabulary. No dataset in this file has columns by these
# names — that is what the bindings are for.
REQUIRED = [
    {"name": "region", "type": "string", "description": "Category axis"},
    {"name": "amount", "type": "number", "description": "Value summed"},
]
SPEC = {
    "visualization": "bar",
    "query": {
        "group_by": "region",
        "metrics": [{"field": "amount", "operation": "sum", "label": "total"}],
        "order_by": "total",
        "order_direction": "desc",
        "limit": 10,
    },
    "x_field": "region",
    "y_fields": ["total"],
}


def make_template(client, *, required=None, spec=None, name=None) -> dict:
    response = client.post(
        "/api/dashboard-templates",
        headers=key(),
        json={
            "name": name or unique("Totals by category"),
            "description": "Sum a value by a category",
            "required_columns": required if required is not None else REQUIRED,
            "spec": spec if spec is not None else SPEC,
        },
    )
    return response.status_code, response.json()


def codes(detail: dict) -> list[str]:
    return [item["code"] for item in detail["errors"]]


def message_for(detail: dict, code: str) -> str:
    return next(item["message"] for item in detail["errors"] if item["code"] == code)


# --------------------------------------------------------------------------
# Writing a definition


def test_a_template_that_reads_an_undeclared_column_is_refused_when_it_is_written(
    client,
):
    status, detail = make_template(
        client,
        required=[{"name": "region", "type": "string"}],
        spec={
            "visualization": "bar",
            "query": {
                "group_by": "region",
                # Never declared, so no binding could ever guarantee it exists.
                "metrics": [{"field": "amount", "operation": "sum", "label": "total"}],
            },
            "x_field": "region",
            "y_fields": ["total"],
        },
    )
    assert status == 422
    assert codes(detail["detail"]) == ["spec_column_undeclared"]
    assert "“amount”" in message_for(detail["detail"], "spec_column_undeclared")


def test_a_template_collects_every_inconsistency_rather_than_the_first(client):
    status, detail = make_template(
        client,
        required=[
            {"name": "region", "type": "string"},
            {"name": "region", "type": "string"},
            {"name": "label", "type": "string"},
        ],
        spec={
            "visualization": "bar",
            "query": {
                "group_by": "region",
                # sum over a column the template itself declares as text.
                "metrics": [{"field": "label", "operation": "sum", "label": "total"}],
            },
            "x_field": "region",
            # Nothing in this query returns "headcount".
            "y_fields": ["headcount"],
        },
    )
    assert status == 422
    # Three separate mistakes, three findings, one round trip.
    assert set(codes(detail["detail"])) == {
        "required_column_duplicate",
        "spec_metric_not_numeric",
        "spec_field_unknown",
    }


def test_a_template_may_not_sum_a_column_it_declared_as_text(client):
    status, detail = make_template(
        client,
        required=[
            {"name": "region", "type": "string"},
            {"name": "amount", "type": "string"},
        ],
    )
    assert status == 422
    message = message_for(detail["detail"], "spec_metric_not_numeric")
    assert "declared string" in message
    assert "sum and avg need a numeric column" in message


# --------------------------------------------------------------------------
# Binding it to real data


def test_binding_a_template_produces_a_dashboard_that_runs(client):
    dataset = make_dataset(client)
    status, template = make_template(client)
    assert status == 201, template
    name = unique("Q1 revenue")

    response = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": name,
            "description": "",
            "dataset_id": dataset["id"],
            "column_bindings": {"region": "territory", "amount": "revenue"},
        },
    )
    assert response.status_code == 201, response.text
    dashboard = response.json()
    assert dashboard["template_id"] == template["id"]
    # The trace is complete: implicit pairs are written down beside explicit ones.
    assert dashboard["bindings"] == {"region": "territory", "amount": "revenue"}
    # The saved spec speaks the dataset's language, not the template's.
    assert dashboard["spec"]["query"]["group_by"] == "territory"
    assert dashboard["spec"]["query"]["metrics"][0]["field"] == "revenue"
    assert dashboard["spec"]["x_field"] == "territory"
    # A metric label is the author's word for the answer and is never rebound.
    assert dashboard["spec"]["y_fields"] == ["total"]

    run = client.post(f"/api/dashboards/{dashboard['id']}/run")
    assert run.status_code == 200, run.text
    assert run.json()["result"]["rows"] == [
        {"territory": "North", "total": 40},
        {"territory": "South", "total": 20},
    ]

    client.delete(f"/api/dashboards/{dashboard['id']}")
    client.delete(f"/api/dashboard-templates/{template['id']}")


def test_one_definition_binds_to_two_different_datasets(client):
    """The reason templates exist rather than starter layouts."""
    deals = make_dataset(client)
    headcount = make_dataset(
        client, "office,people\nBerlin,4\nLisbon,7\nBerlin,2\n"
    )
    status, template = make_template(client)
    assert status == 201, template

    first = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Revenue"),
            "dataset_id": deals["id"],
            "column_bindings": {"region": "territory", "amount": "revenue"},
        },
    )
    second = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Headcount"),
            "dataset_id": headcount["id"],
            "column_bindings": {"region": "office", "amount": "people"},
        },
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    rows = client.post(f"/api/dashboards/{second.json()['id']}/run").json()["result"]
    # The template's own "order by total, descending" survived the rebind.
    assert rows["rows"] == [
        {"office": "Lisbon", "total": 7},
        {"office": "Berlin", "total": 6},
    ]

    client.delete(f"/api/dashboards/{first.json()['id']}")
    client.delete(f"/api/dashboards/{second.json()['id']}")
    client.delete(f"/api/dashboard-templates/{template['id']}")


def test_a_binding_that_does_not_satisfy_the_shape_is_refused_at_bind_time(client):
    dataset = make_dataset(client)
    status, template = make_template(client)
    assert status == 201, template

    # No bindings at all: this dataset happens to call its columns something
    # else entirely, and nothing here should guess.
    unbound = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={"name": unique("Guesswork"), "dataset_id": dataset["id"]},
    )
    assert unbound.status_code == 422, unbound.text
    detail = unbound.json()["detail"]
    assert codes(detail) == ["template_column_missing", "template_column_missing"]
    # Legible: what was wanted, what type, and what this dataset actually has.
    messages = {item["node"]: item["message"] for item in detail["errors"]}
    assert "requires a string column “region”" in messages["region"]
    assert "requires a number column “amount”" in messages["amount"]
    assert "closed_on, revenue, territory" in messages["region"]

    # Bound to the wrong types, both ways round.
    swapped = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Swapped"),
            "dataset_id": dataset["id"],
            "column_bindings": {"region": "revenue", "amount": "territory"},
        },
    )
    assert swapped.status_code == 422
    detail = swapped.json()["detail"]
    assert codes(detail) == ["template_column_type", "template_column_type"]
    assert (
        "requires “amount” to be number, but “territory” is string"
        in message_for(detail, "template_column_type")
    )

    nothing_was_created = [
        item["template_id"] for item in client.get("/api/dashboards").json()
    ]
    assert template["id"] not in nothing_was_created
    client.delete(f"/api/dashboard-templates/{template['id']}")


def test_a_misspelled_binding_is_told_what_it_probably_meant(client):
    dataset = make_dataset(client)
    status, template = make_template(client)
    assert status == 201, template

    response = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Typos"),
            "dataset_id": dataset["id"],
            # One typo in the dataset's column, one in the template's parameter.
            "column_bindings": {"region": "teritory", "amonut": "revenue"},
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert set(codes(detail)) == {
        "binding_unknown_parameter",
        "binding_column_unknown",
        "template_column_missing",
    }
    assert "did you mean territory?" in message_for(detail, "binding_column_unknown")
    assert "did you mean amount?" in message_for(detail, "binding_unknown_parameter")
    # The column reported by name is not *also* reported by the schema, which
    # would say the same thing twice and worse the second time.
    by_code: dict[str, list[str]] = {}
    for item in detail["errors"]:
        by_code.setdefault(item["code"], []).append(item["node"])
    assert by_code["binding_column_unknown"] == ["region"]
    assert by_code["template_column_missing"] == ["amount"]

    client.delete(f"/api/dashboard-templates/{template['id']}")


def test_types_widen_only_where_widening_cannot_change_an_answer(client):
    """An integer is a number; a date is a label; an amount is not a label."""
    dataset = make_dataset(client)
    status, template = make_template(
        client,
        required=[
            {"name": "label", "type": "string"},
            {"name": "value", "type": "number"},
        ],
        spec={
            "visualization": "bar",
            "query": {
                "group_by": "label",
                "metrics": [{"field": "value", "operation": "sum", "label": "total"}],
            },
            "x_field": "label",
            "y_fields": ["total"],
        },
    )
    assert status == 201, template

    # revenue is integer and satisfies number; closed_on is a date and is a
    # perfectly good category axis.
    widened = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("By date"),
            "dataset_id": dataset["id"],
            "column_bindings": {"label": "closed_on", "value": "revenue"},
        },
    )
    assert widened.status_code == 201, widened.text

    # But a number is not a label. Accepting it would draw one bar per price.
    narrowed = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Nonsense"),
            "dataset_id": dataset["id"],
            "column_bindings": {"label": "revenue", "value": "revenue"},
        },
    )
    assert narrowed.status_code == 422
    assert codes(narrowed.json()["detail"]) == ["template_column_type"]

    client.delete(f"/api/dashboards/{widened.json()['id']}")
    client.delete(f"/api/dashboard-templates/{template['id']}")


def test_deleting_a_definition_leaves_the_dashboards_it_produced_running(client):
    dataset = make_dataset(client)
    status, template = make_template(client)
    assert status == 201, template
    bound = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={
            "name": unique("Survivor"),
            "dataset_id": dataset["id"],
            "column_bindings": {"region": "territory", "amount": "revenue"},
        },
    ).json()

    assert client.delete(f"/api/dashboard-templates/{template['id']}").status_code == 204
    assert client.get("/api/dashboard-templates").status_code == 200

    run = client.post(f"/api/dashboards/{bound['id']}/run")
    assert run.status_code == 200, run.text
    # The link is cleared rather than left pointing at a row that is gone.
    assert run.json()["dashboard"]["template_id"] == ""

    client.delete(f"/api/dashboards/{bound['id']}")


# --------------------------------------------------------------------------
# The shared shape


def test_a_bind_refusal_reads_like_a_workflow_that_will_not_compile(client):
    """One vocabulary for "the thing you supplied does not satisfy the contract".

    A dashboard bound to the wrong dataset and a workflow given the wrong
    arguments are the same mistake. They are reported by the same types, in the
    same body, so one renderer draws both and one repair prompt fixes either.
    """
    dataset = make_dataset(client)
    status, template = make_template(client)
    assert status == 201, template

    refusal = client.post(
        f"/api/dashboard-templates/{template['id']}/bind",
        headers=key(),
        json={"name": unique("Mismatch"), "dataset_id": dataset["id"]},
    )
    workflow = client.post(
        "/api/workflows",
        headers={"Idempotency-Key": "wf-" + uuid.uuid4().hex},
        json={
            "name": "Broken " + uuid.uuid4().hex[:6],
            "graph": {
                "name": "Broken",
                "nodes": [
                    {"id": "step", "kind": "tool", "tool": "no_such_tool_at_all"}
                ],
                "edges": [],
            },
        },
    )
    assert refusal.status_code == workflow.status_code == 422, workflow.text

    bind_detail = refusal.json()["detail"]
    workflow_detail = workflow.json()["detail"]
    assert bind_detail.keys() == workflow_detail.keys() == {"errors", "warnings"}
    assert bind_detail["errors"][0].keys() == workflow_detail["errors"][0].keys()
    assert {"code", "message", "node"} == set(bind_detail["errors"][0])

    client.delete(f"/api/dashboard-templates/{template['id']}")
