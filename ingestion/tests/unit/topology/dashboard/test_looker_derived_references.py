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
Test Looker views whose derived tables reference other views
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.unit.topology.dashboard import test_looker_extends_lineage

from metadata.generated.schema.metadataIngestion.workflow import (
    OpenMetadataWorkflowConfig,
)
from metadata.ingestion.lineage.models import Dialect
from metadata.ingestion.source.dashboard.looker.metadata import LookerSource
from metadata.ingestion.source.dashboard.looker.models import (
    LookMlDerivedTableField,
    LookMlView,
)


def table_view(name: str, table: str) -> LookMlView:
    return LookMlView(name=name, sql_table_name=table)


def derived_view(name: str, sql: str) -> LookMlView:
    return LookMlView(name=name, derived_table=LookMlDerivedTableField(sql=sql))


@pytest.fixture
def looker() -> LookerSource:
    mock_config = test_looker_extends_lineage.MOCK_LOOKER_CONFIG
    config = OpenMetadataWorkflowConfig.model_validate(mock_config)
    with patch(
        "metadata.ingestion.source.dashboard.dashboard_service.DashboardServiceSource.test_connection",
        return_value=False,
    ):
        source = LookerSource.create(mock_config["source"], config.workflowConfig.openMetadataServerConfig)
    source.get_db_service_prefixes = lambda: ["Trino"]
    source._get_db_dialect = lambda _: Dialect.TRINO
    source._build_data_model = lambda name: SimpleNamespace(name=name)
    # (source table, data model) instead of an AddLineageRequest
    source.build_lineage_requests = lambda source, db_service_prefix, to_entity, column_lineage=None: iter(
        [(str(source), to_entity.name)]
    )
    return source


def process(looker: LookerSource, view: LookMlView, model: str = "model") -> list:
    datamodel_name = f"{model}_{view.name}_view"
    looker._view_data_model = SimpleNamespace(name=datamodel_name)
    return list(looker._yield_view_source_lineage(view, datamodel_name))


class TestDerivedReferences:
    """
    A view whose references aren't parsed yet gets its lineage once they are
    """

    def test_reference_parsed_first(self, looker):
        process(looker, table_view("base", "db.schema.events"))
        lineage = process(looker, derived_view("daily", "SELECT a FROM ${base.SQL_TABLE_NAME}"))
        assert lineage == [("db.schema.events", "model_daily_view")]

    def test_view_waits_for_its_reference(self, looker):
        assert process(looker, derived_view("daily", "SELECT a FROM ${base.SQL_TABLE_NAME}")) == []
        assert "daily" in looker._unparsed_views

        lineage = process(looker, table_view("base", "db.schema.events"))

        assert ("db.schema.events", "model_base_view") in lineage
        # The retry writes to the waiting view's own data model
        assert ("db.schema.events", "model_daily_view") in lineage
        assert looker._unparsed_views == {}
        assert looker._view_data_model.name == "model_base_view"

    def test_chain_of_waiting_views(self, looker):
        process(looker, derived_view("monthly", "SELECT a FROM ${daily.SQL_TABLE_NAME}"))
        process(looker, derived_view("daily", "SELECT a FROM ${base.SQL_TABLE_NAME}"))

        lineage = process(looker, table_view("base", "db.schema.events"))

        assert sorted(lineage) == [
            ("db.schema.events", "model_base_view"),
            ("db.schema.events", "model_daily_view"),
            ("db.schema.events", "model_monthly_view"),
        ]

    def test_each_explore_data_model_gets_lineage(self, looker):
        view = derived_view("daily", "SELECT a FROM ${base.SQL_TABLE_NAME}")
        process(looker, view, model="sales")
        process(looker, view, model="finance")

        lineage = process(looker, table_view("base", "db.schema.events"))

        assert ("db.schema.events", "sales_daily_view") in lineage
        assert ("db.schema.events", "finance_daily_view") in lineage

    def test_a_failing_data_model_spares_the_others(self, looker):
        def build_data_model(name: str) -> SimpleNamespace:
            if name == "sales_daily_view":
                raise ValueError("lookup failed")
            return SimpleNamespace(name=name)

        looker._build_data_model = build_data_model
        view = derived_view("daily", "SELECT a FROM ${base.SQL_TABLE_NAME}")
        process(looker, view, model="sales")
        process(looker, view, model="finance")

        results = process(looker, table_view("base", "db.schema.events"))

        assert ("db.schema.events", "finance_daily_view") in results
        errors = [result.left for result in results if getattr(result, "left", None)]
        assert [error.name for error in errors] == ["daily"]

    def test_view_never_parsed_stays_waiting(self, looker):
        process(looker, derived_view("daily", "SELECT a FROM ${missing.SQL_TABLE_NAME}"))

        assert list(looker.build_lineage_for_unparsed_views()) == []
        assert "daily" in looker._unparsed_views
