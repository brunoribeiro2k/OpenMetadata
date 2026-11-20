#  Copyright 2025 Collate
#  Licensed under the Collate Community License, Version 1.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  https://github.com/open-metadata/OpenMetadata/blob/main/ingestion/LICENSE
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
Test Liquid rendering in Looker derived table SQL
"""

from metadata.ingestion.lineage.models import Dialect
from metadata.ingestion.lineage.parser import LineageParser
from metadata.ingestion.source.dashboard.looker.metadata import (
    NEUTRAL_DATE,
    render_derived_sql,
)


def lineage(sql: str, **context) -> LineageParser:
    return LineageParser(f"create view v as {render_derived_sql(sql, **context)}", Dialect.TRINO)


def test_sql_without_liquid_is_unchanged():
    sql = "SELECT a, b FROM schema.table WHERE c = '{x}'"
    assert render_derived_sql(sql) == sql
    assert render_derived_sql(None) is None


def test_condition_always_holds_and_keeps_its_field():
    sql = "SELECT a FROM t WHERE {% condition country %} t.country {% endcondition %} AND b = 1"
    assert render_derived_sql(sql) == "SELECT a FROM t WHERE ((t.country) IS NULL OR 1=1) AND b = 1"


def test_incrementcondition_always_holds():
    sql = "SELECT a FROM t WHERE {% incrementcondition updated %} t.updated_at {% endincrementcondition %}"
    assert render_derived_sql(sql) == "SELECT a FROM t WHERE ((t.updated_at) IS NULL OR 1=1)"


def test_empty_condition_is_no_filter():
    sql = "SELECT a FROM t WHERE {% condition country %}{% endcondition %}"
    assert render_derived_sql(sql) == "SELECT a FROM t WHERE 1=1"


def test_dates_and_parameters_are_filled():
    sql = (
        "SELECT a FROM t WHERE d >= DATE({% date_start period %}) AND d < {% date_end period %}"
        " AND c = '{% parameter currency %}' AND n > -{% parameter days %}"
    )
    assert render_derived_sql(sql) == (
        f"SELECT a FROM t WHERE d >= DATE({NEUTRAL_DATE}) AND d < {NEUTRAL_DATE} AND c = '1' AND n > -1"
    )


def test_undefined_values_render_as_null():
    sql = "SELECT a FROM t WHERE c IN ({{ _user_attributes['countries'] }}) AND d = '{{ x.y }}'"
    assert render_derived_sql(sql) == "SELECT a FROM t WHERE c IN (NULL) AND d = 'NULL'"


def test_if_takes_the_branch_with_no_user_values():
    sql = (
        "SELECT a FROM {% if currency._parameter_value == 'USD' %} usd_table"
        " {% elsif openmetadata %} om_table {% else %} other_table {% endif %}"
    )
    assert render_derived_sql(sql).split() == ["SELECT", "a", "FROM", "om_table"]


def test_if_sees_the_model_explore_and_view_names():
    sql = (
        "SELECT a FROM {% if _model._name == 'finance' and _explore._name == 'orders'"
        " and _view._name == 'v' %} finance_table {% else %} other_table {% endif %}"
    )
    assert render_derived_sql(sql).split() == ["SELECT", "a", "FROM", "other_table"]
    rendered = render_derived_sql(sql, model_name="finance", explore_name="orders", view_name="v")
    assert rendered.split() == ["SELECT", "a", "FROM", "finance_table"]


def test_if_with_parentheses_and_not():
    sql = "SELECT a FROM t {% if a and (b or c) %} JOIN u ON t.id = u.id {% endif %}{% if not a %} WHERE 1=1{% endif %}"
    assert render_derived_sql(sql) == "SELECT a FROM t  WHERE 1=1"


def test_assign_and_raw():
    sql = (
        '{% assign anchor = "CAST({% date_end period %} AS DATE)" %}'
        "SELECT {{ anchor }} AS d, '{% raw %}{{ kept }}{% endraw %}' AS r FROM t"
    )
    assert render_derived_sql(sql) == f"SELECT CAST({NEUTRAL_DATE} AS DATE) AS d, '{{{{ kept }}}}' AS r FROM t"


def test_looker_tags_inside_raw_stay_as_written():
    sql = "SELECT '{% raw %}{% date_start period %} {% parameter p %}{% endraw %}' AS marker FROM t"
    assert render_derived_sql(sql) == "SELECT '{% date_start period %} {% parameter p %}' AS marker FROM t"


def test_unrenderable_template_is_returned_as_is():
    sql = "SELECT a FROM t WHERE {% if %}"
    assert render_derived_sql(sql) == sql


def test_rendered_sql_gives_column_lineage():
    parser = lineage(
        "SELECT c.country_name, SUM(o.amount) * {% parameter rate %} AS revenue FROM db.sales.orders o"
        " JOIN db.sales.countries c ON o.country_id = c.id"
        " WHERE {% condition country %} c.country_name {% endcondition %}"
        " AND o.created_at >= {% date_start period %}"
        " GROUP BY 1"
    )
    assert {str(table) for table in parser.source_tables} == {"db.sales.orders", "db.sales.countries"}
    targets = {str(column[-1].raw_name) for column in parser.column_lineage}
    assert {"country_name", "revenue"} <= targets, targets


def test_condition_keeps_the_tables_and_columns_its_field_reads():
    parser = lineage(
        "SELECT CASE WHEN {% condition region %} o.region {% endcondition %} THEN o.amount END AS amount"
        " FROM db.sales.orders o"
        " WHERE {% condition top %} o.id IN (SELECT id FROM db.sales.top_orders) {% endcondition %}"
    )
    assert {str(table) for table in parser.source_tables} == {"db.sales.orders", "db.sales.top_orders"}
    sources = {str(column[0].raw_name) for column in parser.column_lineage}
    assert {"region", "amount"} <= sources, sources
