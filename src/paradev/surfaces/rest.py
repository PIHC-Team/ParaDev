"""REST and OpenAPI contracts for the local ParaDev service."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Literal, cast

from typing_extensions import TypedDict

from paradev._api_table import (
    api_symbol_indexes,
    api_table_selection,
    append_index_entry,
)
from paradev._api_table_markdown import (
    api_indexed_reference_sections,
    api_reference_markdown,
)
from paradev._catalog import (
    CATALOG_QUERY_DEFAULT_INCLUDE_DATA,
    CATALOG_QUERY_DEFAULT_LIMIT,
    CATALOG_QUERY_DEFAULT_OFFSET,
    CATALOG_QUERY_MAX_HYDRATED_LIMIT,
    CATALOG_QUERY_MAX_LIMIT,
    normalize_catalog_query_filters,
)
from paradev.api._requests import (
    bool_value as _bool_value,
    optional_string as _optional_string,
    request_text as _request_text,
    required_string as _required_string,
    validate_request_fields as _validate_request_fields,
)
from paradev.api.projects import (
    apply_project_draft,
    create_module_batch,
    create_module_draft,
    normalize_project_draft_mutations,
    plan_project_localization_update,
    plan_project_source_form_updates,
    read_project_localization_workspace,
    read_project_source_form,
    read_project_source_text as read_project_source,
)
from paradev.desktop import (
    DesktopBuildRegistry,
    desktop_chat,
    desktop_chat_profiles,
    desktop_dependency_status,
    desktop_install_dependency,
    desktop_install_project_package,
    desktop_open_path,
    desktop_read_app_config,
    desktop_read_binary_source,
    desktop_read_config_value,
    desktop_read_text_source,
    desktop_read_thumbnail_cache,
    desktop_reset_chat_profile,
    desktop_run_hoi4,
    desktop_test_llm_route,
    desktop_write_app_config,
    desktop_write_browser_cache,
    desktop_write_chat_profile,
    desktop_write_config_value,
    desktop_write_thumbnail_cache,
    read_project_browser_cache,
)
from paradev.desktop import (
    desktop_path_status as desktop_path_status_payload,
)
from paradev.desktop.local import AI_CHAT_SOURCE_KIND_ROWS, AI_CHAT_SOURCE_KIND_VALUES
from paradev.desktop.shell import (
    desktop_hoi4_launch_readiness,
    desktop_select_project_package_path,
    desktop_select_project_path,
)
from paradev.hb import get_catalog_api_selection
from paradev.sdk import (
    FRONTEND_API_BINDING_SURFACES,
    Project,
    ProjectManifestError,
    complete_pdx_lsp_text,
    create_project,
    desktop_state,
    diagnose_pdx_lsp_text,
    document_symbols_pdx_lsp_text,
    format_pdx_file,
    format_pdx_lsp_text,
    get_architecture_api_selection,
    get_architecture_spec,
    get_frontend_api_action,
    get_frontend_api_binding_lookup,
    get_frontend_api_contract,
    get_frontend_api_selection,
    get_frontend_api_workspace,
    get_lsp_api_selection,
    get_pdx_api_selection,
    get_project_inspection_contract,
    hoi4_keyword_dataset,
    hover_pdx_lsp_text,
    normalize_frontend_api_inputs,
    open_project,
    parse_pdx_file,
    plan_frontend_api_rest_request,
    project_create_payload,
    registered_projects,
    resolve_frontend_api_options,
    semantic_tokens_pdx_lsp_text,
)
from paradev.sdk._module_diagram_api import (
    module_diagram_edit_input_schema,
    module_diagram_family_schema,
    module_diagram_intents,
)
from paradev.sdk._localization_api import (
    localization_request_input_properties,
    localization_request_required_fields,
)
from paradev.sdk.project import MAX_MODULE_CREATE_BATCH_SIZE
from paradev.surfaces._rest_contract import (
    desktop_build_interrupt_request_schema,
    nonblank_run_id_schema,
)
from paradev.surfaces.api_catalog import get_api_catalog_selection

MODULE_DRAFT_SCHEMA = "paradev.rest.module_draft.v1"
MODULE_DRAFT_PATH = "/projects/{project_id}/modules/{family_id}/drafts"
MODULE_CREATE_BATCH_PATH = "/projects/modules/create-batch"
MODULE_DUPLICATE_PATH = "/projects/modules/duplicate"
MODULE_COLLECTION_SET_PATH = "/projects/modules/collection"
MODULE_ACTIVITY_SET_PATH = "/projects/modules/activity"
MODULE_METADATA_CLEAN_PATH = "/projects/modules/metadata/clean"
MODULE_DIAGRAM_PATH = "/projects/modules/diagram"
MODULE_DIAGRAM_EDIT_PATH = "/projects/modules/diagram/edit"
DRAFT_APPLY_SCHEMA = "paradev.rest.draft_apply.v1"
DRAFT_APPLY_PATH = "/projects/{project_id}/drafts/apply"
SOURCE_TEXT_SCHEMA = "paradev.rest.source_text.v1"
SOURCE_PATH = "/projects/{project_id}/sources"
SOURCE_FORM_SCHEMA = "paradev.source-form.v1"
SOURCE_FORM_PATH = "/projects/{project_id}/sources/form"
LOCALIZATION_WORKSPACE_PATH = "/projects/{project_id}/localization/workspace"
LOCALIZATION_PLAN_PATH = "/projects/{project_id}/localization/plan"


def _localization_request_schema(*, include_operation: bool) -> dict[str, object]:
    properties = {
        "project_root": {"type": ["string", "null"]},
        **localization_request_input_properties(include_operation=include_operation),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": localization_request_required_fields(include_operation=include_operation),
        "properties": properties,
    }


_MODULE_DUPLICATE_REQUEST_FIELDS = frozenset(
    {
        "project_root",
        "module_id",
        "object_id",
        "source_root",
        "destination_source_root",
        "identity",
        "write",
        "plan_hash",
    }
)
_MODULE_METADATA_CLEAN_REQUEST_FIELDS = frozenset(
    {
        "project_root",
        "family",
        "module_id",
        "source_root",
        "write",
        "plan_hash",
    }
)
_MODULE_COLLECTION_SET_REQUEST_FIELDS = frozenset(
    {
        "project_root",
        "module_id",
        "collection_id",
        "source_root",
        "write",
        "plan_hash",
    }
)
_MODULE_ACTIVITY_SET_REQUEST_FIELDS = frozenset(
    {
        "project_root",
        "module_id",
        "active",
        "source_root",
        "write",
        "plan_hash",
    }
)
_MODULE_DIAGRAM_EDIT_REQUEST_FIELDS = frozenset(
    {
        "project_root",
        "family",
        "profile",
        "position_intents",
        "edge_intents",
        "node_intents",
        "write",
        "plan_hash",
    }
)
REST_API_TABLE_SCHEMA = "paradev.rest.api-table.v1"
_REST_API_REFERENCE_PAGE = "docs/user-manual/rest-api-reference.md"
_REST_API_TEST_ANCHOR = "tests/test_architecture.py::test_rest_api_table_lists_openapi_routes"
_REST_API_STANDARD_FIELDS = (
    "symbol",
    "kind",
    "layer",
    "feature",
    "method",
    "path",
    "inputs",
    "required_inputs",
    "returns",
    "raises",
    "frontend_operation_ids",
    "registry_seam",
    "doc_page",
    "test_anchor",
)
_REST_API_INDEX_NAMES = ("method_index", "feature_index", "frontend_operation_index")
_REST_API_ROW_LIST_FIELDS = ("frontend_operation_ids",)
_REST_API_METHODS = ("get", "post", "put", "patch", "delete")
_AI_CHAT_SOURCE_KINDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": list(AI_CHAT_SOURCE_KIND_VALUES)},
}
_AI_CHAT_SOURCE_KIND_ROW_SCHEMA = {
    "type": "object",
    "required": ["id", "labelKey", "label", "frontendKinds"],
    "properties": {
        "id": {"type": "string", "enum": list(AI_CHAT_SOURCE_KIND_VALUES)},
        "labelKey": {"type": "string"},
        "label": {"type": "string"},
        "frontendKinds": {
            "type": "array",
            "items": {"type": "string"},
            "default": list(AI_CHAT_SOURCE_KIND_ROWS[0]["frontendKinds"]),
        },
    },
}
_AI_CHAT_PROFILE_SCHEMA = {
    "type": "object",
    "required": ["id", "label", "detail", "prompt", "sourceKinds"],
    "properties": {
        "id": {"type": "string"},
        "label": {"type": "string"},
        "labelKey": {"type": "string"},
        "detail": {"type": "string"},
        "detailKey": {"type": "string"},
        "prompt": {"type": "string"},
        "promptKey": {"type": "string"},
        "sourceKinds": _AI_CHAT_SOURCE_KINDS_SCHEMA,
    },
}
_AI_CHAT_PROFILE_RESPONSE = {
    "description": "Desktop AI chat profile payload.",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": [
                    "schema",
                    "defaultRole",
                    "projectRoot",
                    "sourceKinds",
                    "sourceKindRows",
                    "profiles",
                ],
                "properties": {
                    "schema": {
                        "type": "string",
                        "const": "paradev.desktop.ai-chat-profiles.v1",
                    },
                    "defaultRole": {"type": "string"},
                    "projectRoot": {"type": "string"},
                    "sourceKinds": _AI_CHAT_SOURCE_KINDS_SCHEMA,
                    "sourceKindRows": {
                        "type": "array",
                        "items": _AI_CHAT_SOURCE_KIND_ROW_SCHEMA,
                    },
                    "profiles": {"type": "array", "items": _AI_CHAT_PROFILE_SCHEMA},
                },
            }
        }
    },
}


class RestApiRow(TypedDict):
    """One OpenAPI-backed REST route table row."""

    symbol: str
    kind: str
    layer: str
    feature: str
    method: str
    path: str
    inputs: str
    required_inputs: str
    returns: str
    raises: str
    registry_seam: str
    surface: str
    frontend_operation_ids: list[str]
    doc_page: str
    test_anchor: str


class RestApiTable(TypedDict):
    """Generated API-standard table for local REST/OpenAPI routes."""

    schema: str
    row_count: int
    method_index: dict[str, list[str]]
    feature_index: dict[str, list[str]]
    frontend_operation_index: dict[str, list[str]]
    rows: list[RestApiRow]


def get_rest_api_table() -> RestApiTable:
    """Return the API-standard table for local REST/OpenAPI routes.

    Returns:
        JSON-safe API table derived from `get_openapi_seed()`, with copied
        route rows and grouped indexes for method, feature, and frontend
        operation audits.
    """

    rows = _rest_api_rows(get_openapi_seed())
    method_index, feature_index, frontend_operation_index = api_symbol_indexes(
        rows,
        "method",
        "feature",
        "frontend_operation_ids",
        list_fields=("frontend_operation_ids",),
    )
    return {
        "schema": REST_API_TABLE_SCHEMA,
        "row_count": len(rows),
        "method_index": method_index,
        "feature_index": feature_index,
        "frontend_operation_index": frontend_operation_index,
        "rows": [dict(row) for row in rows],
    }


def get_rest_api_selection(
    symbol: str | None = None,
    index_name: str | None = None,
    key: str | None = None,
) -> RestApiTable | RestApiRow | list[str]:
    """Return the REST route table, one route row, or one index projection.

    Args:
        symbol: Optional route table symbol such as `GET /frontend-api`.
        index_name: Optional index payload name such as `method_index`,
            `feature_index`, or `frontend_operation_index`.
        key: Optional concrete index key used with `index_name`.

    Returns:
        Full REST route table, one route row, or one ordered route-symbol list.

    Raises:
        ValueError: If selector arguments are ambiguous, incomplete, or use an
            unsupported index.
        KeyError: If `symbol` does not match a route row.
    """

    return cast(
        RestApiTable | RestApiRow | list[str],
        api_table_selection(
            get_rest_api_table(),
            row_key_field="symbol",
            row_key=symbol,
            index_name=index_name,
            key=key,
            row_list_fields=_REST_API_ROW_LIST_FIELDS,
            index_names=_REST_API_INDEX_NAMES,
        ),
    )


def render_rest_api_reference_markdown() -> str:
    """Render the local REST/OpenAPI route table as Markdown.

    Returns:
        Deterministic Markdown suitable for
        `docs/user-manual/rest-api-reference.md`. The content is generated
        from `get_rest_api_table()` so REST docs, OpenAPI seed rows, and
        frontend-operation annotations stay aligned.
    """

    table = get_rest_api_table()
    return api_reference_markdown(
        title="REST API Reference",
        source="paradev.surfaces.rest.get_rest_api_table()",
        regenerate_when="Regenerate this file whenever the REST/OpenAPI route table changes:",
        command="rtk uv run paradev rest-api --markdown > docs/user-manual/rest-api-reference.md",
        sections=api_indexed_reference_sections(
            summary_lines=[
                f"- API rows / API 行数: {table['row_count']}",
                f"- Methods / Method 数: {len(table['method_index'])}",
                f"- Features / Feature 数: {len(table['feature_index'])}",
                "- Frontend-bound operations / 前端绑定操作数: " f"{len(table['frontend_operation_index'])}",
                "- Selector helper / Selector helper: Use "
                "`get_rest_api_selection(symbol=..., index_name=..., key=...)` "
                "returns the table, one route row, or one route-symbol index projection.",
            ],
            table=table,
            index_specs=(
                (
                    "Method Index / Method 索引",
                    "method_index",
                    "Method",
                    "Routes",
                    "Symbols",
                ),
                (
                    "Feature Index / Feature 索引",
                    "feature_index",
                    "Feature",
                    "Routes",
                    "Symbols",
                ),
                (
                    "Frontend Operation Index / 前端操作索引",
                    "frontend_operation_index",
                    "Operation",
                    "Routes",
                    "Symbols",
                ),
            ),
            fields=_REST_API_STANDARD_FIELDS,
            list_fields=("frontend_operation_ids",),
        ),
    )


def get_openapi_seed() -> dict[str, object]:
    """Return a JSON-safe OpenAPI seed without starting a server."""

    inspection_contract = get_project_inspection_contract()
    frontend_api_contract = get_frontend_api_contract()
    seed: dict[str, object] = {
        "openapi": "3.1.0",
        "info": {
            "title": "ParaDev Local API",
            "version": "0.1.0",
            "summary": "Local REST contract over the ParaDev SDK API.",
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Check local API health.",
                    "responses": {"200": {"description": "Local API process is responding."}},
                }
            },
            "/api-catalog": {
                "get": {
                    "summary": "Read the aggregate API catalog table, one reference row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "reference_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "API catalog table, row, or index lookup payload."},
                        "400": {"description": "Invalid API catalog selector."},
                    },
                }
            },
            "/rest-api": {
                "get": {
                    "summary": "Read the REST API table, one row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "REST API table, row, or index lookup payload."},
                        "400": {"description": "Invalid REST API selector."},
                    },
                }
            },
            "/cli-api": {
                "get": {
                    "summary": "Read the CLI API table, one row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "CLI API table, row, or index lookup payload."},
                        "400": {"description": "Invalid CLI API selector."},
                    },
                }
            },
            "/surface-contracts": {
                "get": {
                    "summary": "Read the static surface contract summary, one contract payload, or one status id list.",
                    "parameters": [
                        {
                            "name": "identifier",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "status",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Surface contract summary, contract payload, or status id list."},
                        "400": {"description": "Invalid surface contract selector."},
                    },
                }
            },
            "/frontend-api": {
                "get": {
                    "summary": "Read the SDK-owned frontend API contract.",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "group_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "form",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "x-paradev-frontend-api-contract": frontend_api_contract,
                    "responses": {
                        "200": {"description": "Frontend API contract or selected projection."},
                        "400": {"description": "Invalid frontend API selector."},
                    },
                }
            },
            "/frontend-api/workspace": {
                "get": {
                    "summary": "Read the SDK-owned frontend workspace projection.",
                    "responses": {"200": {"description": "Frontend API workspace projection."}},
                }
            },
            "/frontend-api/action": {
                "get": {
                    "summary": "Read one SDK-owned frontend action detail.",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Frontend API action detail."}},
                }
            },
            "/frontend-api/normalize": {
                "post": {
                    "summary": "Normalize frontend API form values through the SDK.",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            }
                        },
                    },
                    "responses": {"200": {"description": "Normalized frontend API input payload."}},
                }
            },
            "/frontend-api/rest-request": {
                "post": {
                    "summary": "Plan a frontend API REST request through the SDK.",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            }
                        },
                    },
                    "responses": {"200": {"description": "Frontend API REST request plan."}},
                }
            },
            "/frontend-api/options": {
                "post": {
                    "summary": "Resolve frontend API field options through the SDK.",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "field_name",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            }
                        },
                    },
                    "responses": {"200": {"description": "Frontend API option-source payload."}},
                }
            },
            "/frontend-api/binding": {
                "get": {
                    "summary": "Map one surface call key back to frontend operation ids.",
                    "parameters": [
                        {
                            "name": "binding_surface",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": list(FRONTEND_API_BINDING_SURFACES),
                            },
                        },
                        {
                            "name": "binding_key",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Frontend API binding lookup payload."}},
                }
            },
            "/architecture": {
                "get": {
                    "summary": "Read the SDK-owned architecture graph or architecture API table selection.",
                    "parameters": [
                        {
                            "name": "api_table",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "ParaDev architecture graph or architecture API table selection."},
                        "400": {"description": "Invalid architecture API table selector."},
                    },
                }
            },
            SOURCE_PATH: {
                "get": {
                    "summary": "Read one project source file as UTF-8 text.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "project_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Project source text and stable revision.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "schema",
                                            "project_id",
                                            "path",
                                            "relative_path",
                                            "encoding",
                                            "size",
                                            "mtime_ns",
                                            "text",
                                        ],
                                        "properties": {
                                            "schema": {"const": "paradev.rest.source_text.v1"},
                                            "project_id": {"type": "string"},
                                            "path": {"type": "string"},
                                            "relative_path": {"type": "string"},
                                            "encoding": {"const": "utf-8"},
                                            "size": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "mtime_ns": {
                                                "type": "string",
                                                "pattern": "^[0-9]+$",
                                            },
                                            "text": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                        "400": {"description": "Invalid project or source path."},
                    },
                }
            },
            SOURCE_FORM_PATH: {
                "post": {
                    "summary": "Return an optional Registry-owned guided form for one JSON or PDX project source.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["path", "text"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "project_root": {"type": ["string", "null"]},
                                        "path": {"type": "string"},
                                        "text": {"type": "string"},
                                        "query": {"type": "string", "maxLength": 200},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Source form payload or null when unsupported."},
                        "400": {"description": "Invalid project, source path, editor text, or family form contract."},
                    },
                }
            },
            LOCALIZATION_WORKSPACE_PATH: {
                "post": {
                    "summary": "Return one Registry-owned cross-language source-unit localization workspace.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": _localization_request_schema(include_operation=False),
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Registry-owned source-unit localization workspace."},
                        "400": {"description": "Invalid project, target, source, draft, or localization syntax."},
                    },
                }
            },
            LOCALIZATION_PLAN_PATH: {
                "post": {
                    "summary": "Plan one lossless Registry-owned source-unit localization edit without writing.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": _localization_request_schema(include_operation=True),
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Revision-guarded source-unit localization update plan."},
                        "400": {"description": "Invalid project, target, draft, localization operation, or source state."},
                    },
                }
            },
            DRAFT_APPLY_PATH: {
                "post": {
                    "summary": "Apply validated project draft edits.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "anyOf": [
                                        {
                                            "required": ["source_edits"],
                                            "properties": {"source_edits": {"minItems": 1}},
                                        },
                                        {
                                            "required": ["source_removals"],
                                            "properties": {"source_removals": {"minItems": 1}},
                                        },
                                        {
                                            "required": ["source_replacements"],
                                            "properties": {"source_replacements": {"minItems": 1}},
                                        },
                                        {
                                            "required": ["module_rename"],
                                        },
                                    ],
                                    "properties": {
                                        "project_root": {"type": "string"},
                                        "source_edits": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "description": (
                                                    "Text replacement with an optional expected_size and expected_mtime_ns pair "
                                                    "that rejects writes when the source changed since it was read."
                                                ),
                                                "required": ["path", "text"],
                                                "dependentRequired": {
                                                    "expected_size": ["expected_mtime_ns"],
                                                    "expected_mtime_ns": ["expected_size"],
                                                },
                                                "properties": {
                                                    "path": {"type": "string"},
                                                    "text": {"type": "string"},
                                                    "expected_size": {
                                                        "type": "integer",
                                                        "minimum": 0,
                                                    },
                                                    "expected_mtime_ns": {
                                                        "type": "string",
                                                        "pattern": "^[0-9]+$",
                                                    },
                                                },
                                            },
                                        },
                                        "source_removals": {
                                            "type": "array",
                                            "items": {
                                                "oneOf": [
                                                    {"type": "string"},
                                                    {
                                                        "type": "object",
                                                        "additionalProperties": False,
                                                        "description": (
                                                            "Removal target with an optional expected_size and "
                                                            "expected_mtime_ns pair that rejects stale deletion."
                                                        ),
                                                        "required": ["path"],
                                                        "dependentRequired": {
                                                            "expected_size": ["expected_mtime_ns"],
                                                            "expected_mtime_ns": ["expected_size"],
                                                        },
                                                        "properties": {
                                                            "path": {"type": "string"},
                                                            "expected_size": {
                                                                "type": "integer",
                                                                "minimum": 0,
                                                            },
                                                            "expected_mtime_ns": {
                                                                "type": "string",
                                                                "pattern": "^[0-9]+$",
                                                            },
                                                        },
                                                    },
                                                ]
                                            },
                                        },
                                        "source_replacements": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "description": (
                                                    "Binary replacement. content_format and target_format are optional together; "
                                                    "content_format=png converts the upload to the target file format before replacement. "
                                                    "An optional expected_size and expected_mtime_ns pair rejects writes when the source "
                                                    "changed since it was read; expected_absent=true rejects a new-file write if the "
                                                    "target appeared meanwhile."
                                                ),
                                                "required": ["path", "content_base64"],
                                                "dependentRequired": {
                                                    "expected_size": ["expected_mtime_ns"],
                                                    "expected_mtime_ns": ["expected_size"],
                                                    "content_format": ["target_format"],
                                                    "target_format": ["content_format"],
                                                },
                                                "allOf": [
                                                    {
                                                        "if": {
                                                            "required": ["expected_absent"],
                                                            "properties": {"expected_absent": {"const": True}},
                                                        },
                                                        "then": {
                                                            "not": {
                                                                "anyOf": [
                                                                    {"required": ["expected_size"]},
                                                                    {"required": ["expected_mtime_ns"]},
                                                                ]
                                                            }
                                                        },
                                                    }
                                                ],
                                                "properties": {
                                                    "path": {"type": "string"},
                                                    "content_base64": {"type": "string"},
                                                    "expected_size": {
                                                        "type": "integer",
                                                        "minimum": 0,
                                                    },
                                                    "expected_mtime_ns": {
                                                        "type": "string",
                                                        "pattern": "^[0-9]+$",
                                                    },
                                                    "expected_absent": {
                                                        "type": "boolean",
                                                    },
                                                    "content_format": {
                                                        "type": "string",
                                                        "enum": ["png"],
                                                    },
                                                    "target_format": {
                                                        "type": "string",
                                                        "enum": [
                                                            "bmp",
                                                            "dds",
                                                            "jpeg",
                                                            "jpg",
                                                            "tga",
                                                            "webp",
                                                        ],
                                                    },
                                                },
                                            },
                                        },
                                        "module_rename": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "description": ("Optional canonical module-folder rename committed after every source draft succeeds."),
                                            "required": [
                                                "module_id",
                                                "object_id",
                                            ],
                                            "properties": {
                                                "module_id": {"type": "string"},
                                                "object_id": {"type": "string"},
                                                "source_root": {
                                                    "type": ["string", "null"],
                                                },
                                                "title": {
                                                    "type": ["string", "null"],
                                                },
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Applied project draft edits."},
                        "400": {"description": "Invalid project or draft request."},
                    },
                }
            },
            MODULE_DRAFT_PATH: {
                "post": {
                    "summary": "Plan or write a source-module draft from an SDK authoring template.",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["object_id"],
                                    "properties": {
                                        "project_root": {"type": "string"},
                                        "template_id": {"type": "string"},
                                        "object_id": {"type": "string"},
                                        "values": {
                                            "type": "object",
                                            "additionalProperties": {"type": ["string", "number", "boolean"]},
                                        },
                                        "write": {"type": "boolean", "default": False},
                                        "force": {"type": "boolean", "default": False},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK scaffold draft plan."},
                        "400": {"description": "Invalid project or scaffold request."},
                    },
                }
            },
            MODULE_CREATE_BATCH_PATH: {
                "post": {
                    "summary": "Plan or atomically create several source modules through the SDK.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["project_id", "modules"],
                                    "properties": {
                                        "project_id": {"type": "string"},
                                        "project_root": {"type": "string"},
                                        "modules": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": MAX_MODULE_CREATE_BATCH_SIZE,
                                            "items": {
                                                "type": "object",
                                                "required": ["object_id"],
                                                "properties": {
                                                    "family": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                    },
                                                    "template_id": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                    },
                                                    "family_or_template": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                    },
                                                    "object_id": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                    },
                                                    "values": {
                                                        "type": "object",
                                                        "additionalProperties": True,
                                                    },
                                                },
                                                "oneOf": [
                                                    {"required": ["family"]},
                                                    {"required": ["template_id"]},
                                                    {"required": ["family_or_template"]},
                                                ],
                                                "additionalProperties": False,
                                            },
                                        },
                                        "source_root": {"type": "string"},
                                        "write": {"type": "boolean", "default": False},
                                        "plan_hash": {"type": "string"},
                                    },
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK module batch plan or apply payload."},
                        "400": {"description": "Invalid project or module batch request."},
                    },
                }
            },
            MODULE_DUPLICATE_PATH: {
                "post": {
                    "summary": "Plan or atomically duplicate one source module without rewriting its content.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "project_root",
                                        "module_id",
                                        "object_id",
                                    ],
                                    "properties": {
                                        "project_root": {"type": "string"},
                                        "module_id": {"type": "string"},
                                        "object_id": {"type": "string"},
                                        "source_root": {"type": "string"},
                                        "destination_source_root": {"type": "string"},
                                        "write": {"type": "boolean", "default": False},
                                        "plan_hash": {"type": "string"},
                                    },
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK module duplicate plan or apply payload."},
                        "400": {"description": "Invalid project or module duplicate request."},
                    },
                }
            },
            MODULE_METADATA_CLEAN_PATH: {
                "post": {
                    "summary": "Plan or atomically remove redundant path-derived module metadata.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["project_root"],
                                    "properties": {
                                        "project_root": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "family": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "module_id": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "source_root": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "write": {
                                            "type": "boolean",
                                            "default": False,
                                        },
                                        "plan_hash": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                    },
                                    "allOf": [
                                        {
                                            "if": {
                                                "properties": {
                                                    "write": {"const": True},
                                                },
                                                "required": ["write"],
                                            },
                                            "then": {"required": ["plan_hash"]},
                                        }
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK module metadata cleanup plan or apply payload."},
                        "400": {"description": "Invalid project or metadata cleanup request."},
                    },
                }
            },
            MODULE_COLLECTION_SET_PATH: {
                "post": {
                    "summary": "Plan or atomically change one module's collection membership.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["project_root", "module_id"],
                                    "properties": {
                                        "project_root": {"type": "string", "minLength": 1},
                                        "module_id": {"type": "string", "minLength": 1},
                                        "collection_id": {
                                            "type": ["string", "null"],
                                            "minLength": 1,
                                        },
                                        "source_root": {"type": "string", "minLength": 1},
                                        "write": {"type": "boolean", "default": False},
                                        "plan_hash": {"type": "string", "minLength": 1},
                                    },
                                    "allOf": [
                                        {
                                            "if": {
                                                "properties": {"write": {"const": True}},
                                                "required": ["write"],
                                            },
                                            "then": {"required": ["plan_hash"]},
                                        }
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK module collection plan or apply payload."},
                        "400": {"description": "Invalid project or module collection request."},
                    },
                }
            },
            MODULE_ACTIVITY_SET_PATH: {
                "post": {
                    "summary": "Plan or atomically activate or deactivate one module.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["project_root", "module_id", "active"],
                                    "properties": {
                                        "project_root": {"type": "string", "minLength": 1},
                                        "module_id": {"type": "string", "minLength": 1},
                                        "active": {"type": "boolean"},
                                        "source_root": {"type": "string", "minLength": 1},
                                        "write": {"type": "boolean", "default": False},
                                        "plan_hash": {"type": "string", "minLength": 1},
                                    },
                                    "allOf": [
                                        {
                                            "if": {
                                                "properties": {"write": {"const": True}},
                                                "required": ["write"],
                                            },
                                            "then": {"required": ["plan_hash"]},
                                        }
                                    ],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "SDK module activity plan or apply payload."},
                        "400": {"description": "Invalid project or module activity request."},
                    },
                }
            },
            MODULE_DIAGRAM_PATH: {
                "get": {
                    "summary": "Project an authoritative module family into a source-backed diagram.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "minLength": 1,
                                "default": ".",
                            },
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": True,
                            "schema": module_diagram_family_schema(editable=False),
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "minLength": 1},
                        },
                    ],
                    "responses": {
                        "200": {"description": "SDK source-backed module diagram payload."},
                        "400": {"description": "Invalid project or module diagram request."},
                    },
                }
            },
            MODULE_DIAGRAM_EDIT_PATH: {
                "post": {
                    "summary": "Plan or apply bounded source-backed module diagram edits.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": module_diagram_edit_input_schema(project_field="project_root")}},
                    },
                    "responses": {
                        "200": {"description": "SDK module diagram edit plan or apply payload."},
                        "400": {"description": "Invalid project or module diagram edit request."},
                    },
                }
            },
            "/pdx/parse": {
                "get": {
                    "summary": "Parse one PDX file through the SDK.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "include_dump",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "include_tokens",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {"200": {"description": "PDX parse payload."}},
                }
            },
            "/pdx/format": {
                "post": {
                    "summary": "Format one PDX file through the SDK.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "indent",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "\t"},
                        },
                        {
                            "name": "comments",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": True},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {"200": {"description": "PDX format payload."}},
                }
            },
            "/pdx-api": {
                "get": {
                    "summary": "Read the PDX API table, one row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "PDX API table, row, or index lookup payload."},
                        "400": {"description": "Invalid PDX API selector."},
                    },
                }
            },
            "/lsp-api": {
                "get": {
                    "summary": "Read the LSP API table, one row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "LSP API table, row, or index lookup payload."},
                        "400": {"description": "Invalid LSP API selector."},
                    },
                }
            },
            "/catalog-api": {
                "get": {
                    "summary": "Read the Catalog API table, one row, or one reverse-index id list.",
                    "parameters": [
                        {
                            "name": "symbol",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "index_name",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "key",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Catalog API table, row, or index lookup payload."},
                        "400": {"description": "Invalid catalog API selector."},
                    },
                }
            },
            "/lsp/diagnostics": {
                "post": {
                    "summary": "Return LSP diagnostics for one PDX document.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP diagnostics payload."}},
                }
            },
            "/lsp/symbols": {
                "post": {
                    "summary": "Return LSP document symbols for one PDX document.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP document symbols payload."}},
                }
            },
            "/lsp/hover": {
                "post": {
                    "summary": "Return LSP hover details for one PDX document position.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text", "line", "character"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "line": {"type": "integer", "minimum": 0},
                                        "character": {"type": "integer", "minimum": 0},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP hover payload."}},
                }
            },
            "/lsp/formatting": {
                "post": {
                    "summary": "Return LSP text edits for one PDX document.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                        "indent": {"type": "string", "default": "\t"},
                                        "comments": {
                                            "type": "boolean",
                                            "default": True,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP formatting payload."}},
                }
            },
            "/lsp/completion": {
                "post": {
                    "summary": "Return LSP completion items for one PDX document position.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text", "line", "character"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "line": {"type": "integer", "minimum": 0},
                                        "character": {"type": "integer", "minimum": 0},
                                        "offset": {"type": "integer", "minimum": 0},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                        "project_path": {"type": "string"},
                                        "database": {"type": "string"},
                                        "game_root": {"type": "string"},
                                        "limit": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "default": 100,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP completion payload."}},
                }
            },
            "/lsp/semantic-tokens": {
                "post": {
                    "summary": "Return LSP semantic tokens for one PDX document.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {"type": "string"},
                                        "uri": {"type": "string"},
                                        "path": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "LSP semantic tokens payload."}},
                }
            },
            "/lsp/keywords": {
                "get": {
                    "summary": "Return the HOI4 keyword dataset used by editor completion.",
                    "parameters": [
                        {
                            "name": "game_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "HOI4 keyword dataset payload."}},
                }
            },
            "/projects/inspections": {
                "get": {
                    "summary": "List SDK project inspection kinds and filters.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        }
                    ],
                    "responses": {"200": {"description": "Project inspection contract."}},
                }
            },
            "/projects/templates": {
                "get": {
                    "summary": "List SDK authoring templates and source roots.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "template_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "kind",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "enum": ["module", "collection"],
                            },
                        },
                        {
                            "name": "source",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "authoring_ready",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                        {
                            "name": "diagnostic_code",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project authoring template payload."}},
                }
            },
            "/projects/authoring-path": {
                "get": {
                    "summary": "Resolve one module or collection authoring path without writing files.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "kind",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "target_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project authoring path payload."}},
                }
            },
            "/projects/authoring-plan": {
                "get": {
                    "summary": "Resolve an authoring path and expected source-slot contract without writing files.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "kind",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "target_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project authoring plan payload."}},
                }
            },
            "/projects/scaffold": {
                "post": {
                    "summary": "Plan or write a source module from an SDK authoring template.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "template_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "object_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "force",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "values": {
                                            "type": "object",
                                            "additionalProperties": True,
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Project module scaffold payload."}},
                }
            },
            "/projects/build": {
                "post": {
                    "summary": "Run a dry build plan or emit build outputs through the SDK.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "emit_artifacts",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "emit_manifests",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "strict_metadata",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "full_rebuild",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "sync_launcher_descriptor",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": True},
                        },
                        {
                            "name": "parallelism",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Build result payload."},
                        "400": {"description": "Invalid project path or build options."},
                    },
                }
            },
            "/projects/catalog": {
                "get": {
                    "summary": "Return the read-only status of one project's local HeavenBase catalog database.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                    ],
                    "responses": {"200": {"description": "Catalog status payload."}},
                },
                "post": {
                    "summary": "Write a local HeavenBase catalog database for one project.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "database",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Catalog write payload."}},
                },
                "put": {
                    "summary": "Refresh a local HeavenBase catalog database for one project.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "database",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Catalog refresh payload."}},
                },
            },
            "/projects": {
                "get": {
                    "summary": "Open a project and return its SDK view model.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "game",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "title",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project view payload."}},
                },
                "post": {
                    "summary": "Create a starter project through the SDK.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "project_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "title",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "game",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "hoi4"},
                        },
                        {
                            "name": "force",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {"200": {"description": "Project create payload."}},
                },
            },
            "/projects/list": {
                "get": {
                    "summary": "List registered local ParaDev projects.",
                    "parameters": [
                        {
                            "name": "project_paths",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                        {
                            "name": "search_roots",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                    ],
                    "responses": {"200": {"description": "Project registry payload."}},
                }
            },
            "/desktop/state": {
                "get": {
                    "summary": "Return SDK-owned project state for the desktop shell.",
                    "parameters": [
                        {
                            "name": "project_path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "project_paths",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                        {
                            "name": "search_roots",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                    ],
                    "responses": {"200": {"description": "Desktop state payload."}},
                }
            },
            "/desktop/builds": {
                "get": {
                    "summary": "List active and retained terminal desktop ParaDev build runs.",
                    "parameters": [
                        {
                            "name": "project_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Desktop build-runs payload."}},
                },
                "post": {
                    "summary": "Start one desktop ParaDev build through the Python desktop facade.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["project_root"],
                                    "properties": {
                                        "projectRoot": {"type": "string"},
                                        "project_root": {"type": "string"},
                                        "mode": {
                                            "type": "string",
                                            "enum": ["cached", "full"],
                                            "default": "cached",
                                        },
                                        "profile": {"type": "string"},
                                        "strictMetadata": {"type": "boolean"},
                                        "strict_metadata": {"type": "boolean"},
                                        "parallelism": {
                                            "type": "integer",
                                            "minimum": 1,
                                        },
                                        "target": {
                                            "type": "object",
                                            "required": ["kind", "id"],
                                            "properties": {
                                                "kind": {
                                                    "type": "string",
                                                    "enum": [
                                                        "module",
                                                        "collection",
                                                        "family",
                                                    ],
                                                },
                                                "id": {"type": "string"},
                                                "family": {"type": "string"},
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop build run payload."}},
                },
            },
            "/desktop/builds/status": {
                "get": {
                    "summary": "Return active or retained terminal status for one exact desktop ParaDev build run.",
                    "parameters": [
                        {
                            "name": "run_id",
                            "in": "query",
                            "required": True,
                            "schema": nonblank_run_id_schema(),
                        },
                    ],
                    "responses": {"200": {"description": "Desktop build run payload."}},
                }
            },
            "/desktop/builds/interrupt": {
                "post": {
                    "summary": "Interrupt one exact active desktop ParaDev build run or return its retained terminal status.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": desktop_build_interrupt_request_schema()}},
                    },
                    "responses": {"200": {"description": "Desktop build run payload."}},
                }
            },
            "/desktop/open-path": {
                "post": {
                    "summary": "Open one local path through the Python desktop facade.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["path"],
                                    "properties": {
                                        "path": {"type": "string"},
                                        "target": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop open-path payload."}},
                }
            },
            "/desktop/select-project": {
                "post": {
                    "summary": "Select one local ParaDev project folder through the native Python host.",
                    "responses": {"200": {"description": "Desktop project-selection payload."}},
                }
            },
            "/desktop/import-project-package": {
                "post": {
                    "summary": "Select and transactionally install one verified project package.",
                    "responses": {"200": {"description": "Desktop project-package installation payload or null."}},
                }
            },
            "/desktop/path-status": {
                "get": {
                    "summary": "Return SDK-owned status for one desktop local path.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Desktop path status payload."}},
                }
            },
            "/desktop/hoi4-launch-readiness": {
                "get": {
                    "summary": "Return authoritative read-only HOI4 launch readiness for one project.",
                    "parameters": [
                        {
                            "name": "project_root",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Desktop HOI4 launch-readiness payload."}},
                }
            },
            "/desktop/app-config": {
                "get": {
                    "summary": "Read desktop-only GUI app settings.",
                    "responses": {"200": {"description": "Desktop app config payload."}},
                },
                "put": {
                    "summary": "Write desktop-only GUI app settings.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop app config write status."}},
                },
            },
            "/desktop/config-value": {
                "get": {
                    "summary": "Read one CM_PARADEV-backed desktop config value.",
                    "parameters": [
                        {
                            "name": "key",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Desktop config value payload."}},
                },
                "put": {
                    "summary": "Write one CM_PARADEV-backed desktop config value.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["key", "value"],
                                    "properties": {
                                        "key": {"type": "string"},
                                        "value": {},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop config value payload."}},
                },
            },
            "/desktop/llm/test": {
                "post": {
                    "summary": "Test one desktop LLM route through the Python SDK.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["provider", "model", "gateway"],
                                    "properties": {
                                        "provider": {
                                            "type": "string",
                                            "default": "deepseek",
                                        },
                                        "model": {
                                            "type": "string",
                                            "default": "deepseek-v4-flash",
                                        },
                                        "gateway": {
                                            "type": "string",
                                            "default": "openai",
                                        },
                                        "preset": {
                                            "type": "string",
                                            "default": "chat",
                                            "enum": [
                                                "system",
                                                "chat",
                                                "reason",
                                                "coder",
                                            ],
                                        },
                                        "key_env": {"type": "string"},
                                        "base_url": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop LLM route test payload."}},
                }
            },
            "/desktop/ai/chat": {
                "post": {
                    "summary": "Send one desktop AI chat prompt through the Python SDK and HeavenBase.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "provider",
                                        "model",
                                        "gateway",
                                        "prompt",
                                    ],
                                    "properties": {
                                        "provider": {
                                            "type": "string",
                                            "default": "deepseek",
                                        },
                                        "model": {
                                            "type": "string",
                                            "default": "deepseek-v4-flash",
                                        },
                                        "gateway": {
                                            "type": "string",
                                            "default": "openai",
                                        },
                                        "preset": {
                                            "type": "string",
                                            "default": "chat",
                                            "enum": [
                                                "system",
                                                "chat",
                                                "reason",
                                                "coder",
                                            ],
                                        },
                                        "key_env": {"type": "string"},
                                        "base_url": {"type": "string"},
                                        "prompt": {"type": "string"},
                                        "role": {"type": "string", "default": "chat"},
                                        "project_root": {"type": "string"},
                                        "sources": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": True,
                                            },
                                            "default": [],
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Desktop AI chat payload."}},
                }
            },
            "/desktop/ai/profiles": {
                "get": {
                    "summary": "Return SDK-owned desktop AI chat role profiles.",
                    "x-paradev-frontend-api-operation-ids": ["ai.profiles"],
                    "parameters": [
                        {
                            "name": "project_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": _AI_CHAT_PROFILE_RESPONSE},
                }
            },
            "/desktop/ai/profiles/{profile_id}": {
                "put": {
                    "summary": "Write one SDK-owned desktop AI chat role profile override.",
                    "x-paradev-frontend-api-operation-ids": ["ai.profile.write"],
                    "parameters": [
                        {
                            "name": "profile_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {
                                            "type": "object",
                                            "required": ["profile"],
                                            "properties": {
                                                "profile": {
                                                    "type": "object",
                                                    "properties": {
                                                        "label": {"type": "string"},
                                                        "detail": {"type": "string"},
                                                        "prompt": {"type": "string"},
                                                        "sourceKinds": _AI_CHAT_SOURCE_KINDS_SCHEMA,
                                                    },
                                                },
                                                "project_root": {"type": "string"},
                                            },
                                        },
                                        {
                                            "type": "object",
                                            "required": ["prompt"],
                                            "properties": {
                                                "label": {"type": "string"},
                                                "detail": {"type": "string"},
                                                "prompt": {"type": "string"},
                                                "sourceKinds": _AI_CHAT_SOURCE_KINDS_SCHEMA,
                                                "projectRoot": {"type": "string"},
                                            },
                                        },
                                    ],
                                }
                            }
                        },
                    },
                    "responses": {"200": _AI_CHAT_PROFILE_RESPONSE},
                },
                "delete": {
                    "summary": "Reset one SDK-owned desktop AI chat role profile override to built-in defaults.",
                    "x-paradev-frontend-api-operation-ids": ["ai.profile.reset"],
                    "parameters": [
                        {
                            "name": "profile_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "project_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": _AI_CHAT_PROFILE_RESPONSE},
                },
            },
            "/projects/browser": {
                "get": {
                    "summary": "Return the read-only project browser payload for frontend clients.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "profile",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "kind",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "enum": ["module", "collection"],
                            },
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project browser payload."}},
                }
            },
            "/projects/find": {
                "get": {
                    "summary": "Find a project from a root or nested path.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        }
                    ],
                    "responses": {"200": {"description": "Project discovery payload."}},
                }
            },
            "/projects/rename": {
                "patch": {
                    "summary": "Rename a project's display title without moving files.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "title",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project rename payload."}},
                }
            },
            "/projects/language": {
                "patch": {
                    "summary": "Plan or apply the project-wide authoring language.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "preferred_language",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": [
                                    "en",
                                    "fr",
                                    "de",
                                    "ru",
                                    "es",
                                    "pl",
                                    "pt_br",
                                    "zh",
                                    "ja",
                                    "ko",
                                ],
                            },
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "plan_hash",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Guarded project language plan or apply payload."}},
                }
            },
            "/projects/modules/rename": {
                "patch": {
                    "summary": "Rename a source module folder or synchronize its readable title without rewriting PDX content.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "object_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "title",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Module rename payload."}},
                }
            },
            "/projects/modules/remove": {
                "delete": {
                    "summary": "Plan or remove a source module folder.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {"200": {"description": "Module remove payload."}},
                }
            },
            "/projects/modules/file": {
                "get": {
                    "summary": "Read one text source file from a module.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "relative_path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "encoding",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "utf-8"},
                        },
                    ],
                    "responses": {"200": {"description": "Module file payload."}},
                },
                "patch": {
                    "summary": "Write one text source file inside a module.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "module_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "relative_path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "create",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "encoding",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "utf-8"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {"text": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Module file payload."}},
                },
            },
            "/projects/collections/scaffold": {
                "post": {
                    "summary": "Plan or transactionally write a collection from a Registry-backed template.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "template_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "force",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "plan_hash",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "values": {
                                            "type": "object",
                                            "additionalProperties": True,
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Guarded collection scaffold plan or apply payload."}},
                }
            },
            "/projects/collections": {
                "post": {
                    "summary": "Plan or write a collection descriptor metadata scaffold.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "force",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "metadata": {
                                            "type": "object",
                                            "additionalProperties": True,
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Collection create payload."}},
                },
                "delete": {
                    "summary": "Plan or remove a collection while preserving and ungrouping its modules.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "write",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "plan_hash",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Collection remove payload."}},
                },
            },
            "/projects/collections/rename": {
                "patch": {
                    "summary": "Rename a collection descriptor and its explicit member pointers.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "target_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Collection rename payload."}},
                }
            },
            "/projects/collections/file": {
                "get": {
                    "summary": "Read one text source file from a collection descriptor.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "relative_path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "encoding",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "utf-8"},
                        },
                    ],
                    "responses": {"200": {"description": "Collection file payload."}},
                },
                "patch": {
                    "summary": "Write one text source file inside a collection descriptor.",
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "collection_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "relative_path",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "family",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "source_root",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "create",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                        {
                            "name": "encoding",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "utf-8"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {"text": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Collection file payload."}},
                },
            },
            "/projects/inspect": {
                "get": {
                    "summary": "Run one read-only SDK project inspection.",
                    "x-paradev-inspection-contract": inspection_contract,
                    "x-paradev-catalog-query-edge": {
                        "kind": "catalog-query",
                        "default_limit": CATALOG_QUERY_DEFAULT_LIMIT,
                        "max_limit": CATALOG_QUERY_MAX_LIMIT,
                        "default_offset": CATALOG_QUERY_DEFAULT_OFFSET,
                        "default_include_data": CATALOG_QUERY_DEFAULT_INCLUDE_DATA,
                        "max_hydrated_limit": CATALOG_QUERY_MAX_HYDRATED_LIMIT,
                    },
                    "parameters": [
                        {
                            "name": "path",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "default": "."},
                        },
                        {
                            "name": "kind",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": {"description": "Project inspection payload."}},
                }
            },
        },
    }
    _attach_frontend_api_operation_ids(seed["paths"], frontend_api_contract)
    return seed


def _openapi_request_body(path: str, method: str) -> dict[str, object]:
    seed = get_openapi_seed()
    paths = seed.get("paths")
    if not isinstance(paths, Mapping):
        raise RuntimeError("OpenAPI seed paths must be an object.")
    path_item = paths.get(path)
    if not isinstance(path_item, Mapping):
        raise RuntimeError(f"OpenAPI seed path {path!r} is missing.")
    operation = path_item.get(method)
    if not isinstance(operation, Mapping):
        raise RuntimeError(f"OpenAPI seed operation {method.upper()} {path} is missing.")
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        raise RuntimeError(f"OpenAPI seed operation {method.upper()} {path} has no request body.")
    return dict(request_body)


def _attach_frontend_api_operation_ids(paths: object, frontend_api_contract: Mapping[str, object]) -> None:
    if not isinstance(paths, dict):
        return
    for (path, method), operation_ids in _frontend_api_rest_operation_ids(frontend_api_contract).items():
        path_item = paths.get(path)
        if not isinstance(path_item, dict):
            continue
        operation = path_item.get(method)
        if isinstance(operation, dict):
            operation["x-paradev-frontend-api-operation-ids"] = operation_ids


def _frontend_api_rest_operation_ids(
    frontend_api_contract: Mapping[str, object],
) -> dict[tuple[str, str], list[str]]:
    operation_ids: dict[tuple[str, str], list[str]] = {}
    operations = frontend_api_contract.get("operations", [])
    if not isinstance(operations, list):
        return operation_ids
    for row in operations:
        if not isinstance(row, dict):
            continue
        bindings = row.get("bindings", {})
        rest = bindings.get("rest") if isinstance(bindings, dict) else None
        if not isinstance(rest, dict):
            continue
        key = (str(rest["path"]), str(rest["method"]).lower())
        append_index_entry(operation_ids, key, str(row["id"]))
    return operation_ids


def _rest_api_rows(seed: Mapping[str, object]) -> list[RestApiRow]:
    paths = seed.get("paths", {})
    if not isinstance(paths, Mapping):
        return []
    rows: list[RestApiRow] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        for method in _REST_API_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, Mapping):
                continue
            frontend_operation_ids = _rest_api_frontend_operation_ids(operation)
            method_name = method.upper()
            rows.append(
                {
                    "symbol": f"{method_name} {path}",
                    "kind": "REST route",
                    "layer": "rest",
                    "feature": _rest_api_feature(path, frontend_operation_ids),
                    "method": method_name,
                    "path": path,
                    "inputs": _rest_api_inputs(operation, required_only=False),
                    "required_inputs": _rest_api_inputs(operation, required_only=True),
                    "returns": _rest_api_responses(operation, success=True),
                    "raises": _rest_api_responses(operation, success=False),
                    "registry_seam": f"OpenAPI path {path}",
                    "surface": "rest",
                    "frontend_operation_ids": frontend_operation_ids,
                    "doc_page": _REST_API_REFERENCE_PAGE,
                    "test_anchor": _REST_API_TEST_ANCHOR,
                }
            )
    return rows


def _rest_api_frontend_operation_ids(operation: Mapping[str, object]) -> list[str]:
    operation_ids = operation.get("x-paradev-frontend-api-operation-ids", [])
    if not isinstance(operation_ids, list):
        return []
    return [str(operation_id) for operation_id in operation_ids]


def _rest_api_feature(path: str, frontend_operation_ids: list[str]) -> str:
    if path == "/health":
        return "health"
    if path == "/api-catalog":
        return "api-catalog"
    if path == "/rest-api":
        return "rest"
    if path == "/cli-api":
        return "cli"
    if path == "/surface-contracts":
        return "surface-contracts"
    if path.startswith("/frontend-api"):
        return "frontend-api"
    if path == "/architecture":
        return "architecture"
    if path.startswith("/pdx"):
        return "pdx"
    if path.startswith("/lsp"):
        return "lsp"
    if path.startswith("/desktop"):
        return "desktop"
    if path == "/projects/inspect":
        return "inspections"
    if path in {
        "/projects/templates",
        "/projects/authoring-path",
        "/projects/authoring-plan",
        "/projects/scaffold",
    }:
        return "authoring"
    if path == "/projects/build":
        return "build"
    if path in {"/catalog-api", "/projects/catalog"} or any(operation_id.startswith("catalog.") for operation_id in frontend_operation_ids):
        return "catalog"
    if "/collections" in path or any(operation_id.startswith("collection.") for operation_id in frontend_operation_ids):
        return "collections"
    if "/modules" in path or any(operation_id.startswith("module.") for operation_id in frontend_operation_ids):
        return "modules"
    if path.startswith("/projects"):
        return "projects"
    return "rest"


def _rest_api_inputs(operation: Mapping[str, object], *, required_only: bool) -> str:
    inputs: list[str] = []
    parameters = operation.get("parameters", [])
    if isinstance(parameters, list):
        for parameter in parameters:
            if not isinstance(parameter, Mapping):
                continue
            if required_only and not parameter.get("required"):
                continue
            name = parameter.get("name")
            if not isinstance(name, str):
                continue
            location = parameter.get("in")
            inputs.append(f"{location}:{name}" if isinstance(location, str) and location else name)
    request_body = operation.get("requestBody")
    if isinstance(request_body, Mapping) and (not required_only or request_body.get("required")):
        inputs.append("body")
    return ", ".join(inputs) if inputs else "none"


def _rest_api_responses(operation: Mapping[str, object], *, success: bool) -> str:
    responses = operation.get("responses", {})
    if not isinstance(responses, Mapping):
        return ""
    descriptions: list[str] = []
    for status, response in responses.items():
        status_text = str(status)
        is_success = status_text.startswith("2")
        if success != is_success:
            continue
        if isinstance(response, Mapping):
            description = response.get("description")
            if isinstance(description, str) and description:
                descriptions.append(f"{status_text} {description}")
                continue
        descriptions.append(status_text)
    return "; ".join(descriptions)


def _optional_request_text(request: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        if name in request:
            return _optional_string(request.get(name), name)
    return None


def _optional_ai_chat_sources(
    request: Mapping[str, object],
) -> list[Mapping[str, object]]:
    value = request.get("sources")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Request field 'sources' must be a list.")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError("Request field 'sources' must contain JSON objects.")
    return value


def _request_bool(request: Mapping[str, object], *names: str, default: bool | None = False) -> bool | None:
    for name in names:
        if name in request:
            return _bool_value(request.get(name), name)
    return default


def _optional_request_int(request: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        if name not in request or request.get(name) is None:
            continue
        value = request.get(name)
        if type(value) is not int:
            raise ValueError(f"Request field {name!r} must be an integer.")
        return value
    return None


def _optional_build_target(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("build target must be a JSON object.")
    target = {
        "kind": _required_string(value.get("kind"), "target.kind"),
        "id": _required_string(value.get("id"), "target.id"),
    }
    if family := _optional_string(value.get("family"), "target.family"):
        target["family"] = family
    return target


def _desktop_localization_request(request: Mapping[str, object], *, include_operation: bool) -> tuple[str, dict[str, object]]:
    allowed = {
        "projectId",
        "projectRoot",
        "targetKind",
        "targetId",
        "family",
        "sourceRoot",
        "drafts",
        "limit",
    }
    if include_operation:
        allowed.add("operation")
    _validate_request_fields(request, frozenset(allowed), "Desktop localization request")
    project_id = _request_text(request, "projectId")
    normalized: dict[str, object] = {
        "project_root": _request_text(request, "projectRoot"),
        "target_kind": _request_text(request, "targetKind"),
        "target_id": _request_text(request, "targetId"),
    }
    family = request.get("family")
    if family is not None:
        normalized["family"] = _required_string(family, "family")
    source_root = request.get("sourceRoot")
    if source_root is not None:
        normalized["source_root"] = _required_string(source_root, "sourceRoot")
    drafts = request.get("drafts", [])
    if not isinstance(drafts, list):
        raise ValueError("Desktop localization drafts must be an array.")
    normalized_drafts: list[dict[str, object]] = []
    for index, draft in enumerate(drafts):
        if not isinstance(draft, Mapping):
            raise ValueError(f"Desktop localization draft {index} must be an object.")
        _validate_request_fields(
            draft,
            frozenset({"sourcePath", "text"}),
            f"Desktop localization draft {index}",
        )
        text = draft.get("text")
        if not isinstance(text, str):
            raise ValueError(f"Desktop localization draft {index} text must be a string.")
        normalized_drafts.append({"source_path": _request_text(draft, "sourcePath"), "text": text})
    normalized["drafts"] = normalized_drafts
    limit = request.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("Desktop localization limit must be an integer.")
        normalized["limit"] = limit
    if include_operation:
        operation = request.get("operation")
        if not isinstance(operation, Mapping):
            raise ValueError("Desktop localization operation must be an object.")
        normalized["operation"] = dict(operation)
    return project_id, normalized


def duplicate_module(*, request: Mapping[str, object]) -> dict[str, object]:
    """Plan or atomically create an independent module copy.

    Identity rewriting is the default and is owned by the registered family.
    Pass `identity="preserve"` for an explicit byte-preserving copy. Applying
    a plan requires its exact hash so changed source cannot be published from
    a stale preview.

    Args:
        request (Mapping[str, object]): JSON-like module identity, source-root,
            identity-mode, and guarded plan/apply fields.

    Returns:
        dict[str, object]: JSON-safe SDK duplicate plan or apply payload.

    Raises:
        ValueError: If the request or identity mode is invalid.
        OSError: If safe planning, staging, or rollback cannot complete.
    """

    _validate_request_fields(request, _MODULE_DUPLICATE_REQUEST_FIELDS, "module duplicate request")
    project_root = _required_string(request.get("project_root"), "project_root")
    return open_project(project_root).duplicate_module(
        _required_string(request.get("module_id"), "module_id"),
        _required_string(request.get("object_id"), "object_id"),
        source_root=_optional_string(request.get("source_root"), "source_root"),
        destination_source_root=_optional_string(
            request.get("destination_source_root"),
            "destination_source_root",
        ),
        identity=_optional_string(request.get("identity"), "identity") or "rewrite",
        write=_bool_value(request.get("write", False), "write"),
        plan_hash=_optional_string(request.get("plan_hash"), "plan_hash"),
    )


def clean_module_metadata(*, request: Mapping[str, object]) -> dict[str, object]:
    """Plan or atomically remove redundant path-derived module metadata.

    The request defaults to a dry plan. Applying the plan requires the exact
    current ``plan_hash`` returned by a prior dry plan.

    Args:
        request: JSON-like body containing ``project_root`` and optional
            family, module, source-root, write, and plan-hash selectors.

    Returns:
        JSON-safe SDK metadata cleanup plan or apply payload.

    Raises:
        ProjectManifestError: If the selected project cannot be loaded.
        OSError: If safe source mutation fails.
        ValueError: If the request, scope, or plan hash is invalid.
    """

    _validate_request_fields(
        request,
        _MODULE_METADATA_CLEAN_REQUEST_FIELDS,
        "module metadata clean request",
    )
    project_root = _required_string(request.get("project_root"), "project_root")
    return open_project(project_root).clean_module_metadata(
        family=_optional_string(request.get("family"), "family"),
        module_id=_optional_string(request.get("module_id"), "module_id"),
        source_root=_optional_string(request.get("source_root"), "source_root"),
        write=_bool_value(request.get("write", False), "write"),
        plan_hash=_optional_string(request.get("plan_hash"), "plan_hash"),
    )


def set_module_collection(*, request: Mapping[str, object]) -> dict[str, object]:
    """Plan or atomically change one module's collection membership."""

    _validate_request_fields(
        request,
        _MODULE_COLLECTION_SET_REQUEST_FIELDS,
        "module collection request",
    )
    project_root = _required_string(request.get("project_root"), "project_root")
    return open_project(project_root).set_module_collection(
        _required_string(request.get("module_id"), "module_id"),
        _optional_string(request.get("collection_id"), "collection_id"),
        source_root=_optional_string(request.get("source_root"), "source_root"),
        write=_bool_value(request.get("write", False), "write"),
        plan_hash=_optional_string(request.get("plan_hash"), "plan_hash"),
    )


def set_module_active(*, request: Mapping[str, object]) -> dict[str, object]:
    """Plan or atomically activate or deactivate one source module."""

    _validate_request_fields(
        request,
        _MODULE_ACTIVITY_SET_REQUEST_FIELDS,
        "module activity request",
    )
    project_root = _required_string(request.get("project_root"), "project_root")
    return open_project(project_root).set_module_active(
        _required_string(request.get("module_id"), "module_id"),
        _bool_value(request.get("active"), "active"),
        source_root=_optional_string(request.get("source_root"), "source_root"),
        write=_bool_value(request.get("write", False), "write"),
        plan_hash=_optional_string(request.get("plan_hash"), "plan_hash"),
    )


def read_module_diagram(
    *,
    path: str,
    family: str,
    profile: str | None = None,
) -> dict[str, object]:
    """Project one authoritative module family into a source-backed diagram.

    Args:
        path: Project root or nested project path.
        family: Editable source-backed diagram family. Use `technology` for a
            technology tree, `focus_tree` for a national focus tree, or `mio`
            for a Military Industrial Organization trait tree.
        profile: Optional build profile used for source discovery.

    Returns:
        JSON-safe SDK module diagram payload.

    Raises:
        ProjectManifestError: If the selected project cannot be loaded.
        OSError: If an authoritative source cannot be read.
        ValueError: If the family, profile, or source contract is invalid.
    """

    return open_project(_required_string(path, "path")).module_diagram(
        _required_string(family, "family"),
        profile=_optional_string(profile, "profile"),
    )


def edit_module_diagram(
    *,
    request: Mapping[str, object],
) -> dict[str, object]:
    """Plan or apply bounded source-backed module diagram intents.

    Args:
        request: JSON-like body containing `project_root`, `family`, optional
            `profile`, bounded position/edge intent arrays, and guarded write
            fields.

    Returns:
        JSON-safe SDK module diagram edit plan or apply payload.

    Raises:
        ProjectManifestError: If the selected project cannot be loaded.
        OSError: If safe source mutation fails.
        ValueError: If the request, provider, source contract, or plan hash is
            invalid.
    """

    _validate_request_fields(
        request,
        _MODULE_DIAGRAM_EDIT_REQUEST_FIELDS,
        "module diagram edit request",
    )
    from paradev.sdk._module_diagram_api import (
        module_diagram_node_intents,
    )

    position_intents, edge_intents = module_diagram_intents(
        request.get("position_intents"),
        request.get("edge_intents"),
    )
    node_intents = module_diagram_node_intents(
        request.get("node_intents"),
    )
    return open_project(_required_string(request.get("project_root"), "project_root")).edit_module_diagram(
        _required_string(request.get("family"), "family"),
        position_intents=position_intents,
        edge_intents=edge_intents,
        node_intents=node_intents,
        profile=_optional_string(request.get("profile"), "profile"),
        write=_bool_value(request.get("write", False), "write"),
        plan_hash=_optional_string(request.get("plan_hash"), "plan_hash"),
    )


def build_app():
    """Build the optional FastAPI app.

    Returns:
        FastAPI application.

    Raises:
        RuntimeError: If the optional REST dependencies are not installed.
    """

    try:
        from fastapi import Body, FastAPI, HTTPException, Query, Request
        from fastapi.middleware.cors import CORSMiddleware

        from paradev.surfaces._rest_models import DesktopBuildInterruptRequest
    except ImportError as exc:
        raise RuntimeError("Install ParaDev with the 'rest' extra to build the local REST app.") from exc

    build_registry = DesktopBuildRegistry()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            build_registry.close()

    # FastAPI resolves postponed endpoint annotations through module globals.
    globals()["Request"] = Request
    globals()["DesktopBuildInterruptRequest"] = DesktopBuildInterruptRequest
    app = FastAPI(
        title="ParaDev Local API",
        version="0.1.0",
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type"],
    )

    def frontend_api_bad_request(error: ValueError) -> HTTPException:
        return HTTPException(status_code=400, detail=str(error))

    def api_catalog_bad_request(error: KeyError | ValueError) -> HTTPException:
        message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        return HTTPException(status_code=400, detail=message)

    def native_bad_request(error: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(error))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/architecture")
    def architecture(
        api_table: bool = False,
        symbol: str | None = None,
        index_name: str | None = None,
        key: str | None = None,
    ) -> dict[str, object] | list[str]:
        uses_table_selector = symbol is not None or index_name is not None or key is not None
        if uses_table_selector and not api_table:
            raise HTTPException(
                status_code=400,
                detail="architecture API table selectors require api_table=true",
            )
        if api_table:
            try:
                return get_architecture_api_selection(symbol=symbol, index_name=index_name, key=key)
            except (KeyError, ValueError) as error:
                raise api_catalog_bad_request(error) from error
        return get_architecture_spec().to_dict()

    @app.get("/api-catalog")
    def api_catalog(
        reference_id: str | None = None,
        index_name: str | None = None,
        key: str | None = None,
    ) -> dict[str, object] | list[str]:
        try:
            return get_api_catalog_selection(reference_id=reference_id, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/rest-api")
    def rest_api(symbol: str | None = None, index_name: str | None = None, key: str | None = None) -> dict[str, object] | list[str]:
        try:
            return get_rest_api_selection(symbol=symbol, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/cli-api")
    def cli_api(symbol: str | None = None, index_name: str | None = None, key: str | None = None) -> dict[str, object] | list[str]:
        from paradev.surfaces.cli import get_cli_api_selection

        try:
            return get_cli_api_selection(symbol=symbol, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/surface-contracts")
    def surface_contracts(identifier: str | None = None, status: str | None = None) -> dict[str, object] | list[str]:
        from paradev.surfaces import get_surface_contract_selection

        try:
            return get_surface_contract_selection(identifier=identifier, status=status)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get(SOURCE_PATH)
    def source_text(project_id: str, path: str, project_root: str | None = None) -> dict[str, object]:
        try:
            return read_project_source(project_id=project_id, source_path=path, project_root=project_root)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(SOURCE_FORM_PATH)
    def source_form(project_id: str, request: dict[str, object]) -> dict[str, object] | None:
        try:
            return read_project_source_form(project_id=project_id, request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(LOCALIZATION_WORKSPACE_PATH)
    def localization_workspace(project_id: str, request: dict[str, object]) -> dict[str, object]:
        try:
            return read_project_localization_workspace(project_id=project_id, request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(LOCALIZATION_PLAN_PATH)
    def localization_plan(project_id: str, request: dict[str, object]) -> dict[str, object]:
        try:
            return plan_project_localization_update(project_id=project_id, request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    draft_apply_request_body = _openapi_request_body(DRAFT_APPLY_PATH, "post")

    @app.post(DRAFT_APPLY_PATH)
    def draft_apply(project_id: str, request: dict[str, object]) -> dict[str, object]:
        try:
            return apply_project_draft(project_id=project_id, request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_CREATE_BATCH_PATH)
    def module_create_batch(request: dict[str, object]) -> dict[str, object]:
        try:
            return create_module_batch(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_DUPLICATE_PATH)
    def module_duplicate(request: dict[str, object]) -> dict[str, object]:
        try:
            return duplicate_module(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_METADATA_CLEAN_PATH)
    def module_metadata_clean(request: dict[str, object]) -> dict[str, object]:
        try:
            return clean_module_metadata(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_COLLECTION_SET_PATH)
    def module_collection_set(request: dict[str, object]) -> dict[str, object]:
        try:
            return set_module_collection(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_ACTIVITY_SET_PATH)
    def module_activity_set(request: dict[str, object]) -> dict[str, object]:
        try:
            return set_module_active(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get(MODULE_DIAGRAM_PATH)
    def module_diagram(
        family: str,
        path: str = ".",
        profile: str | None = None,
    ) -> dict[str, object]:
        try:
            return read_module_diagram(
                path=path,
                family=family,
                profile=profile,
            )
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_DIAGRAM_EDIT_PATH)
    def module_diagram_edit(
        request: dict[str, object],
    ) -> dict[str, object]:
        try:
            return edit_module_diagram(request=request)
        except (ProjectManifestError, ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(MODULE_DRAFT_PATH)
    def module_draft(project_id: str, family_id: str, request: dict[str, object]) -> dict[str, object]:
        try:
            return create_module_draft(project_id=project_id, family_id=family_id, request=request)
        except (ProjectManifestError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/frontend-api")
    def frontend_api(
        operation_id: str | None = None,
        group_id: str | None = None,
        form: bool = False,
        index_name: str | None = None,
        key: str | None = None,
    ) -> dict[str, object] | list[str]:
        try:
            return get_frontend_api_selection(
                operation_id=operation_id,
                group_id=group_id,
                form=form,
                index_name=index_name,
                key=key,
            )
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.get("/frontend-api/workspace")
    def frontend_api_workspace() -> dict[str, object]:
        return get_frontend_api_workspace()

    @app.get("/frontend-api/action")
    def frontend_api_action(operation_id: str) -> dict[str, object]:
        try:
            return get_frontend_api_action(operation_id)
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.post("/frontend-api/normalize")
    def frontend_api_normalize(operation_id: str, body: dict[str, object] | None = Body(None)) -> dict[str, object]:
        try:
            return normalize_frontend_api_inputs(operation_id, body or {})
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.post("/frontend-api/rest-request")
    def frontend_api_rest_request(operation_id: str, body: dict[str, object] | None = Body(None)) -> dict[str, object]:
        try:
            return plan_frontend_api_rest_request(operation_id, body or {})
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.post("/frontend-api/options")
    def frontend_api_options(operation_id: str, field_name: str, body: dict[str, object] | None = Body(None)) -> dict[str, object]:
        try:
            return resolve_frontend_api_options(operation_id, field_name, body or {})
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.get("/frontend-api/binding")
    def frontend_api_binding(binding_surface: str, binding_key: str) -> dict[str, object]:
        try:
            return get_frontend_api_binding_lookup(binding_surface, binding_key)
        except ValueError as error:
            raise frontend_api_bad_request(error) from error

    @app.get("/pdx/parse")
    def pdx_parse(path: str, include_dump: bool = False, include_tokens: bool = False) -> dict[str, object]:
        return parse_pdx_file(path, include_dump=include_dump, include_tokens=include_tokens)

    @app.post("/pdx/format")
    def pdx_format(path: str, indent: str = "\t", comments: bool = True, write: bool = False) -> dict[str, object]:
        return format_pdx_file(path, indent=indent, comments=comments, write=write)

    @app.get("/pdx-api")
    def pdx_api(symbol: str | None = None, index_name: str | None = None, key: str | None = None) -> dict[str, object] | list[str]:
        try:
            return get_pdx_api_selection(symbol=symbol, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/lsp-api")
    def lsp_api(symbol: str | None = None, index_name: str | None = None, key: str | None = None) -> dict[str, object] | list[str]:
        try:
            return get_lsp_api_selection(symbol=symbol, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/catalog-api")
    def catalog_api(symbol: str | None = None, index_name: str | None = None, key: str | None = None) -> dict[str, object] | list[str]:
        try:
            return get_catalog_api_selection(symbol=symbol, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            raise api_catalog_bad_request(error) from error

    @app.get("/projects/list")
    def project_list(
        project_paths: list[str] = Query(default=[]),
        search_roots: list[str] = Query(default=[]),
    ) -> dict[str, object]:
        return registered_projects(project_paths=tuple(project_paths), search_roots=tuple(search_roots))

    @app.get("/desktop/state")
    def project_desktop_state(
        project_path: str | None = None,
        project_paths: list[str] = Query(default=[]),
        search_roots: list[str] = Query(default=[]),
        include_browser: bool = True,
    ) -> dict[str, object]:
        try:
            return desktop_state(
                project_path,
                project_paths=tuple(project_paths),
                search_roots=tuple(search_roots),
                include_browser=include_browser,
            )
        except ProjectManifestError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/desktop/builds")
    def desktop_build_start(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return build_registry.start(
                _request_text(request, "projectRoot", "project_root"),
                mode=_optional_request_text(request, "mode"),
                profile=_optional_request_text(request, "profile"),
                strict_metadata=_request_bool(request, "strictMetadata", "strict_metadata", default=None),
                parallelism=_optional_request_int(request, "parallelism"),
                target=_optional_build_target(request.get("target")),
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/builds")
    def desktop_build_runs(project_root: str | None = None) -> dict[str, object]:
        try:
            return build_registry.runs(project_root)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/builds/status")
    def desktop_build_status(
        run_id: str = Query(..., min_length=1, pattern=r".*\S.*"),
    ) -> dict[str, object]:
        return build_registry.status(_required_string(run_id, "run_id"))

    @app.post("/desktop/builds/interrupt")
    def desktop_build_interrupt(
        request: DesktopBuildInterruptRequest,
    ) -> dict[str, object]:
        try:
            return build_registry.interrupt(request.exact_run_id())
        except (OSError, RuntimeError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/open-path")
    def desktop_path_open(request: dict[str, object] = Body(...)) -> dict[str, object]:
        try:
            path = _request_text(request, "path")
            target = _optional_request_text(request, "target")
            command = desktop_open_path(path, target, launch=True)
            return {
                "schema": "paradev.desktop.open-path.v1",
                "status": "started",
                "path": path,
                "target": target,
                "command": command,
            }
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/select-project")
    def desktop_project_select() -> dict[str, object]:
        try:
            selected = desktop_select_project_path()
            return {
                "schema": "paradev.desktop.project-selection.v1",
                "path": selected,
            }
        except (OSError, RuntimeError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/import-project-package")
    def desktop_project_package_import() -> dict[str, object] | None:
        try:
            archive_path = desktop_select_project_package_path()
            if archive_path is None:
                return None
            return dict(desktop_install_project_package(archive_path))
        except (OSError, RuntimeError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/path-status")
    def desktop_path_status_route(path: str) -> dict[str, object]:
        try:
            return desktop_path_status_payload(path)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/run-hoi4")
    def desktop_hoi4_run(
        request: dict[str, object] | None = Body(None),
    ) -> dict[str, object]:
        body = request or {}
        try:
            return desktop_run_hoi4(
                project_root=_optional_request_text(body, "projectRoot", "project_root"),
                game_root=_optional_request_text(body, "gameRoot", "game_root"),
                mode=_optional_request_text(body, "mode"),
                launch=True,
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/hoi4-launch-readiness")
    def desktop_hoi4_readiness(project_root: str) -> dict[str, object]:
        try:
            return desktop_hoi4_launch_readiness(project_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/sources/text")
    def desktop_source_text(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            project_root = _request_text(request, "projectRoot", "project_root")
            source_path = _request_text(request, "sourcePath", "source_path")
            return {
                "schema": "paradev.desktop.text-source.v1",
                "projectRoot": project_root,
                "sourcePath": source_path,
                "text": desktop_read_text_source(project_root, source_path),
            }
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/sources/form-updates/plan")
    def desktop_source_form_updates_plan(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            _validate_request_fields(
                request,
                frozenset({"projectId", "projectRoot", "updates"}),
                "Desktop source-form update-batch request",
            )
            project_id = _request_text(request, "projectId")
            project_root = _request_text(request, "projectRoot")
            updates = request.get("updates")
            if not isinstance(updates, list):
                raise ValueError("Desktop source-form update-batch updates must be an array.")
            normalized_updates: list[dict[str, object]] = []
            for index, update in enumerate(updates):
                if not isinstance(update, Mapping):
                    raise ValueError(f"Desktop source-form update row {index} must be an object.")
                _validate_request_fields(
                    update,
                    frozenset({"sourcePath", "values", "text"}),
                    f"Desktop source-form update row {index}",
                )
                source_path = _request_text(update, "sourcePath")
                values = update.get("values")
                if not isinstance(values, Mapping):
                    raise ValueError(f"Desktop source-form update row {index} values must " "be an object.")
                text = update.get("text")
                if text is not None and not isinstance(text, str):
                    raise ValueError(f"Desktop source-form update row {index} text must be a string.")
                normalized_updates.append(
                    {
                        "source_path": source_path,
                        "values": values,
                        **({"text": text} if text is not None else {}),
                    }
                )
            return plan_project_source_form_updates(
                project_id=project_id,
                request={
                    "project_root": project_root,
                    "updates": normalized_updates,
                },
            )
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/localization/workspace")
    def desktop_localization_workspace(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            project_id, normalized = _desktop_localization_request(request, include_operation=False)
            return read_project_localization_workspace(project_id=project_id, request=normalized)
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/localization/plan")
    def desktop_localization_plan(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            project_id, normalized = _desktop_localization_request(request, include_operation=True)
            return plan_project_localization_update(project_id=project_id, request=normalized)
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/sources/binary")
    def desktop_source_binary(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return desktop_read_binary_source(
                _request_text(request, "projectRoot", "project_root"),
                _request_text(request, "sourcePath", "source_path"),
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/project-browser-cache")
    def desktop_project_browser_cache(project_root: str) -> dict[str, object] | None:
        try:
            return read_project_browser_cache(project_root)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.put("/desktop/project-browser-cache")
    def desktop_project_browser_cache_write(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return desktop_write_browser_cache(
                _request_text(request, "projectRoot", "project_root"),
                request.get("payload") if isinstance(request.get("payload"), Mapping) else {},
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/thumbnail-cache")
    def desktop_thumbnail_cache(project_root: str, cache_key: str) -> dict[str, object] | None:
        try:
            return desktop_read_thumbnail_cache(project_root, cache_key)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.put("/desktop/thumbnail-cache")
    def desktop_thumbnail_cache_write(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return desktop_write_thumbnail_cache(
                _request_text(request, "projectRoot", "project_root"),
                _request_text(request, "cacheKey", "cache_key"),
                request.get("bytes") if isinstance(request.get("bytes"), list) else [],
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/app-config")
    def desktop_app_config() -> object:
        return desktop_read_app_config()

    @app.put("/desktop/app-config")
    def desktop_app_config_write(
        config: dict[str, object] = Body(...),
    ) -> dict[str, str]:
        try:
            desktop_write_app_config(config)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error
        return {"status": "ok"}

    @app.get("/desktop/config-value")
    def desktop_config_value(key: str) -> dict[str, object]:
        try:
            return desktop_read_config_value(key)
        except ValueError as error:
            raise native_bad_request(error) from error

    @app.put("/desktop/config-value")
    def desktop_config_value_write(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return desktop_write_config_value(_request_text(request, "key"), request.get("value"))
        except ValueError as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/dependencies/{dependency_id}")
    def desktop_dependency(dependency_id: str) -> dict[str, object]:
        try:
            return desktop_dependency_status(dependency_id)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/dependencies/{dependency_id}/install")
    def desktop_dependency_install(dependency_id: str) -> dict[str, object]:
        try:
            return desktop_install_dependency(dependency_id)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/llm/test")
    def desktop_llm_test(request: dict[str, object] = Body(...)) -> dict[str, object]:
        try:
            return desktop_test_llm_route(
                _request_text(request, "provider", "provider"),
                _request_text(request, "model", "model"),
                _request_text(request, "gateway", "gateway"),
                key_env=_optional_request_text(request, "keyEnv", "key_env"),
                base_url=_optional_request_text(request, "baseUrl", "base_url"),
                preset=_optional_request_text(request, "preset", "preset"),
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/ai/chat")
    def desktop_ai_chat(request: dict[str, object] = Body(...)) -> dict[str, object]:
        try:
            return desktop_chat(
                _request_text(request, "provider", "provider"),
                _request_text(request, "model", "model"),
                _request_text(request, "gateway", "gateway"),
                _request_text(request, "prompt", "prompt"),
                _optional_request_text(request, "role", "role") or "chat",
                _optional_request_text(request, "projectRoot", "project_root") or "",
                _optional_ai_chat_sources(request),
                key_env=_optional_request_text(request, "keyEnv", "key_env"),
                base_url=_optional_request_text(request, "baseUrl", "base_url"),
                preset=_optional_request_text(request, "preset", "preset"),
            )
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/desktop/ai/profiles")
    def desktop_ai_profiles(project_root: str = "") -> dict[str, object]:
        return desktop_chat_profiles(project_root)

    @app.put("/desktop/ai/profiles/{profile_id}")
    def desktop_ai_profile_write(profile_id: str, request: dict[str, object] = Body(...)) -> dict[str, object]:
        try:
            project_root = (
                _optional_request_text(request, "projectRoot", "project_root") or _optional_request_text(request, "project_root", "project_root") or ""
            )
            if "profile" in request:
                planned_profile = request["profile"]
                if not isinstance(planned_profile, Mapping):
                    raise ValueError("AI chat profile override must be a JSON object.")
                profile = dict(planned_profile)
            else:
                profile = {key: value for key, value in request.items() if key not in {"projectRoot", "project_root"}}
            return desktop_write_chat_profile(profile_id, profile, project_root=project_root)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.delete("/desktop/ai/profiles/{profile_id}")
    def desktop_ai_profile_reset(profile_id: str, project_root: str = "") -> dict[str, object]:
        try:
            return desktop_reset_chat_profile(profile_id, project_root=project_root)
        except (OSError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/modules/draft")
    def desktop_module_draft(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            project = open_project(_request_text(request, "projectRoot", "project_root"))
            return create_module_draft(
                project_id=project.project_id,
                family_id=_request_text(request, "familyId", "family_id"),
                request={
                    "project_root": str(project.root),
                    "template_id": request.get("templateId", request.get("template_id")),
                    "object_id": request.get("objectId", request.get("object_id")),
                    "values": request.get("values"),
                    "write": request.get("write", False),
                    "force": request.get("force", False),
                },
            )
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/drafts/apply")
    def desktop_draft_apply(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            _validate_request_fields(
                request,
                frozenset(
                    {
                        "projectId",
                        "project_id",
                        "projectRoot",
                        "project_root",
                        "sourceEdits",
                        "source_edits",
                        "sourceRemovals",
                        "source_removals",
                        "sourceReplacements",
                        "source_replacements",
                        "moduleRename",
                        "module_rename",
                    }
                ),
                "Desktop draft apply request",
            )
            for camel_name, snake_name in (
                ("projectId", "project_id"),
                ("projectRoot", "project_root"),
                ("sourceEdits", "source_edits"),
                ("sourceRemovals", "source_removals"),
                ("sourceReplacements", "source_replacements"),
                ("moduleRename", "module_rename"),
            ):
                if camel_name in request and snake_name in request:
                    raise ValueError("Desktop draft apply request cannot provide both " f"{camel_name!r} and {snake_name!r}.")
            mutations = normalize_project_draft_mutations(
                source_edits=request.get("sourceEdits", request.get("source_edits")),
                source_removals=request.get("sourceRemovals", request.get("source_removals")),
                source_replacements=request.get("sourceReplacements", request.get("source_replacements")),
                module_rename=request.get("moduleRename", request.get("module_rename")),
                label="desktop draft apply",
            )
            project = open_project(_request_text(request, "projectRoot", "project_root"))
            return apply_project_draft(
                project_id=_optional_request_text(request, "projectId", "project_id") or project.project_id,
                request={
                    "project_root": str(project.root),
                    **mutations,
                },
            )
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.post("/desktop/modules/batch-request")
    def desktop_module_batch_request(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return open_project(_request_text(request, "projectRoot", "project_root")).module_batch_edit_request(
                request.get("edits") if isinstance(request.get("edits"), list) else [],
                create=_request_bool(request, "create", default=False),
                encoding=_optional_request_text(request, "encoding") or "utf-8",
            )
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.patch("/desktop/modules/rename")
    def desktop_module_rename(
        request: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            return open_project(_request_text(request, "projectRoot", "project_root")).rename_module(
                _request_text(request, "moduleId", "module_id"),
                _request_text(request, "objectId", "object_id"),
                source_root=_optional_request_text(request, "sourceRoot", "source_root"),
                title=_optional_request_text(request, "title"),
            )
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/projects/browser")
    def project_browser(
        path: str = ".",
        profile: str | None = None,
        kind: str | None = None,
        family: str | None = None,
        module_id: str | None = None,
        collection_id: str | None = None,
        summary: bool = False,
    ) -> dict[str, object]:
        try:
            project = open_project(path)
            if summary:
                if module_id is not None or collection_id is not None:
                    raise ValueError("summary cannot be combined with module_id or collection_id.")
                return project.browser_summary(profile=profile, kind=kind, family=family)
            return project.browser(
                profile=profile,
                kind=kind,
                family=family,
                module_id=module_id,
                collection_id=collection_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/lsp/diagnostics")
    def lsp_diagnostics(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP diagnostics request body must include string field 'text'.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP diagnostics request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP diagnostics request body field 'path' must be a string.")
        return diagnose_pdx_lsp_text(text, uri=uri, path=path)

    @app.post("/lsp/symbols")
    def lsp_symbols(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP symbols request body must include string field 'text'.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP symbols request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP symbols request body field 'path' must be a string.")
        return document_symbols_pdx_lsp_text(text, uri=uri, path=path)

    @app.post("/lsp/hover")
    def lsp_hover(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP hover request body must include string field 'text'.")
        line = body.get("line")
        if type(line) is not int or line < 0:
            raise ValueError("LSP hover request body field 'line' must be a non-negative integer.")
        character = body.get("character")
        if type(character) is not int or character < 0:
            raise ValueError("LSP hover request body field 'character' must be a non-negative integer.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP hover request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP hover request body field 'path' must be a string.")
        return hover_pdx_lsp_text(text, line, character, uri=uri, path=path)

    @app.post("/lsp/formatting")
    def lsp_formatting(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP formatting request body must include string field 'text'.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP formatting request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP formatting request body field 'path' must be a string.")
        indent = body.get("indent", "\t")
        if not isinstance(indent, str):
            raise ValueError("LSP formatting request body field 'indent' must be a string.")
        comments = body.get("comments", True)
        if not isinstance(comments, bool):
            raise ValueError("LSP formatting request body field 'comments' must be a boolean.")
        return format_pdx_lsp_text(text, uri=uri, path=path, indent=indent, comments=comments)

    @app.post("/lsp/completion")
    def lsp_completion(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP completion request body must include string field 'text'.")
        line = body.get("line")
        if type(line) is not int or line < 0:
            raise ValueError("LSP completion request body field 'line' must be a non-negative integer.")
        character = body.get("character")
        if type(character) is not int or character < 0:
            raise ValueError("LSP completion request body field 'character' must be a non-negative integer.")
        offset = body.get("offset")
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError("LSP completion request body field 'offset' must be a non-negative integer when provided.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP completion request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP completion request body field 'path' must be a string.")
        project_path = body.get("project_path")
        if project_path is not None and not isinstance(project_path, str):
            raise ValueError("LSP completion request body field 'project_path' must be a string.")
        database = body.get("database")
        if database is not None and not isinstance(database, str):
            raise ValueError("LSP completion request body field 'database' must be a string.")
        game_root = body.get("game_root")
        if game_root is not None and not isinstance(game_root, str):
            raise ValueError("LSP completion request body field 'game_root' must be a string.")
        limit = body.get("limit", 100)
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("LSP completion request body field 'limit' must be a positive integer.")
        project = open_project(project_path) if project_path else None
        return complete_pdx_lsp_text(
            text,
            line,
            character,
            uri=uri,
            path=path,
            project=project,
            database=database,
            game_root=game_root,
            limit=limit,
            offset=offset,
        )

    @app.post("/lsp/semantic-tokens")
    def lsp_semantic_tokens(body: dict[str, object] = Body(...)) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("LSP semantic tokens request body must include string field 'text'.")
        uri = body.get("uri")
        if uri is not None and not isinstance(uri, str):
            raise ValueError("LSP semantic tokens request body field 'uri' must be a string.")
        path = body.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError("LSP semantic tokens request body field 'path' must be a string.")
        return semantic_tokens_pdx_lsp_text(text, uri=uri, path=path)

    @app.get("/lsp/keywords")
    def lsp_keywords(game_root: str | None = None) -> dict[str, object]:
        return hoi4_keyword_dataset(game_root=game_root)

    @app.get("/projects/inspections")
    def project_inspections(path: str = ".") -> dict[str, object]:
        return open_project(path).inspect("inspections")

    @app.get("/projects/templates")
    def project_templates(
        path: str = ".",
        template_id: str | None = None,
        family: str | None = None,
        kind: Literal["module", "collection"] | None = None,
        source: str | None = None,
        authoring_ready: bool | None = None,
        diagnostic_code: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).templates(
            template_id=template_id,
            family=family,
            kind=kind,
            source=source,
            authoring_ready=authoring_ready,
            diagnostic_code=diagnostic_code,
        )

    @app.get("/projects/authoring-path")
    def project_authoring_path(
        kind: str,
        family: str,
        target_id: str,
        path: str = ".",
        source_root: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).authoring_path(kind, family, target_id, source_root=source_root)

    @app.get("/projects/authoring-plan")
    def project_authoring_plan(
        kind: str,
        family: str,
        target_id: str,
        path: str = ".",
        source_root: str | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).authoring_plan(kind, family, target_id, source_root=source_root, profile=profile)

    @app.post("/projects/scaffold")
    def project_scaffold(
        template_id: str,
        object_id: str,
        path: str = ".",
        source_root: str | None = None,
        write: bool = False,
        plan_hash: str | None = None,
        force: bool = False,
        body: dict[str, object] | None = Body(default=None),
    ) -> dict[str, object]:
        values = body.get("values") if isinstance(body, dict) else None
        return open_project(path).scaffold_module(
            template_id,
            object_id,
            source_root=source_root,
            values=values if isinstance(values, dict) else None,
            write=write,
            force=force,
        )

    @app.post(
        "/projects/build",
        responses={400: {"description": "Invalid project path or build options."}},
    )
    def project_build(
        path: str = ".",
        profile: str | None = None,
        emit_artifacts: bool = False,
        emit_manifests: bool = False,
        strict_metadata: bool | None = None,
        family: str | None = None,
        module_id: str | None = None,
        collection_id: str | None = None,
        full_rebuild: bool = False,
        sync_launcher_descriptor: bool = True,
        parallelism: int | None = Query(default=None, ge=1),
    ) -> dict[str, object]:
        try:
            project = open_project(path)
            return project.build(
                profile=profile,
                emit_artifacts=emit_artifacts,
                emit_manifests=emit_manifests,
                strict_metadata=strict_metadata,
                family=family,
                module_id=module_id,
                collection_id=collection_id,
                full_rebuild=full_rebuild,
                sync_launcher_descriptor=sync_launcher_descriptor,
                parallelism=parallelism,
            ).to_dict()
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    @app.get("/projects/catalog")
    def project_catalog_status(path: str = ".") -> dict[str, object]:
        return open_project(path).catalog_status()

    @app.post("/projects/catalog")
    def project_catalog_write(path: str = ".", profile: str | None = None, database: str | None = None) -> dict[str, object]:
        from paradev.hb import catalog_write

        try:
            return catalog_write(open_project(path), profile=profile, database=database)
        except FileExistsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put("/projects/catalog")
    def project_catalog_refresh(path: str = ".", profile: str | None = None, database: str | None = None) -> dict[str, object]:
        from paradev.hb import catalog_refresh

        return catalog_refresh(open_project(path), profile=profile, database=database)

    @app.get("/projects")
    def project_open(path: str = ".", game: str | None = None, title: str | None = None) -> dict[str, object]:
        try:
            return open_project(path, game=game, title=title).to_view()
        except (ProjectManifestError, OSError, ValueError) as error:
            # A caller-supplied path that does not hold a project is a bad request, not a
            # server fault. Answering 500 discards the message, which is the only thing a
            # frontend can show the user when they typed or picked the wrong folder.
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/projects")
    def project_create(
        path: str,
        project_id: str | None = None,
        title: str | None = None,
        game: str = "hoi4",
        force: bool = False,
    ) -> dict[str, object]:
        project = create_project(path, project_id=project_id, title=title, game=game, force=force)
        return project_create_payload(project)

    @app.get("/projects/find")
    def project_find(path: str = ".") -> dict[str, object]:
        return Project.find(path)

    @app.patch("/projects/rename")
    def project_rename(title: str, path: str = ".") -> dict[str, object]:
        project = open_project(path)
        renamed = project.rename(title)
        return {
            "schema": "paradev.project.rename.v1",
            "previous_title": project.title,
            "project": renamed.to_view(),
        }

    @app.patch("/projects/language")
    def project_preferred_language(
        preferred_language: str,
        path: str = ".",
        write: bool = False,
        plan_hash: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).set_preferred_language(
            preferred_language,
            write=write,
            plan_hash=plan_hash,
        )

    @app.patch("/projects/modules/rename")
    def module_rename(
        module_id: str,
        object_id: str,
        path: str = ".",
        source_root: str | None = None,
        title: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).rename_module(
            module_id,
            object_id,
            source_root=source_root,
            title=title,
        )

    @app.delete("/projects/modules/remove")
    def module_remove(
        module_id: str,
        path: str = ".",
        source_root: str | None = None,
        write: bool = False,
    ) -> dict[str, object]:
        return open_project(path).remove_module(module_id, source_root=source_root, write=write)

    @app.get("/projects/modules/file")
    def module_file(
        module_id: str,
        relative_path: str,
        path: str = ".",
        source_root: str | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, object]:
        try:
            return open_project(path).read_module_file(module_id, relative_path, source_root=source_root, encoding=encoding)
        except (ProjectManifestError, OSError, ValueError) as error:
            # An unknown module or a missing file is a bad request, not a server fault.
            # A 500 discards "Unknown module: <id>.", which is the only thing the GUI has
            # to show when it asks for something that is no longer there.
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.patch("/projects/modules/file")
    def module_edit(
        module_id: str,
        relative_path: str,
        path: str = ".",
        source_root: str | None = None,
        create: bool = False,
        encoding: str = "utf-8",
        body: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("Module file request body must include string field 'text'.")
        return open_project(path).write_module_file(
            module_id,
            relative_path,
            text,
            source_root=source_root,
            create=create,
            encoding=encoding,
        )

    @app.post("/projects/collections/scaffold")
    def collection_scaffold(
        template_id: str,
        collection_id: str,
        path: str = ".",
        source_root: str | None = None,
        write: bool = False,
        force: bool = False,
        plan_hash: str | None = None,
        body: dict[str, object] | None = Body(default=None),
    ) -> dict[str, object]:
        values = body.get("values") if isinstance(body, dict) else None
        if values is not None and not isinstance(values, dict):
            raise ValueError("Collection scaffold request body field 'values' must be an object.")
        return open_project(path).scaffold_collection(
            template_id,
            collection_id,
            source_root=source_root,
            values=values,
            write=write,
            force=force,
            plan_hash=plan_hash,
        )

    @app.post("/projects/collections")
    def collection_create(
        family: str,
        collection_id: str,
        path: str = ".",
        source_root: str | None = None,
        write: bool = False,
        force: bool = False,
        body: dict[str, object] | None = Body(default=None),
    ) -> dict[str, object]:
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("Collection create request body field 'metadata' must be an object.")
        return open_project(path).create_collection(
            family,
            collection_id,
            source_root=source_root,
            metadata=metadata,
            write=write,
            force=force,
        )

    @app.delete("/projects/collections")
    def collection_remove(
        collection_id: str,
        path: str = ".",
        family: str | None = None,
        source_root: str | None = None,
        write: bool = False,
        plan_hash: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).remove_collection(
            collection_id,
            family=family,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )

    @app.patch("/projects/collections/rename")
    def collection_rename(
        collection_id: str,
        target_id: str,
        path: str = ".",
        family: str | None = None,
        source_root: str | None = None,
    ) -> dict[str, object]:
        return open_project(path).rename_collection(
            collection_id,
            target_id,
            family=family,
            source_root=source_root,
        )

    @app.get("/projects/collections/file")
    def collection_file(
        collection_id: str,
        relative_path: str,
        path: str = ".",
        family: str | None = None,
        source_root: str | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, object]:
        return open_project(path).read_collection_file(
            collection_id,
            relative_path,
            family=family,
            source_root=source_root,
            encoding=encoding,
        )

    @app.patch("/projects/collections/file")
    def collection_edit(
        collection_id: str,
        relative_path: str,
        path: str = ".",
        family: str | None = None,
        source_root: str | None = None,
        create: bool = False,
        encoding: str = "utf-8",
        body: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ValueError("Collection file request body must include string field 'text'.")
        return open_project(path).write_collection_file(
            collection_id,
            relative_path,
            text,
            family=family,
            source_root=source_root,
            create=create,
            encoding=encoding,
        )

    @app.get("/projects/inspect")
    def project_inspect(
        request: Request,
        path: str = ".",
        kind: str = "summary",
    ) -> dict[str, object]:
        try:
            normalized_kind = kind.strip().lower().replace("_", "-")
            filters = {key: value for key, value in request.query_params.items() if key not in {"path", "kind"}}
            if normalized_kind == "catalog-query":
                filters = normalize_catalog_query_filters(filters)
            return open_project(path).inspect(normalized_kind, **filters)
        except (OSError, ProjectManifestError, ValueError) as error:
            raise native_bad_request(error) from error

    default_openapi = app.openapi

    def canonical_openapi() -> dict[str, object]:
        schema = default_openapi()
        paths = schema.get("paths")
        if not isinstance(paths, dict):
            raise RuntimeError("Live OpenAPI paths must be an object.")
        path_item = paths.get(DRAFT_APPLY_PATH)
        if not isinstance(path_item, dict):
            raise RuntimeError(f"Live OpenAPI path {DRAFT_APPLY_PATH!r} is missing.")
        operation = path_item.get("post")
        if not isinstance(operation, dict):
            raise RuntimeError(f"Live OpenAPI operation POST {DRAFT_APPLY_PATH} is missing.")
        operation["requestBody"] = draft_apply_request_body
        return cast(dict[str, object], schema)

    app.openapi = canonical_openapi
    return app
