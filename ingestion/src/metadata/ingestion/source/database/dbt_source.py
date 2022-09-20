#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
DBT source methods.
"""
import traceback
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.api.tests.createTestCase import CreateTestCaseRequest
from metadata.generated.schema.api.tests.createTestDefinition import (
    CreateTestDefinitionRequest,
)
from metadata.generated.schema.api.tests.createTestSuite import CreateTestSuiteRequest
from metadata.generated.schema.entity.data.table import (
    Column,
    DataModel,
    ModelType,
    Table,
)
from metadata.generated.schema.entity.teams.team import Team
from metadata.generated.schema.entity.teams.user import User
from metadata.generated.schema.tests.basic import (
    TestCaseResult,
    TestCaseStatus,
    TestResultValue,
)
from metadata.generated.schema.tests.testCase import TestCase
from metadata.generated.schema.tests.testDefinition import (
    EntityType,
    TestDefinition,
    TestPlatform,
)
from metadata.generated.schema.tests.testSuite import TestSuite
from metadata.generated.schema.type.entityLineage import EntitiesEdge
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.database.column_type_parser import ColumnTypeParser
from metadata.utils import fqn
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()


class DBTMixin:

    metadata: OpenMetadata

    def get_data_model(self, table_fqn: str) -> Optional[DataModel]:
        return self.data_models.get(table_fqn)

    def _parse_data_model(self):
        """
        Get all the DBT information and feed it to the Table Entity
        """
        if (
            self.source_config.dbtConfigSource
            and self.dbt_manifest
            and self.dbt_catalog
        ):
            logger.info("Parsing Data Models")
            self.manifest_entities = {
                **self.dbt_manifest["nodes"],
                **self.dbt_manifest["sources"],
            }
            self.catalog_entities = {
                **self.dbt_catalog["nodes"],
                **self.dbt_catalog["sources"],
            }
            for key, mnode in self.manifest_entities.items():
                try:
                    name = mnode["alias"] if "alias" in mnode.keys() else mnode["name"]
                    cnode = self.catalog_entities.get(key)
                    columns = (
                        self._parse_data_model_columns(name, mnode, cnode)
                        if cnode
                        else []
                    )

                    if mnode["resource_type"] == "test":
                        self.dbt_tests[key] = mnode
                        continue
                    upstream_nodes = self._parse_data_model_upstream(mnode)
                    model_name = (
                        mnode["alias"] if "alias" in mnode.keys() else mnode["name"]
                    )
                    database = mnode["database"] if mnode["database"] else "default"
                    schema = mnode["schema"] if mnode["schema"] else "default"
                    raw_sql = mnode.get("raw_sql", "")
                    description = mnode.get("description")
                    dbt_owner = cnode["metadata"].get("owner")
                    owner = None
                    if dbt_owner:
                        owner_name = f"*{dbt_owner}*"
                        user_owner_fqn = fqn.build(
                            self.metadata, entity_type=User, user_name=owner_name
                        )
                        if user_owner_fqn:
                            owner = self.metadata.get_entity_reference(
                                entity=User, fqn=user_owner_fqn
                            )
                        else:
                            team_owner_fqn = fqn.build(
                                self.metadata, entity_type=Team, team_name=owner_name
                            )
                            if team_owner_fqn:
                                owner = self.metadata.get_entity_reference(
                                    entity=Team, fqn=team_owner_fqn
                                )
                            else:
                                logger.warning(
                                    f"Unable to ingest owner from DBT since no user or team was found with name {dbt_owner}"
                                )

                    model = DataModel(
                        modelType=ModelType.DBT,
                        description=description if description else None,
                        path=f"{mnode['root_path']}/{mnode['original_file_path']}",
                        rawSql=raw_sql,
                        sql=mnode.get("compiled_sql", raw_sql),
                        columns=columns,
                        upstream=upstream_nodes,
                        owner=owner,
                    )
                    model_fqn = fqn.build(
                        self.metadata,
                        entity_type=DataModel,
                        service_name=self.config.serviceName,
                        database_name=database,
                        schema_name=schema,
                        model_name=model_name,
                    )
                    self.data_models[model_fqn] = model
                except Exception as exc:
                    logger.debug(traceback.format_exc())
                    logger.warning(f"Unexpected exception parsing data model: {exc}")

    def _parse_data_model_upstream(self, mnode):
        upstream_nodes = []
        if "depends_on" in mnode and "nodes" in mnode["depends_on"]:
            for node in mnode["depends_on"]["nodes"]:
                try:
                    parent_node = self.manifest_entities[node]
                    parent_fqn = fqn.build(
                        self.metadata,
                        entity_type=Table,
                        service_name=self.config.serviceName,
                        database_name=parent_node["database"]
                        if parent_node["database"]
                        else "default",
                        schema_name=parent_node["schema"]
                        if parent_node["schema"]
                        else "default",
                        table_name=parent_node["name"],
                    )
                    if parent_fqn:
                        upstream_nodes.append(parent_fqn)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.debug(traceback.format_exc())
                    logger.warning(
                        f"Failed to parse the node {node} to capture lineage: {exc}"
                    )
                    continue
        return upstream_nodes

    def _parse_data_model_columns(
        self, model_name: str, mnode: Dict, cnode: Dict
    ) -> List[Column]:
        columns = []
        ccolumns = cnode.get("columns")
        manifest_columns = mnode.get("columns", {})
        for key in ccolumns:
            ccolumn = ccolumns[key]
            col_name = ccolumn["name"].lower()
            try:
                ctype = ccolumn["type"]
                col_type = ColumnTypeParser.get_column_type(ctype)
                description = manifest_columns.get(key.lower(), {}).get("description")
                if description is None:
                    description = ccolumn.get("comment")
                col = Column(
                    name=col_name,
                    description=description if description else None,
                    dataType=col_type,
                    dataLength=1,
                    ordinalPosition=ccolumn["index"],
                )
                columns.append(col)
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(traceback.format_exc())
                logger.warning(f"Failed to parse column {col_name}: {exc}")

        return columns

    def create_dbt_lineage(self) -> Iterable[AddLineageRequest]:
        """
        After everything has been processed, add the lineage info
        """
        logger.info("Processing DBT lineage")
        for data_model_name, data_model in self.data_models.items():
            for upstream_node in data_model.upstream:
                try:
                    from_entity: Table = self.metadata.get_by_name(
                        entity=Table, fqn=upstream_node
                    )
                    to_entity: Table = self.metadata.get_by_name(
                        entity=Table, fqn=data_model_name
                    )
                    if from_entity and to_entity:
                        yield AddLineageRequest(
                            edge=EntitiesEdge(
                                fromEntity=EntityReference(
                                    id=from_entity.id.__root__,
                                    type="table",
                                ),
                                toEntity=EntityReference(
                                    id=to_entity.id.__root__,
                                    type="table",
                                ),
                            )
                        )

                except Exception as exc:  # pylint: disable=broad-except
                    logger.debug(traceback.format_exc())
                    logger.warning(
                        f"Failed to parse the node {upstream_node} to capture lineage: {exc}"
                    )

    def create_dbt_tests_suite_definition(self):
        """
        After everything has been processed, add the tests suite and test definitions
        """
        try:
            if (
                self.source_config.dbtConfigSource
                and self.dbt_manifest
                and self.dbt_catalog
            ):
                logger.info("Processing DBT Tests Suites and Test Definitions")
                for key, dbt_test in self.dbt_tests.items():
                    test_suite_name = dbt_test["meta"].get(
                        "test_suite_name", "DBT_TEST_SUITE"
                    )
                    test_suite_desciption = dbt_test["meta"].get(
                        "test_suite_desciption", ""
                    )
                    check_test_suite_exists = self.metadata.get_by_name(
                        fqn=test_suite_name, entity=TestSuite
                    )
                    if not check_test_suite_exists:
                        test_suite = CreateTestSuiteRequest(
                            name=test_suite_name,
                            description=test_suite_desciption,
                        )
                        yield test_suite
                    check_test_definition_exists = self.metadata.get_by_name(
                        fqn=dbt_test["name"],
                        entity=TestDefinition,
                    )
                    if not check_test_definition_exists:
                        column_name = dbt_test.get("column_name")
                        if column_name:
                            entity_type = EntityType.COLUMN
                        else:
                            entity_type = EntityType.TABLE
                        test_definition = CreateTestDefinitionRequest(
                            name=dbt_test["name"],
                            description=dbt_test["description"],
                            entityType=entity_type,
                            testPlatforms=[TestPlatform.DBT],
                            parameterDefinition=self.create_test_case_parameter_definitions(
                                dbt_test
                            ),
                        )
                        yield test_definition
        except Exception as err:  # pylint: disable=broad-except
            logger.error(f"Failed to parse the node to capture tests {err}")

    def create_dbt_test_cases(self):
        """
        After test suite and test definitions have been processed, add the tests cases info
        """
        if (
            self.source_config.dbtConfigSource
            and self.dbt_manifest
            and self.dbt_catalog
        ):
            logger.info("Processing DBT Tests Cases")
            for key, dbt_test in self.dbt_tests.items():
                try:
                    entity_link_list = self.generate_entity_link(dbt_test)
                    for entity_link in entity_link_list:
                        test_suite_name = dbt_test["meta"].get(
                            "test_suite_name", "DBT_TEST_SUITE"
                        )
                        test_case = CreateTestCaseRequest(
                            name=dbt_test["name"],
                            description=dbt_test["description"],
                            testDefinition=EntityReference(
                                id=self.metadata.get_by_name(
                                    fqn=dbt_test["name"],
                                    entity=TestDefinition,
                                ).id.__root__,
                                type="testDefinition",
                            ),
                            entityLink=entity_link,
                            testSuite=EntityReference(
                                id=self.metadata.get_by_name(
                                    fqn=test_suite_name, entity=TestSuite
                                ).id.__root__,
                                type="testSuite",
                            ),
                            parameterValues=self.create_test_case_parameter_values(
                                dbt_test
                            ),
                        )
                        yield test_case
                except Exception as err:  # pylint: disable=broad-except
                    logger.error(
                        f"Failed to parse the node {key} to capture tests {err}"
                    )
            self.update_dbt_test_result()

    def update_dbt_test_result(self):
        """
        After test cases has been processed, add the tests results info
        """
        if self.dbt_run_results:
            logger.info("Processing DBT Tests Results")
            for dbt_test_result in self.dbt_run_results.get("results"):
                try:
                    # Process the Test Status
                    test_case_status = TestCaseStatus.Aborted
                    test_result_value = -1
                    if dbt_test_result.get("status") == "success":
                        test_case_status = TestCaseStatus.Success
                        test_result_value = 1
                    elif dbt_test_result.get("status") == "failure":
                        test_case_status = TestCaseStatus.Failed
                        test_result_value = 0

                    # Process the Test Timings
                    dbt_test_timings = dbt_test_result["timing"]
                    dbt_test_completed_at = None
                    for dbt_test_timing in dbt_test_timings:
                        if dbt_test_timing.get("name", "") == "execute":
                            dbt_test_completed_at = dbt_test_timing.get("completed_at")
                    dbt_timestamp = None
                    if dbt_test_completed_at:
                        dbt_timestamp = datetime.strptime(
                            dbt_test_completed_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        dbt_timestamp = self.unix_time_millis(dbt_timestamp)

                    test_case_result = TestCaseResult(
                        timestamp=dbt_timestamp,
                        testCaseStatus=test_case_status,
                        testResultValue=[
                            TestResultValue(
                                name=dbt_test_result.get("unique_id"),
                                value=str(test_result_value),
                            )
                        ],
                    )

                    dbt_test_node = self.dbt_tests.get(dbt_test_result["unique_id"])
                    if dbt_test_node:
                        nodes = dbt_test_node["depends_on"]["nodes"]
                        for node in nodes:
                            model = self.manifest_entities.get(node)
                            test_case_fqn = fqn.build(
                                self.metadata,
                                entity_type=TestCase,
                                service_name=self.config.serviceName,
                                database_name=model.get("database"),
                                schema_name=model.get("schema"),
                                table_name=model.get("name"),
                                column_name=dbt_test_node.get("column_name"),
                                test_case_name=self.dbt_tests.get(
                                    dbt_test_result["unique_id"]
                                )["name"],
                            )
                            self.metadata.add_test_case_results(
                                test_results=test_case_result,
                                test_case_name=test_case_fqn,
                            )
                except Exception as err:  # pylint: disable=broad-except
                    logger.error(f"Failed capture tests results {err}")

    def create_test_case_parameter_definitions(self, dbt_test):
        test_case_param_definition = [
            {
                "name": dbt_test["test_metadata"]["name"],
                "displayName": dbt_test["test_metadata"]["name"],
                "required": False,
            }
        ]
        return test_case_param_definition

    def create_test_case_parameter_values(self, dbt_test):
        values = dbt_test["test_metadata"]["kwargs"].get("values")
        dbt_test_values = ""
        if values:
            dbt_test_values = ",".join(values)
        test_case_param_values = [
            {"name": dbt_test["test_metadata"]["name"], "value": dbt_test_values}
        ]
        return test_case_param_values

    def generate_entity_link(self, dbt_test):
        nodes = dbt_test["depends_on"]["nodes"]
        entity_link_list = []
        for node in nodes:
            model = self.manifest_entities.get(node)
            table_fqn = fqn.build(
                self.metadata,
                entity_type=Table,
                service_name=self.config.serviceName,
                database_name=model.get("database"),
                schema_name=model.get("schema"),
                table_name=model.get("name"),
            )
            column_name = dbt_test.get("column_name")
            if column_name:
                entity_link = (
                    f"<#E::table::" f"{table_fqn}" f"::columns::" f"{column_name}>"
                )
            else:
                entity_link = f"<#E::table::" f"{table_fqn}>"
            entity_link_list.append(entity_link)
        return entity_link_list

    def unix_time(self, dt):
        epoch = datetime.utcfromtimestamp(0)
        delta = dt - epoch
        return delta.total_seconds()

    def unix_time_millis(self, dt):
        return int(self.unix_time(dt) * 1000)
