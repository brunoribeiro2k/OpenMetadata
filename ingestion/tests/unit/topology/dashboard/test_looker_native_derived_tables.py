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
Test lineage for Looker native derived tables (explore_source)
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import lkml
import pytest
from tests.unit.topology.dashboard import test_looker_extends_lineage

from metadata.generated.schema.entity.data.dashboardDataModel import (
    DashboardDataModel,
    DataModelType,
)
from metadata.generated.schema.entity.data.table import Column, DataType
from metadata.generated.schema.metadataIngestion.workflow import (
    OpenMetadataWorkflowConfig,
)
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.source.dashboard.looker.metadata import LookerSource
from metadata.ingestion.source.dashboard.looker.models import LkmlFile, LookMlView

NDT_LKML = """
view: usage_ndt {
  derived_table: {
    explore_source: usage {
      column: partner_id { field: partners.id }
      column: check_ins {}
      filters: [usage.reference_date: "after 100 days ago"]
    }
  }
  dimension: partner_id { sql: ${TABLE}.partner_id ;; }
  measure: total_check_ins { type: sum sql: ${TABLE}.check_ins ;; }
}
"""

IMPLICIT_DIMENSION_LKML = """
view: usage_ndt {
  derived_table: {
    explore_source: usage {
      column: partner_id { field: partners.id }
    }
  }
  dimension: partner_id {}
  dimension: partner_label { sql: CONCAT('partner ', ${partner_id}) ;; }
}
"""


def load_view(lkml_text: str) -> LookMlView:
    return LkmlFile.model_validate(lkml.load(lkml_text)).views[0]


def data_model(name: str, display_name: str, columns: list) -> DashboardDataModel:
    return DashboardDataModel(
        id=uuid.uuid4(),
        name=name,
        displayName=display_name,
        fullyQualifiedName=f"looker.model.{name}",
        service=EntityReference(id=uuid.uuid4(), type="dashboardService"),
        dataModelType=DataModelType.LookMlView,
        columns=[
            Column(name=column, dataType=DataType.STRING, fullyQualifiedName=f"looker.model.{name}.{column}")
            for column in columns
        ],
    )


def column_pairs(lineage) -> list[tuple[str, str]]:
    return sorted(
        (column.fromColumns[0].root, column.toColumn.root) for column in lineage.edge.lineageDetails.columnsLineage
    )


@pytest.fixture
def explore_model() -> DashboardDataModel:
    return data_model("sales_usage", "usage", ["partners.id", "usage.check_ins", "usage.other"])


@pytest.fixture
def view_model() -> DashboardDataModel:
    return data_model("sales_usage_ndt_view", "usage_ndt", ["partner_id", "partner_label", "total_check_ins"])


@pytest.fixture
def data_models(view_model) -> dict[str, DashboardDataModel]:
    """The data models OpenMetadata holds, by name"""
    return {"sales_usage_ndt_view": view_model}


@pytest.fixture
def looker(explore_model, data_models) -> LookerSource:
    mock_config = test_looker_extends_lineage.MOCK_LOOKER_CONFIG
    config = OpenMetadataWorkflowConfig.model_validate(mock_config)
    with patch(
        "metadata.ingestion.source.dashboard.dashboard_service.DashboardServiceSource.test_connection",
        return_value=False,
    ):
        source = LookerSource.create(mock_config["source"], config.workflowConfig.openMetadataServerConfig)
    source._explores_cache["sales_usage"] = explore_model
    source._build_data_model = data_models.get
    return source


@pytest.fixture
def view() -> LookMlView:
    return load_view(NDT_LKML)


class TestNativeDerivedTables:
    """
    A native derived table gets lineage from the Explore it's built from
    """

    def test_explore_source_is_parsed(self, view):
        explore_source = view.derived_table.explore_source
        assert explore_source.name == "usage"
        assert [(column.name, column.field) for column in explore_source.columns] == [
            ("partner_id", "partners.id"),
            ("check_ins", None),
        ]
        assert view.derived_table.sql is None

    def test_lineage_waits_for_every_explore(self, looker, view, view_model, explore_model):
        looker._view_data_model = view_model
        explore = SimpleNamespace(model_name="sales", name="usage")

        results = list(looker.add_view_lineage(view, explore))

        # Only the view -> explore edge of the explore using the view, no source yet
        assert [result.right.edge.toEntity.id.root for result in results] == [explore_model.id.root]
        assert "sales_usage_ndt_view" in looker._native_derived_tables

    def test_lineage_from_the_explore(self, looker, view, view_model, explore_model):
        looker._native_derived_tables["sales_usage_ndt_view"] = (view, "sales")

        results = list(looker.yield_native_derived_table_lineage())

        assert len(results) == 1
        lineage = results[0].right
        assert lineage.edge.fromEntity.id.root == explore_model.id.root
        assert lineage.edge.toEntity.id.root == view_model.id.root
        assert column_pairs(lineage) == [
            ("looker.model.sales_usage.partners.id", "looker.model.sales_usage_ndt_view.partner_id"),
            ("looker.model.sales_usage.usage.check_ins", "looker.model.sales_usage_ndt_view.total_check_ins"),
        ]
        assert looker._native_derived_tables == {}

    def test_implicit_dimensions_get_column_lineage(self, looker):
        looker._native_derived_tables["sales_usage_ndt_view"] = (load_view(IMPLICIT_DIMENSION_LKML), "sales")

        results = list(looker.yield_native_derived_table_lineage())

        assert column_pairs(results[0].right) == [
            ("looker.model.sales_usage.partners.id", "looker.model.sales_usage_ndt_view.partner_id"),
            ("looker.model.sales_usage.partners.id", "looker.model.sales_usage_ndt_view.partner_label"),
        ]

    def test_explore_in_another_model(self, looker, view, explore_model):
        looker._native_derived_tables["sales_usage_ndt_view"] = (view, "finance")

        results = list(looker.yield_native_derived_table_lineage())

        assert results[0].right.edge.fromEntity.id.root == explore_model.id.root

    def test_explore_of_the_model_comes_first(self, looker, view, data_models):
        finance_explore = data_model("finance_usage", "usage", ["partners.id"])
        data_models["finance_usage"] = finance_explore
        looker._native_derived_tables["sales_usage_ndt_view"] = (view, "finance")

        results = list(looker.yield_native_derived_table_lineage())

        assert results[0].right.edge.fromEntity.id.root == finance_explore.id.root

    def test_explore_in_several_other_models_gives_no_lineage(self, looker, view):
        looker._explores_cache["marketing_usage"] = data_model("marketing_usage", "usage", ["partners.id"])
        looker._native_derived_tables["sales_usage_ndt_view"] = (view, "finance")

        assert list(looker.yield_native_derived_table_lineage()) == []

    def test_missing_explore_gives_no_lineage(self, looker, view):
        looker._explores_cache.clear()
        looker._native_derived_tables["sales_usage_ndt_view"] = (view, "sales")

        assert list(looker.yield_native_derived_table_lineage()) == []
