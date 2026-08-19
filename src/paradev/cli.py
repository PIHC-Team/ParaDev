"""Typer and Rich command line entry point for ParaDev."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

import typer
from heavenbase.utils import dumps_json, dumps_yaml, load_json, loads_json
from rich.console import Console

from ._catalog import (
    CATALOG_QUERY_DEFAULT_INCLUDE_DATA,
    CATALOG_QUERY_DEFAULT_LIMIT,
    CATALOG_QUERY_DEFAULT_OFFSET,
    CATALOG_QUERY_MAX_LIMIT,
    normalize_catalog_query_filters,
)
from .config import (
    CM_PARADEV,
    config_get,
    config_history,
    config_list,
    config_scopes,
    config_set,
    config_unset,
)
from .lsp import serve_pdx_lsp_stdio
from .package_api import render_package_api_reference_markdown
from .pdx import render_pdx_core_api_reference_markdown
from .sdk import (
    Project,
    ProjectCreateError,
    ProjectManifestError,
    complete_pdx_lsp_text,
    create_project,
    diagnose_pdx_lsp_text,
    desktop_state,
    document_symbols_pdx_lsp_text,
    format_pdx_file,
    format_pdx_lsp_text,
    get_architecture_api_selection,
    get_architecture_spec,
    get_frontend_api_action,
    get_frontend_api_binding_lookup,
    get_frontend_api_selection,
    get_frontend_api_workspace,
    get_project_inspection_selection,
    hoi4_keyword_dataset,
    hover_pdx_lsp_text,
    normalize_frontend_api_inputs,
    open_project,
    parse_pdx_file,
    plan_frontend_api_rest_request,
    project_create_payload,
    registered_projects,
    render_architecture_api_reference_markdown,
    render_frontend_api_reference_markdown,
    render_frontend_api_sdk_cli_markdown,
    render_frontend_api_typescript,
    render_lsp_api_reference_markdown,
    render_pdx_api_reference_markdown,
    render_project_api_reference_markdown,
    render_project_inspection_reference_markdown,
    render_sdk_api_reference_markdown,
    resolve_frontend_api_options,
    semantic_tokens_pdx_lsp_text,
)
from .sdk._module_diagram_api import module_diagram_intents
from .surfaces import (
    get_api_catalog_selection,
    get_api_catalog_table,
    get_surface_contract_selection,
    render_api_catalog_reference_markdown,
    render_surface_contract_reference_markdown,
    render_surfaces_api_reference_markdown,
)
from .version import __version__

console = Console()
app = typer.Typer(help="ParaDev project CLI.", invoke_without_command=True, no_args_is_help=True)
config_app = typer.Typer(help="Read and write project config.", no_args_is_help=True)
hb_app = typer.Typer(help="Preview and manage HeavenBase catalog integration.", no_args_is_help=True)
lsp_app = typer.Typer(help="Return LSP-shaped PDX editor payloads.", no_args_is_help=True)
mcp_app = typer.Typer(help="Run ParaDev MCP agent surfaces.", no_args_is_help=True)
app.add_typer(config_app, name="config", help="Read and write project config.")
app.add_typer(config_app, name="cfg", help="Alias for config.")
app.add_typer(hb_app, name="hb", help="Preview and manage HeavenBase catalog integration.")
app.add_typer(lsp_app, name="lsp", help="Return LSP-shaped PDX editor payloads.")
app.add_typer(mcp_app, name="mcp", help="Run ParaDev MCP agent surfaces.")


def _json_enabled(ctx: typer.Context, local: bool = False) -> bool:
    """Return whether structured output should use JSON."""

    if _json_requested(ctx, local):
        return True
    return str(CM_PARADEV.get("paradev.cli.output", default="yaml")).strip() == "json"


def _json_requested(ctx: typer.Context, local: bool = False) -> bool:
    """Return whether JSON was explicitly requested for this command."""

    return local or bool((ctx.obj or {}).get("json"))


def _emit(value: Any, *, as_json: bool = False) -> None:
    """Print one CLI result."""

    if as_json and value is not None:
        print(dumps_json(value, indent=2).rstrip())
        return
    if isinstance(value, (dict, list, tuple)):
        print(dumps_yaml(value).rstrip())
        return
    if value is not None:
        console.print(value)


def _build_progress_jsonl_writer(
    path: str | None,
) -> Callable[[dict[str, object]], None] | None:
    """Return a callback that appends build progress JSON lines."""

    if not path:
        return None
    progress_path = Path(path)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text("", encoding="utf-8")

    def write_event(event: dict[str, object]) -> None:
        with progress_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n")

    return write_event


def _emit_standard_api_reference(
    ctx: typer.Context,
    *,
    command_key: str,
    markdown: bool,
    render_markdown: Callable[[], str],
    symbol: str | None,
    index_name: str | None,
    key: str | None,
    json_output: bool,
) -> None:
    """Emit one standard generated API reference command."""

    index_lookup = index_name is not None or key is not None
    if markdown and (symbol is not None or index_lookup):
        raise typer.BadParameter("--markdown cannot be combined with selectors.")
    if markdown and _json_requested(ctx, json_output):
        raise typer.BadParameter("--markdown cannot be combined with --json.")
    if symbol is not None and index_lookup:
        raise typer.BadParameter("Pass only one API reference selector: --symbol or --index/--key.")
    if index_lookup and (index_name is None or key is None):
        raise typer.BadParameter("--index requires --key, and --key requires --index.")
    try:
        if markdown:
            print(render_markdown().rstrip())
            return
        selector = _standard_api_reference_selector(command_key)
        payload = selector(symbol=symbol, index_name=index_name, key=key)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        message = str(error.args[0]) if error.args else str(error)
        raise typer.BadParameter(message) from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


def _standard_api_reference_selector(command_key: str) -> Callable[..., object]:
    """Resolve a standard API reference selector helper by CLI command."""

    for row in get_api_catalog_table()["rows"]:
        if row["cli_command"] != command_key:
            continue
        selector_helper = row["selector_helper"]
        if not selector_helper:
            raise KeyError(f"API reference command {command_key!r} has no selector helper.")
        module = import_module(str(row["owner_module"]))
        selector = getattr(module, str(selector_helper))
        if not callable(selector):
            raise TypeError(f"API reference selector {selector_helper!r} is not callable.")
        return selector
    raise KeyError(f"unknown API reference command {command_key!r}")


@app.callback()
def _main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
    version: bool = typer.Option(False, "--version", help="Show the ParaDev version and exit.", is_eager=True),
) -> None:
    """Configure global CLI options."""

    if version:
        console.print(f"paradev {__version__}")
        raise typer.Exit()
    ctx.obj = {"json": json_output}


@app.command()
def setup(
    reset: bool = typer.Option(False, "--reset", help="Reset stored project config before setup."),
) -> None:
    """Initialize the project config store."""

    CM_PARADEV.setup(reset=reset)
    console.print(f"setup {CM_PARADEV.base_scope}")


@app.command(
    "dashboard",
    context_settings={
        "allow_extra_args": True,
        "help_option_names": [],
        "ignore_unknown_options": True,
    },
)
def _dashboard(ctx: typer.Context) -> None:
    """Launch or install the ParaDev desktop dashboard."""

    from .gui import main as launch_dashboard

    exit_code = launch_dashboard(ctx.args, prog="paradev dashboard")
    if exit_code:
        raise typer.Exit(exit_code)


@app.command("init")
def init_cmd(
    scope: str | None = typer.Argument(None, help="Optional config scope."),
    reset: bool = typer.Option(False, "--reset", help="Reset the scope before initializing it."),
) -> None:
    """Initialize one config scope."""

    created = CM_PARADEV.init(scope=scope, reset=reset)
    console.print(f"{'initialized' if created else 'exists'} {scope or CM_PARADEV.scope}")


@app.command()
def pj(
    parts: list[str] = typer.Argument(None, help="Path parts to join."),
    abs_path: bool = typer.Option(False, "--abs", help="Return an absolute path."),
) -> None:
    """Join path parts using the project config path aliases."""

    console.print(CM_PARADEV.pj(*parts, abs=abs_path))


@app.command()
def architecture(
    ctx: typer.Context,
    api_table: bool = typer.Option(False, "--api-table", help="Return the architecture API standard table."),
    symbol: str | None = typer.Option(
        None,
        "--symbol",
        help="Return one architecture API table row by symbol. Requires --api-table.",
    ),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one architecture API table index. Requires --api-table.",
    ),
    key: str | None = typer.Option(None, "--key", help="Architecture API table index key used with --index."),
    api_table_markdown: bool = typer.Option(
        False,
        "--api-table-markdown",
        help="Render the architecture API reference as Markdown.",
    ),
    surface_contracts: bool = typer.Option(False, "--surface-contracts", help="Return the static surface contract summary."),
    surface_contract: str | None = typer.Option(None, "--surface-contract", help="Return one static surface contract payload."),
    surface_contracts_markdown: bool = typer.Option(
        False,
        "--surface-contracts-markdown",
        help="Render the static surface contract reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned ParaDev architecture graph and surface tables."""

    api_table_index_lookup = index_name is not None or key is not None
    api_table_selector = symbol is not None or api_table_index_lookup
    if api_table_selector and not api_table:
        raise typer.BadParameter("--symbol and --index/--key require --api-table.")
    if symbol is not None and api_table_index_lookup:
        raise typer.BadParameter("Pass only one architecture API table selector: --symbol or --index/--key.")
    if api_table_index_lookup and (index_name is None or key is None):
        raise typer.BadParameter("--index requires --key, and --key requires --index.")
    if api_table_markdown and (
        api_table or api_table_selector or surface_contracts or surface_contract or surface_contracts_markdown or _json_requested(ctx, json_output)
    ):
        raise typer.BadParameter("--api-table-markdown cannot be combined with selectors, --surface-contracts-markdown, or --json.")
    if surface_contracts_markdown and (api_table or api_table_markdown or surface_contracts or surface_contract or _json_requested(ctx, json_output)):
        raise typer.BadParameter("--surface-contracts-markdown cannot be combined with --surface-contracts, --surface-contract, or --json.")
    if sum(bool(selector) for selector in (api_table, surface_contracts, surface_contract)) > 1:
        raise typer.BadParameter("Pass only one architecture selector: --api-table, --surface-contracts, or --surface-contract.")
    try:
        if api_table_markdown:
            print(render_architecture_api_reference_markdown().rstrip())
            return
        if surface_contracts_markdown:
            print(render_surface_contract_reference_markdown().rstrip())
            return
        if api_table:
            payload = get_architecture_api_selection(symbol=symbol, index_name=index_name, key=key)
        elif surface_contract:
            payload = get_surface_contract_selection(identifier=surface_contract)
        elif surface_contracts:
            payload = get_surface_contract_selection()
        else:
            payload = get_architecture_spec().to_dict()
    except (KeyError, ValueError) as error:
        message = str(error.args[0]) if error.args else str(error)
        raise typer.BadParameter(message) from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("frontend-api")
def frontend_api_cmd(
    ctx: typer.Context,
    operation_id: str | None = typer.Option(
        None,
        "--operation",
        "--operation-id",
        help="Return one frontend API operation row.",
    ),
    group_id: str | None = typer.Option(None, "--group", "--group-id", help="Return one frontend API group slice."),
    form: bool = typer.Option(False, "--form", help="Return the derived form contract for one operation."),
    action: bool = typer.Option(
        False,
        "--action",
        help="Return one operation's workspace action, form, bindings, and option-source summary.",
    ),
    option_field: str | None = typer.Option(None, "--option-field", help="Resolve dynamic options for one operation field."),
    binding_surface: str | None = typer.Option(None, "--binding-surface", help="Return operation ids for one surface call key."),
    binding_key: str | None = typer.Option(None, "--binding-key", help="Surface call key used with --binding-surface."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return operation ids for one frontend API index.",
    ),
    key: str | None = typer.Option(None, "--key", help="Frontend API index key used with --index."),
    values_json: str | None = typer.Option(
        None,
        "--values-json",
        "--values",
        help="Normalize one JSON object of frontend form values for the selected operation.",
    ),
    rest_request: bool = typer.Option(False, "--rest-request", help="Plan the REST request for --values-json values."),
    workspace: bool = typer.Option(
        False,
        "--workspace",
        help="Return the SDK-owned frontend workspace action projection.",
    ),
    markdown: bool = typer.Option(False, "--markdown", help="Render the full frontend API reference as Markdown."),
    sdk_cli_markdown: bool = typer.Option(
        False,
        "--sdk-cli-markdown",
        help="Render the generated Python SDK and CLI reference as Markdown.",
    ),
    typescript: bool = typer.Option(
        False,
        "--typescript",
        help="Render the generated frontend API TypeScript contract.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned frontend API contract."""

    binding_lookup = binding_surface is not None or binding_key is not None
    index_lookup = index_name is not None or key is not None
    if workspace and (
        operation_id
        or group_id
        or form
        or action
        or option_field
        or binding_lookup
        or index_lookup
        or values_json is not None
        or rest_request
        or markdown
        or sdk_cli_markdown
        or typescript
    ):
        raise typer.BadParameter("--workspace cannot be combined with selectors, form, values, rest-request, markdown, sdk-cli-markdown, or typescript.")
    if markdown and (
        operation_id
        or group_id
        or form
        or action
        or option_field
        or binding_lookup
        or index_lookup
        or values_json is not None
        or rest_request
        or workspace
        or sdk_cli_markdown
        or typescript
        or _json_requested(ctx, json_output)
    ):
        raise typer.BadParameter("--markdown cannot be combined with selectors, form, values, rest-request, sdk-cli-markdown, typescript, or --json.")
    if sdk_cli_markdown and (
        operation_id
        or group_id
        or form
        or action
        or option_field
        or binding_lookup
        or index_lookup
        or values_json is not None
        or rest_request
        or workspace
        or markdown
        or typescript
        or _json_requested(ctx, json_output)
    ):
        raise typer.BadParameter(
            "--sdk-cli-markdown cannot be combined with selectors, form, values, rest-request, markdown, typescript, workspace, or --json."
        )
    if typescript and (
        operation_id
        or group_id
        or form
        or action
        or option_field
        or binding_lookup
        or index_lookup
        or values_json is not None
        or rest_request
        or workspace
        or markdown
        or sdk_cli_markdown
        or _json_requested(ctx, json_output)
    ):
        raise typer.BadParameter(
            "--typescript cannot be combined with selectors, form, values, rest-request, markdown, sdk-cli-markdown, workspace, or --json."
        )
    if rest_request and (not operation_id or values_json is None or group_id or form):
        raise typer.BadParameter("--rest-request requires --operation and --values-json and cannot be combined with --group or --form.")
    if action and (not operation_id or group_id or form or option_field or values_json is not None or rest_request):
        raise typer.BadParameter("--action requires --operation and cannot be combined with --group, --form, --option-field, --values-json, or --rest-request.")
    if option_field and (not operation_id or group_id or form or rest_request):
        raise typer.BadParameter("--option-field requires --operation and cannot be combined with --group, --form, or --rest-request.")
    if binding_lookup:
        if not binding_surface or not binding_key:
            raise typer.BadParameter("--binding-surface requires --binding-key, and --binding-key requires --binding-surface.")
        if operation_id or group_id or form or action or option_field or values_json is not None or rest_request:
            raise typer.BadParameter(
                "--binding-surface cannot be combined with --operation, --group, --form, --action, --option-field, --values-json, or --rest-request."
            )
    if index_lookup:
        if not index_name or not key:
            raise typer.BadParameter("--index requires --key, and --key requires --index.")
        if operation_id or group_id or form or action or option_field or binding_lookup or values_json is not None or rest_request:
            raise typer.BadParameter(
                "--index cannot be combined with --operation, --group, --form, --action, --option-field, --binding-surface, --values-json, or --rest-request."
            )
    if values_json is not None and (not operation_id or group_id or form or action):
        raise typer.BadParameter("--values-json requires --operation and cannot be combined with --group or --form.")
    if form and (not operation_id or group_id):
        raise typer.BadParameter("--form requires --operation and cannot be combined with --group.")
    if not form and operation_id and group_id:
        raise typer.BadParameter("Pass only one frontend API selector: --operation or --group.")
    try:
        if markdown:
            print(render_frontend_api_reference_markdown().rstrip())
            return
        if sdk_cli_markdown:
            print(render_frontend_api_sdk_cli_markdown().rstrip())
            return
        if typescript:
            print(render_frontend_api_typescript().rstrip())
            return
        if workspace:
            payload = get_frontend_api_workspace()
        elif binding_surface and binding_key:
            payload = get_frontend_api_binding_lookup(binding_surface, binding_key)
        elif index_name and key:
            payload = get_frontend_api_selection(index_name=index_name, key=key)
        elif action and operation_id:
            payload = get_frontend_api_action(operation_id)
        elif rest_request and values_json is not None and operation_id:
            values = loads_json(values_json)
            if not isinstance(values, dict):
                raise typer.BadParameter("--values-json must be a JSON object.")
            payload = plan_frontend_api_rest_request(operation_id, values)
        elif option_field and operation_id:
            values = loads_json(values_json) if values_json is not None else {}
            if not isinstance(values, dict):
                raise typer.BadParameter("--values-json must be a JSON object.")
            payload = resolve_frontend_api_options(operation_id, option_field, values)
        elif values_json is not None and operation_id:
            values = loads_json(values_json)
            if not isinstance(values, dict):
                raise typer.BadParameter("--values-json must be a JSON object.")
            payload = normalize_frontend_api_inputs(operation_id, values)
        elif form or operation_id or group_id:
            payload = get_frontend_api_selection(operation_id=operation_id, group_id=group_id, form=form)
        else:
            payload = get_frontend_api_selection()
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("sdk-api")
def sdk_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the SDK facade API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public Python SDK facade API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="sdk-api",
        markdown=markdown,
        render_markdown=render_sdk_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("api-catalog")
def api_catalog_cmd(
    ctx: typer.Context,
    reference_id: str | None = typer.Option(
        None,
        "--reference",
        "--reference-id",
        help="Return one aggregate API catalog row.",
    ),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return reference ids for one catalog index.",
    ),
    key: str | None = typer.Option(None, "--key", help="Catalog index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the aggregate API catalog reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the aggregate catalog of generated API references."""

    index_lookup = index_name is not None or key is not None
    if markdown and (reference_id is not None or index_lookup):
        raise typer.BadParameter("--markdown cannot be combined with selectors.")
    if markdown and _json_requested(ctx, json_output):
        raise typer.BadParameter("--markdown cannot be combined with --json.")
    if reference_id is not None and index_lookup:
        raise typer.BadParameter("Pass only one api-catalog selector: --reference or --index/--key.")
    if index_lookup and (index_name is None or key is None):
        raise typer.BadParameter("--index requires --key, and --key requires --index.")
    try:
        if markdown:
            print(render_api_catalog_reference_markdown().rstrip())
            return
        payload = get_api_catalog_selection(reference_id=reference_id, index_name=index_name, key=key)
    except (KeyError, ValueError) as error:
        message = str(error.args[0]) if error.args else str(error)
        raise typer.BadParameter(message) from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("package-api")
def package_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the root package API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public root package facade API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="package-api",
        markdown=markdown,
        render_markdown=render_package_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("config-api")
def config_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the config facade API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public config module facade API table."""

    from paradev.config import render_config_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="config-api",
        markdown=markdown,
        render_markdown=render_config_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("gui-api")
def gui_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the GUI launcher API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public GUI launcher facade API table."""

    from paradev.gui import render_gui_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="gui-api",
        markdown=markdown,
        render_markdown=render_gui_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("desktop-api")
def desktop_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the desktop facade API reference as Markdown."),
    typescript: bool = typer.Option(
        False,
        "--typescript",
        help="Render the generated desktop GUI TypeScript contract.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public desktop package facade API table."""

    from paradev.desktop import (
        render_desktop_api_reference_markdown,
        render_desktop_typescript,
    )

    index_lookup = index_name is not None or key is not None
    if typescript and (symbol is not None or index_lookup or markdown or _json_requested(ctx, json_output)):
        raise typer.BadParameter("--typescript cannot be combined with selectors, markdown, or --json.")
    if typescript:
        print(render_desktop_typescript().rstrip())
        return

    _emit_standard_api_reference(
        ctx,
        command_key="desktop-api",
        markdown=markdown,
        render_markdown=render_desktop_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("games-api")
def games_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the games facade API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public games package facade API table."""

    from paradev.games import render_games_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="games-api",
        markdown=markdown,
        render_markdown=render_games_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("surfaces-api")
def surfaces_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the surfaces facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public surfaces facade API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="surfaces-api",
        markdown=markdown,
        render_markdown=render_surfaces_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("project-api")
def project_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the Project object API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public Project object API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="project-api",
        markdown=markdown,
        render_markdown=render_project_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("templates-api")
def templates_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the SDK authoring-template API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK authoring-template helper API table."""

    from paradev.sdk.templates import render_templates_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="templates-api",
        markdown=markdown,
        render_markdown=render_templates_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("copy-roots-api")
def copy_roots_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the SDK copy-root API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK copy-root helper API table."""

    from paradev.sdk.copy_roots import render_copy_roots_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="copy-roots-api",
        markdown=markdown,
        render_markdown=render_copy_roots_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("project-facade-api")
def project_facade_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the project package facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public project package facade API table."""

    from paradev.project import (
        render_project_facade_api_reference_markdown,
    )

    _emit_standard_api_reference(
        ctx,
        command_key="project-facade-api",
        markdown=markdown,
        render_markdown=render_project_facade_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("localization-api")
def localization_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the localization facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public localization package facade API table."""

    from paradev.localization import (
        render_localization_api_reference_markdown,
    )

    _emit_standard_api_reference(
        ctx,
        command_key="localization-api",
        markdown=markdown,
        render_markdown=render_localization_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("build-api")
def build_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the build facade API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public build facade API table."""

    from paradev.build import render_build_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="build-api",
        markdown=markdown,
        render_markdown=render_build_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("pdx-api")
def pdx_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the PDX API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned PDX parse and format API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="pdx-api",
        markdown=markdown,
        render_markdown=render_pdx_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("pdx-core-api")
def pdx_core_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the PDX parser facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public PDX parser facade API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="pdx-core-api",
        markdown=markdown,
        render_markdown=render_pdx_core_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("lsp-api")
def lsp_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the LSP API reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned LSP editor API table."""

    _emit_standard_api_reference(
        ctx,
        command_key="lsp-api",
        markdown=markdown,
        render_markdown=render_lsp_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("lsp-server-api")
def lsp_server_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the LSP server facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public LSP server facade API table."""

    from paradev.lsp import (
        render_lsp_server_api_reference_markdown,
    )

    _emit_standard_api_reference(
        ctx,
        command_key="lsp-server-api",
        markdown=markdown,
        render_markdown=render_lsp_server_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("catalog-api")
def catalog_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the HeavenBase catalog API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned HeavenBase catalog API table."""

    from paradev.hb import render_catalog_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="catalog-api",
        markdown=markdown,
        render_markdown=render_catalog_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("hb-api")
def hb_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the HeavenBase facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public HeavenBase facade API table."""

    from paradev.hb import render_hb_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="hb-api",
        markdown=markdown,
        render_markdown=render_hb_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("rest-api")
def rest_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the local REST/OpenAPI reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned REST/OpenAPI route table."""

    from paradev.surfaces.rest import render_rest_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="rest-api",
        markdown=markdown,
        render_markdown=render_rest_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("rest-facade-api")
def rest_facade_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the REST facade API reference as Markdown.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the public REST package facade API table."""

    from paradev.api import (
        render_rest_facade_api_reference_markdown,
    )

    _emit_standard_api_reference(
        ctx,
        command_key="rest-facade-api",
        markdown=markdown,
        render_markdown=render_rest_facade_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("mcp-api")
def mcp_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the MCP tool reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned MCP tool table."""

    from paradev.surfaces.mcp import render_mcp_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="mcp-api",
        markdown=markdown,
        render_markdown=render_mcp_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@app.command("cli-api")
def cli_api_cmd(
    ctx: typer.Context,
    symbol: str | None = typer.Option(None, "--symbol", help="Return one API reference row by symbol."),
    index_name: str | None = typer.Option(
        None,
        "--index",
        "--index-name",
        help="Return symbols for one API reference index.",
    ),
    key: str | None = typer.Option(None, "--key", help="API reference index key used with --index."),
    markdown: bool = typer.Option(False, "--markdown", help="Render the CLI command reference as Markdown."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the SDK-owned CLI command table."""

    from paradev.surfaces.cli import render_cli_api_reference_markdown

    _emit_standard_api_reference(
        ctx,
        command_key="cli-api",
        markdown=markdown,
        render_markdown=render_cli_api_reference_markdown,
        symbol=symbol,
        index_name=index_name,
        key=key,
        json_output=json_output,
    )


@lsp_app.command("serve")
def lsp_serve_cmd(
    project_path: str | None = typer.Option(
        None,
        "--project",
        "--project-path",
        help="Optional project root for catalog-backed completions.",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Optional SQLite catalog path. Defaults to .paradev/.cache/hb/catalog.sqlite.",
    ),
    game_root: str | None = typer.Option(
        None,
        "--game-root",
        help="Optional Hearts of Iron IV install path for built-in keyword completions.",
    ),
    limit: int | None = typer.Option(100, "--limit", help="Maximum completion items to return."),
    change_diagnostics_max_bytes: int | None = typer.Option(
        50_000,
        "--change-diagnostics-max-bytes",
        help="Maximum document size for synchronous diagnostics on didChange; save still diagnoses full documents.",
    ),
) -> None:
    """Run the PDX language server over stdio JSON-RPC."""

    serve_pdx_lsp_stdio(
        project_path=project_path,
        database=database,
        game_root=game_root,
        completion_limit=limit,
        change_diagnostics_max_bytes=change_diagnostics_max_bytes,
    )


@mcp_app.command("serve")
def mcp_serve_cmd() -> None:
    """Run the authoring MCP server over clean stdio.

    Args:
        None.

    Returns:
        None: This function does not return a value.
    """

    from paradev.surfaces.mcp import serve_authoring_mcp_stdio

    serve_authoring_mcp_stdio()


@lsp_app.command("diagnostics")
def lsp_diagnostics_cmd(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP diagnostics for PDX editor text."""

    try:
        payload = diagnose_pdx_lsp_text(_lsp_text(text, text_file), uri=uri, path=path or text_file)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("formatting")
def lsp_formatting_cmd(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    indent: str = typer.Option("\t", "--indent", help="Indentation unit used by the formatter."),
    comments: bool = typer.Option(True, "--comments/--no-comments", help="Keep comments in formatted output."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP formatting text edits for PDX editor text."""

    try:
        payload = format_pdx_lsp_text(
            _lsp_text(text, text_file),
            uri=uri,
            path=path or text_file,
            indent=indent,
            comments=comments,
        )
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("symbols")
def lsp_symbols_cmd(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP document symbols for PDX editor text."""

    try:
        payload = document_symbols_pdx_lsp_text(_lsp_text(text, text_file), uri=uri, path=path or text_file)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("hover")
def lsp_hover_cmd(
    ctx: typer.Context,
    line: int = typer.Option(..., "--line", help="Zero-based document line."),
    character: int = typer.Option(..., "--character", help="Zero-based document character."),
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP hover details for a PDX editor position."""

    try:
        payload = hover_pdx_lsp_text(_lsp_text(text, text_file), line, character, uri=uri, path=path or text_file)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("completion")
def lsp_completion_cmd(
    ctx: typer.Context,
    line: int = typer.Option(..., "--line", help="Zero-based document line."),
    character: int = typer.Option(..., "--character", help="Zero-based document character."),
    offset: int | None = typer.Option(
        None,
        "--offset",
        help="Optional zero-based document offset for faster large-buffer completion.",
    ),
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    project_path: str | None = typer.Option(
        None,
        "--project",
        "--project-path",
        help="Optional project path for catalog-backed completions.",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Optional SQLite catalog path. Defaults to .paradev/.cache/hb/catalog.sqlite.",
    ),
    game_root: str | None = typer.Option(
        None,
        "--game-root",
        help="Optional Hearts of Iron IV install path for built-in keyword completions.",
    ),
    limit: int | None = typer.Option(100, "--limit", help="Maximum completion items to return."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP completion items for a PDX editor position."""

    try:
        project = open_project(project_path) if project_path else None
        payload = complete_pdx_lsp_text(
            _lsp_text(text, text_file),
            line,
            character,
            uri=uri,
            path=path or text_file,
            project=project,
            database=database,
            game_root=game_root,
            limit=limit,
            offset=offset,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="--project") from error
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error), param_hint="--database") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("keywords")
def lsp_keywords_cmd(
    ctx: typer.Context,
    game_root: str | None = typer.Option(
        None,
        "--game-root",
        help="Optional Hearts of Iron IV install path for keyword data.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return the HOI4 keyword dataset used by editor completion."""

    try:
        payload = hoi4_keyword_dataset(game_root=game_root)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--game-root") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@lsp_app.command("semantic-tokens")
def lsp_semantic_tokens_cmd(
    ctx: typer.Context,
    text: str | None = typer.Option(None, "--text", help="Current editor text. Mutually exclusive with --text-file."),
    text_file: str | None = typer.Option(None, "--text-file", help="Read current editor text from a file."),
    uri: str | None = typer.Option(None, "--uri", help="Optional document URI."),
    path: str | None = typer.Option(None, "--path", help="Optional display path for file extension metadata."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Return LSP semantic tokens for PDX editor highlighting."""

    try:
        payload = semantic_tokens_pdx_lsp_text(_lsp_text(text, text_file), uri=uri, path=path or text_file)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--text/--text-file") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("parse")
def parse_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="PDX source file to parse."),
    include_dump: bool = typer.Option(False, "--dump", help="Include the lossless PDX dump payload."),
    include_tokens: bool = typer.Option(False, "--tokens", help="Include lexer token rows when parsing succeeds."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Parse one PDX source file."""

    payload = parse_pdx_file(path, include_dump=include_dump, include_tokens=include_tokens)
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if not payload["ok"]:
        raise typer.Exit(1)


@app.command("format")
def format_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="PDX source file to format."),
    indent: str = typer.Option("\t", "--indent", help="Indentation unit used by the formatter."),
    comments: bool = typer.Option(True, "--comments/--no-comments", help="Keep comments in formatted output."),
    write: bool = typer.Option(
        False,
        "--write",
        help="Replace the source file with formatted text when changed.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Format one PDX source file."""

    payload = format_pdx_file(path, indent=indent, comments=comments, write=write)
    if _json_enabled(ctx, json_output):
        _emit(payload, as_json=True)
    elif payload["ok"] and not write:
        _emit(payload["formatted_text"])
    elif payload["ok"]:
        _emit(f"{'wrote' if payload['written'] else 'unchanged'} {payload['path']}")
    else:
        _emit(payload)
    if not payload["ok"]:
        raise typer.Exit(1)


@app.command("new")
def new_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Target project root to create."),
    project_id: str | None = typer.Option(None, "--project-id", help="Stable project id. Defaults to the folder name."),
    title: str | None = typer.Option(None, "--title", help="Display title. Defaults to the folder name."),
    game: str = typer.Option("hoi4", "--game", help="Target game package. Currently supports hoi4."),
    force: bool = typer.Option(False, "--force", help="Allow writing starter files into a non-empty directory."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Create a buildable starter project."""

    try:
        project = create_project(path, project_id=project_id, title=title, game=game, force=force)
    except ProjectCreateError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    payload = project_create_payload(project)
    _emit(
        payload if _json_enabled(ctx, json_output) else f"created {project.root}",
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("project-find")
def project_find_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Find a project from a root or nested path."""

    payload = Project.find(path)
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if payload.get("found") is not True:
        raise typer.Exit(1)


@app.command("projects")
def projects_cmd(
    ctx: typer.Context,
    project_paths: list[str] | None = typer.Option(
        None,
        "--project",
        help="Explicit project root, manifest, or nested path. Repeat for multiple projects.",
    ),
    search_roots: list[str] | None = typer.Option(
        None,
        "--root",
        help="Directory to search for ParaDev projects. Repeat for multiple roots.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print registered local ParaDev projects."""

    payload = registered_projects(project_paths=tuple(project_paths or ()), search_roots=tuple(search_roots or ()))
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("desktop-state")
def desktop_state_cmd(
    ctx: typer.Context,
    project_path: str | None = typer.Option(None, "--project", help="Active project root, manifest, or nested path."),
    project_paths: list[str] | None = typer.Option(
        None,
        "--known-project",
        help="Known project root, manifest, or nested path. Repeat for multiple projects.",
    ),
    search_roots: list[str] | None = typer.Option(
        None,
        "--root",
        help="Directory to search for ParaDev projects. Repeat for multiple roots.",
    ),
    include_browser: bool = typer.Option(
        True,
        "--browser/--no-browser",
        help="Include the active project browser payload.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print SDK-owned project state for the desktop shell."""

    try:
        payload = desktop_state(
            project_path,
            project_paths=tuple(project_paths or ()),
            search_roots=tuple(search_roots or ()),
            include_browser=include_browser,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="--project") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("project-browser")
def project_browser_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(None, "--profile", help="Build profile to inspect."),
    kind: str | None = typer.Option(None, "--kind", help="Optional browser item kind: module or collection."),
    family: str | None = typer.Option(None, "--family", help="Only include one family."),
    module_id: str | None = typer.Option(
        None,
        "--module",
        help="Only include one module id, or collections containing it.",
    ),
    collection_id: str | None = typer.Option(
        None,
        "--collection",
        help="Only include one collection id, or modules assigned to it.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Only count browser families without parsing item payloads.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the read-only project browser payload for frontend clients."""

    try:
        project = open_project(path)
        if summary:
            if module_id is not None or collection_id is not None:
                raise ValueError("--summary cannot be combined with --module or --collection.")
            payload = project.browser_summary(profile=profile, kind=kind, family=family)
        else:
            payload = project.browser(
                profile=profile,
                kind=kind,
                family=family,
                module_id=module_id,
                collection_id=collection_id,
            )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--kind") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("project-rename")
def project_rename_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    title: str = typer.Argument(..., help="New project display title."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Rename a project's display title without moving files."""

    try:
        project = open_project(path)
        renamed = project.rename(title)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="title") from error
    payload = _project_rename_payload(project, renamed)
    _emit(
        payload if _json_enabled(ctx, json_output) else f"renamed {renamed.root}",
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("project-language")
def project_language_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    preferred_language: str = typer.Argument(
        ...,
        help="Default authoring language alias, such as en or zh.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply the reviewed manifest update.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the current dry plan.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Render structured output as JSON.",
    ),
) -> None:
    """Plan or apply a project's default module-authoring language."""

    try:
        payload = open_project(path).set_preferred_language(
            preferred_language,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(
            str(error),
            param_hint="preferred_language",
        ) from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("module-rename")
def module_rename_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Existing module id in family/object_id form."),
    object_id: str = typer.Argument(..., help="New module object id path segment."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate module ids.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Display title used to canonicalize the readable module-folder suffix.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Rename a source module folder or readable title without rewriting PDX content."""

    try:
        payload = open_project(path).rename_module(
            module_id,
            object_id,
            source_root=source_root,
            title=title,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="module_id") from error
    message = f"renamed {payload['previous_module_id']} -> {payload['module_id']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("module-remove")
def module_remove_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Existing module id in family/object_id form."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate module ids.",
    ),
    write: bool = typer.Option(False, "--write", help="Remove the module folder when the plan is not blocked."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or remove a source module folder."""

    try:
        payload = open_project(path).remove_module(module_id, source_root=source_root, write=write)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="module_id") from error
    message = f"removed {payload['relative_path']}" if payload.get("removed") else f"planned remove {payload['relative_path']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-duplicate")
def module_duplicate_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Existing module id in family/object_id form."),
    object_id: str = typer.Argument(..., help="Object id for the duplicated module folder."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate the source module.",
    ),
    destination_source_root: str | None = typer.Option(
        None,
        "--destination-source-root",
        help="Configured source root that receives the duplicate.",
    ),
    identity: str = typer.Option(
        "rewrite",
        "--identity",
        help="Identity policy: rewrite (independent module) or preserve (exact copy).",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply a reviewed duplicate plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan; required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or atomically create an independent module copy."""

    try:
        payload = open_project(path).duplicate_module(
            module_id,
            object_id,
            source_root=source_root,
            destination_source_root=destination_source_root,
            identity=identity,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="module_id") from error
    action = "duplicated" if payload.get("applied") else "planned duplicate"
    message = f"{action} {payload['source_module_id']} -> {payload['module_id']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-metadata-clean")
def module_metadata_clean_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="Only clean modules in one compiler family.",
    ),
    module_id: str | None = typer.Option(
        None,
        "--module",
        "--module-id",
        help="Only clean one module id in family/object_id form.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Only clean modules in one configured source root.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply a reviewed metadata cleanup plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan; required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or atomically remove redundant path-derived module metadata."""

    try:
        payload = open_project(path).clean_module_metadata(
            family=family,
            module_id=module_id,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--module") from error
    action = "applied metadata cleanup" if payload.get("applied") else "planned metadata cleanup"
    as_json = _json_enabled(ctx, json_output)
    _emit(payload if as_json or payload.get("blocked") else action, as_json=as_json)
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-collection-set")
def module_collection_set_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Module id in family/object_id form."),
    collection_id: str | None = typer.Option(
        None,
        "--collection",
        "--collection-id",
        help="Target same-family collection. Omit to clear membership.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root for an ambiguous module id.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply a reviewed module collection plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan; required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or atomically change one module's collection membership."""

    try:
        payload = open_project(path).set_module_collection(
            module_id,
            collection_id,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="module_id") from error
    action = "updated collection" if payload.get("applied") else "planned collection update"
    as_json = _json_enabled(ctx, json_output)
    _emit(payload if as_json or payload.get("blocked") else action, as_json=as_json)
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-activity-set")
def module_activity_set_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Module id in family/object_id form."),
    active: bool = typer.Option(
        True,
        "--active/--inactive",
        help="Include or omit this module in every compilation mode.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root for an ambiguous module id.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply a reviewed module activity plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan; required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or atomically activate or deactivate one source module."""

    try:
        payload = open_project(path).set_module_active(
            module_id,
            active,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="module_id") from error
    action = "updated module activity" if payload.get("applied") else "planned module activity update"
    as_json = _json_enabled(ctx, json_output)
    _emit(payload if as_json or payload.get("blocked") else action, as_json=as_json)
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-diagram")
def module_diagram_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    family: str = typer.Argument(
        ...,
        help=("Registered source-backed diagram provider id or alias. " "Discover project-specific values from project-browser."),
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Build profile used to discover authoritative module sources.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Render structured output as JSON.",
    ),
) -> None:
    """Project one authoritative module family into a source-backed diagram."""

    try:
        payload = open_project(path).module_diagram(family, profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="family") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("module-diagram-edit")
def module_diagram_edit_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    family: str = typer.Argument(
        ...,
        help=("Registered editable diagram provider id or alias. " "Discover project-specific values from project-browser."),
    ),
    request: str | None = typer.Option(
        None,
        "--request",
        help=("JSON request file containing position_intents, edge_intents, " "or one provider-owned node_intent, or '-' for stdin."),
    ),
    request_json: str | None = typer.Option(
        None,
        "--request-json",
        help=("Inline JSON request containing position_intents, edge_intents, " "or one provider-owned node_intent."),
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Build profile used to discover authoritative module sources.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        "--apply",
        help="Apply the exact reviewed diagram plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan; required with --write.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Render structured output as JSON.",
    ),
) -> None:
    """Plan or apply bounded source-backed diagram intents from JSON."""

    try:
        body = _json_request_object(
            request,
            request_json=request_json,
            label="Module diagram edit request",
        )
        _validate_json_request_fields(
            body,
            frozenset(
                {
                    "position_intents",
                    "edge_intents",
                    "node_intents",
                }
            ),
            label="Module diagram edit request",
        )
        from paradev.sdk._module_diagram_api import (
            module_diagram_node_intents,
        )

        position_intents, edge_intents = module_diagram_intents(
            body.get("position_intents"),
            body.get("edge_intents"),
        )
        node_intents = module_diagram_node_intents(
            body.get("node_intents"),
        )
        payload = open_project(path).edit_module_diagram(
            family,
            position_intents=position_intents,
            edge_intents=edge_intents,
            node_intents=node_intents,
            profile=profile,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--request") from error
    as_json = _json_enabled(ctx, json_output)
    status = str(payload.get("status", "planned"))
    _emit(
        payload if as_json or payload.get("blocked") else f"{status} {family} diagram edits",
        as_json=as_json,
    )
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-file")
def module_file_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Existing module id in family/object_id form."),
    relative_path: str = typer.Argument(..., help="File path relative to the module root."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate module ids.",
    ),
    encoding: str = typer.Option("utf-8", "--encoding", help="Text encoding used to read the file."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Read one text source file from a module."""

    try:
        payload = open_project(path).read_module_file(module_id, relative_path, source_root=source_root, encoding=encoding)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="relative_path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("module-edit")
def module_edit_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    module_id: str = typer.Argument(..., help="Existing module id in family/object_id form."),
    relative_path: str = typer.Argument(..., help="File path relative to the module root."),
    text: str = typer.Option(..., "--text", help="Replacement file text."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate module ids.",
    ),
    create: bool = typer.Option(False, "--create", help="Create the file and parent folders when missing."),
    encoding: str = typer.Option("utf-8", "--encoding", help="Text encoding used to write the file."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Write one text source file inside a module."""

    try:
        payload = open_project(path).write_module_file(
            module_id,
            relative_path,
            text,
            source_root=source_root,
            create=create,
            encoding=encoding,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="relative_path") from error
    message = f"wrote {payload['relative_path']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("module-batch-edit")
def module_batch_edit_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    request: str | None = typer.Option(
        None,
        "--request",
        help="JSON request file containing an edits array and optional defaults, or '-' to read stdin.",
    ),
    request_json: str | None = typer.Option(
        None,
        "--request-json",
        help="Inline JSON request object containing an edits array and optional defaults.",
    ),
    create: bool = typer.Option(False, "--create", help="Create missing files by default."),
    encoding: str = typer.Option("utf-8", "--encoding", help="Default text encoding used to write files."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview the batch without writing files."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Write multiple text source files inside modules from a JSON request."""

    try:
        body = _json_request_object(request, request_json=request_json, label="Module batch edit request")
        request_create = _json_request_bool_default(body, "create", default=create, label="Module batch edit request")
        request_encoding = _json_request_text_default(body, "encoding", default=encoding, label="Module batch edit request")
        edits = _json_request_array(body, "edits", label="Module batch edit request")
        payload = open_project(path).write_module_files(edits, create=request_create, encoding=request_encoding, write=not dry_run)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--request") from error
    verb = "planned" if dry_run else "wrote"
    message = _module_batch_edit_message(payload, verb=verb)
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("module-batch-create")
def module_batch_create_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    request: str | None = typer.Option(
        None,
        "--request",
        help="JSON request file containing a modules array, or '-' to read stdin.",
    ),
    request_json: str | None = typer.Option(
        None,
        "--request-json",
        help="Inline JSON request object containing a modules array.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that receives every new module.",
    ),
    write: bool = typer.Option(False, "--write", "--apply", help="Apply the reviewed plan atomically."),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by the dry plan. Required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or atomically create multiple modules from one JSON request."""

    try:
        body = _json_request_object(request, request_json=request_json, label="Module batch create request")
        modules = _json_request_array(body, "modules", label="Module batch create request")
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--request") from error
    try:
        project = open_project(path)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    try:
        payload = project.create_modules(
            modules,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--request/--source-root") from error
    except OSError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    message = _module_batch_create_message(payload)
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("module-batch-request")
def module_batch_request_cmd(
    path: str = typer.Argument(..., help="Project root or nested project path."),
    request: str | None = typer.Option(
        None,
        "--request",
        help="JSON request file containing an edits array and optional defaults, or '-' to read stdin.",
    ),
    request_json: str | None = typer.Option(
        None,
        "--request-json",
        help="Inline JSON request object containing an edits array and optional defaults.",
    ),
    edit_json: list[str] | None = typer.Option(None, "--edit-json", help="Inline JSON edit object. Repeat for multiple edits."),
    create: bool = typer.Option(
        False,
        "--create",
        help="Create missing files by default when the request is applied.",
    ),
    encoding: str = typer.Option(
        "utf-8",
        "--encoding",
        help="Default text encoding used when the request is applied.",
    ),
) -> None:
    """Emit a canonical JSON request for module-batch-edit."""

    try:
        body = _module_batch_request_object(request, request_json=request_json, edit_json=edit_json)
        request_create = _json_request_bool_default(body, "create", default=create, label="Module batch edit request")
        request_encoding = _json_request_text_default(body, "encoding", default=encoding, label="Module batch edit request")
        edits = _json_request_array(body, "edits", label="Module batch edit request")
        payload = open_project(path).module_batch_edit_request(edits, create=request_create, encoding=request_encoding)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--request") from error
    _emit(payload, as_json=True)


@app.command("collection-file")
def collection_file_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    collection_id: str = typer.Argument(..., help="Existing collection id."),
    relative_path: str = typer.Argument(..., help="File path relative to the collection descriptor root."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="Collection family used to disambiguate duplicate collection ids.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate collection ids.",
    ),
    encoding: str = typer.Option("utf-8", "--encoding", help="Text encoding used to read the file."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Read one text source file from a collection descriptor."""

    try:
        payload = open_project(path).read_collection_file(
            collection_id,
            relative_path,
            family=family,
            source_root=source_root,
            encoding=encoding,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="relative_path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("collection-edit")
def collection_edit_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    collection_id: str = typer.Argument(..., help="Existing collection id."),
    relative_path: str = typer.Argument(..., help="File path relative to the collection descriptor root."),
    text: str = typer.Option(..., "--text", help="Replacement file text."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="Collection family used to disambiguate duplicate collection ids.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate collection ids.",
    ),
    create: bool = typer.Option(False, "--create", help="Create the file and parent folders when missing."),
    encoding: str = typer.Option("utf-8", "--encoding", help="Text encoding used to write the file."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Write one text source file inside a collection descriptor."""

    try:
        payload = open_project(path).write_collection_file(
            collection_id,
            relative_path,
            text,
            family=family,
            source_root=source_root,
            create=create,
            encoding=encoding,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="relative_path") from error
    message = f"wrote {payload['relative_path']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("collection-create")
def collection_create_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    family: str = typer.Argument(..., help="Collection family."),
    collection_id: str = typer.Argument(..., help="New collection descriptor id."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that receives the collection descriptor.",
    ),
    metadata_pairs: list[str] | None = typer.Option(
        None,
        "--metadata",
        "-m",
        help="Metadata value as KEY=VALUE. Repeat for multiple values.",
    ),
    write: bool = typer.Option(False, "--write", help="Write meta.yaml when the create plan is not blocked."),
    force: bool = typer.Option(False, "--force", help="Allow overwriting an existing collection meta.yaml."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or write a collection descriptor metadata scaffold."""

    try:
        payload = open_project(path).create_collection(
            family,
            collection_id,
            source_root=source_root,
            metadata=_metadata_values(metadata_pairs),
            write=write,
            force=force,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="collection") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("collection-rename")
def collection_rename_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    collection_id: str = typer.Argument(..., help="Existing collection descriptor id."),
    target_id: str = typer.Argument(..., help="New collection descriptor id path segment."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="Collection family used to disambiguate duplicate collection ids.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate collection ids.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Rename a collection descriptor folder without rewriting authored content."""

    try:
        payload = open_project(path).rename_collection(collection_id, target_id, family=family, source_root=source_root)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="collection_id") from error
    message = f"renamed {payload['previous_collection_id']} -> {payload['collection_id']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )


@app.command("collection-remove")
def collection_remove_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Project root or nested project path."),
    collection_id: str = typer.Argument(..., help="Existing collection descriptor id."),
    family: str | None = typer.Option(
        None,
        "--family",
        help="Collection family used to disambiguate duplicate collection ids.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root used to disambiguate duplicate collection ids.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Apply the reviewed collection removal plan.",
    ),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact plan_hash returned by the current dry plan.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or remove a collection while preserving and ungrouping its modules."""

    try:
        payload = open_project(path).remove_collection(
            collection_id,
            family=family,
            source_root=source_root,
            write=write,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="collection_id") from error
    message = f"removed {payload['relative_path']}" if payload.get("removed") else f"planned remove {payload['relative_path']}"
    _emit(
        payload if _json_enabled(ctx, json_output) else message,
        as_json=_json_enabled(ctx, json_output),
    )
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("templates")
def templates_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    template_id: str | None = typer.Option(None, "--template-id", help="Only include one template id."),
    family: str | None = typer.Option(None, "--family", help="Only include templates for one family."),
    kind: str | None = typer.Option(None, "--kind", help="Only include module or collection templates."),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Only include templates from one source: builtin or project.",
    ),
    authoring_ready: bool | None = typer.Option(
        None,
        "--authoring-ready/--not-authoring-ready",
        help="Filter by scaffold readiness.",
    ),
    diagnostic_code: str | None = typer.Option(
        None,
        "--diagnostic-code",
        help="Only include templates with one diagnostic code.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print SDK authoring templates for a local project."""

    try:
        payload = open_project(path).templates(
            template_id=template_id,
            family=family,
            kind=kind,
            source=source,
            authoring_ready=authoring_ready,
            diagnostic_code=diagnostic_code,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("authoring-path")
def authoring_path_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    kind: str = typer.Argument(..., help="Authoring target kind: module or collection."),
    family: str = typer.Argument(..., help="Module or collection family."),
    target_id: str = typer.Argument(..., help="Module object id or collection id."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that owns the authoring path.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the canonical source folder for a module or collection."""

    try:
        payload = open_project(path).authoring_path(kind, family, target_id, source_root=source_root)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="target") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("authoring-plan")
def authoring_plan_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    kind: str = typer.Argument(..., help="Authoring target kind: module or collection."),
    family: str = typer.Argument(..., help="Module or collection family."),
    target_id: str = typer.Argument(..., help="Module object id or collection id."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that owns the authoring path.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print an authoring destination with expected source slots."""

    try:
        payload = open_project(path).authoring_plan(kind, family, target_id, source_root=source_root, profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="target") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("scaffold")
def scaffold_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    template_id: str = typer.Argument(..., help="Template id from the templates command."),
    object_id: str = typer.Argument(..., help="New module folder/object id."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that receives the new module.",
    ),
    value_pairs: list[str] | None = typer.Option(
        None,
        "--value",
        "-v",
        help="Template value as KEY=VALUE. Repeat for multiple values.",
    ),
    write: bool = typer.Option(False, "--write", help="Write files when the scaffold plan is not blocked."),
    force: bool = typer.Option(False, "--force", help="Allow overwriting existing scaffold files."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or write a source module from an authoring template."""

    try:
        payload = open_project(path).scaffold_module(
            template_id,
            object_id,
            source_root=source_root,
            values=_template_values(value_pairs),
            write=write,
            force=force,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="template") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("collection-scaffold")
def collection_scaffold_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    template_id: str = typer.Argument(..., help="Collection template id or unambiguous family."),
    collection_id: str = typer.Argument(..., help="New logical collection id."),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Configured source root that receives the new collection.",
    ),
    value_pairs: list[str] | None = typer.Option(
        None,
        "--value",
        "-v",
        help="Template value as KEY=VALUE. Repeat for multiple values.",
    ),
    write: bool = typer.Option(False, "--write", help="Apply the reviewed collection plan."),
    force: bool = typer.Option(False, "--force", help="Allow overwriting existing collection scaffold files."),
    plan_hash: str | None = typer.Option(
        None,
        "--plan-hash",
        help="Exact hash returned by a dry plan. Required with --write.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan or transactionally write a collection from a template."""

    try:
        payload = open_project(path).scaffold_collection(
            template_id,
            collection_id,
            source_root=source_root,
            values=_template_values(value_pairs),
            write=write,
            force=force,
            plan_hash=plan_hash,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="template") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if write and payload.get("blocked"):
        raise typer.Exit(1)


@app.command("draft-apply")
def draft_apply_cmd(
    ctx: typer.Context,
    project_id: str = typer.Argument(..., help="Project id to apply draft edits to."),
    project_root: str | None = typer.Option(None, "--project-root", help="Explicit project root for the project id."),
    request: str = typer.Option(
        ...,
        "--request",
        help="JSON request body file containing source edits, removals, or replacements, or '-' to read stdin.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Apply validated draft edits and an optional module rename."""

    try:
        body = _json_request_object(request, label="Draft apply request")
        project = _project_from_id(project_id, project_root=project_root)
        payload = project.apply_source_draft(
            source_edits=body.get("source_edits"),
            source_removals=body.get("source_removals"),
            source_replacements=body.get("source_replacements"),
            module_rename=body.get("module_rename"),
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="--project-root") from error
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error), param_hint="--request") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def build(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Build only one module or collection family."),
    module_id: str | None = typer.Option(None, "--module", "--module-id", help="Build only one module id."),
    collection_id: str | None = typer.Option(None, "--collection", "--collection-id", help="Build only one collection id."),
    emit_artifacts: bool = typer.Option(
        False,
        "--emit-artifacts",
        help="Write planned artifacts under the project output root.",
    ),
    emit_manifests: bool = typer.Option(
        False,
        "--emit-manifests",
        help="Write build manifest JSON files under the project build root.",
    ),
    full_rebuild: bool = typer.Option(
        False,
        "--full-rebuild",
        help="Clean generated output and build roots before artifact emission while preserving registered HOI4 output directories.",
    ),
    sync_launcher_descriptor: bool = typer.Option(
        True,
        "--sync-launcher-descriptor/--no-sync-launcher-descriptor",
        help="Synchronize the external HoI4 launcher descriptor during artifact emission. Disable for project-only builds.",
    ),
    parallelism: int | None = typer.Option(
        None,
        "--parallelism",
        min=1,
        help="Maximum independent build-family workers. Defaults to paradev.build.parallelism.",
    ),
    progress_jsonl: str | None = typer.Option(None, "--progress-jsonl", help="Write compiler progress events as JSON lines."),
    strict_metadata: bool | None = typer.Option(
        None,
        "--strict-metadata/--no-strict-metadata",
        help="Treat unknown module and collection metadata keys as blocking diagnostics. Defaults to paradev.build.strict_metadata.",
    ),
    summary_only: bool = typer.Option(
        False,
        "--summary",
        help="Render only the compact summary after planning or emitting the build.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Plan a local project build and optionally emit its artifacts."""

    if module_id is not None and collection_id is not None:
        raise typer.BadParameter("Pass only one targeted build selector: --module or --collection.")
    if full_rebuild and not emit_artifacts:
        raise typer.BadParameter("--full-rebuild requires --emit-artifacts.")
    progress = _build_progress_jsonl_writer(progress_jsonl)
    try:
        from paradev.build.progress import emit_progress

        emit_progress(
            progress,
            "load_project",
            detail=f"Opening {path}.",
            label="Opening project",
            percent=1,
        )
        project = open_project(path)
        result = project.build(
            profile=profile,
            emit_artifacts=emit_artifacts,
            emit_manifests=emit_manifests,
            family=family,
            module_id=module_id,
            collection_id=collection_id,
            full_rebuild=full_rebuild,
            sync_launcher_descriptor=sync_launcher_descriptor,
            parallelism=parallelism,
            strict_metadata=strict_metadata,
            progress=progress,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    if summary_only:
        from paradev.build.views import summary_view

        payload = summary_view(result)
    else:
        payload = result.to_dict()
    _emit(payload, as_json=_json_enabled(ctx, json_output))
    if emit_artifacts and result.blocked:
        raise typer.Exit(1)


@app.command()
def summary(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print the build summary for a local project."""

    try:
        payload = open_project(path).inspect("summary", profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def manifests(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print all build manifest payloads for a local project."""

    try:
        payload = open_project(path).inspect("manifests", profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def inspections(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Render the generated project inspection reference as Markdown.",
    ),
    kind: str | None = typer.Option(None, "--kind", help="Select one static project inspection row by kind."),
    index_name: str | None = typer.Option(None, "--index", help="Select one static project inspection index."),
    key: str | None = typer.Option(None, "--key", help="Select one key inside the selected inspection index."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print SDK inspection kinds and filters for adapter clients."""

    index_lookup = index_name is not None or key is not None
    if markdown and (kind is not None or index_lookup):
        raise typer.BadParameter("--markdown cannot be combined with selectors.")
    if markdown and _json_requested(ctx, json_output):
        raise typer.BadParameter("--markdown cannot be combined with --json.")
    if kind is not None and index_lookup:
        raise typer.BadParameter("Pass only one project inspection selector: --kind or --index/--key.")
    if index_lookup and (index_name is None or key is None):
        raise typer.BadParameter("--index requires --key, and --key requires --index.")
    if markdown:
        print(render_project_inspection_reference_markdown().rstrip())
        return
    if kind is not None or index_lookup:
        try:
            payload = get_project_inspection_selection(kind=kind, index_name=index_name, key=key)
        except (KeyError, ValueError) as error:
            message = str(error.args[0]) if error.args else str(error)
            raise typer.BadParameter(message) from error
        _emit(payload, as_json=_json_enabled(ctx, json_output))
        return
    try:
        payload = open_project(path).inspect("inspections")
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def modules(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include modules from one family."),
    module_id: str | None = typer.Option(None, "--module", help="Only include one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include modules in one collection."),
    source_slot: str | None = typer.Option(None, "--slot", help="Only include modules with one source slot."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print discovered build modules for a local project."""

    try:
        payload = open_project(path).inspect(
            "modules",
            profile=profile,
            family=family,
            module_id=module_id,
            collection_id=collection_id,
            source_slot=source_slot,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def collections(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include collections from one family."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include one collection id."),
    module_id: str | None = typer.Option(None, "--module", help="Only include collections containing one module id."),
    source_slot: str | None = typer.Option(None, "--slot", help="Only include collections with one descriptor source slot."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print discovered build collections for a local project."""

    try:
        payload = open_project(path).inspect(
            "collections",
            profile=profile,
            family=family,
            collection_id=collection_id,
            module_id=module_id,
            source_slot=source_slot,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def artifacts(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    artifact_type: str | None = typer.Option(None, "--type", help="Only include one artifact type."),
    target_root: str | None = typer.Option(None, "--target-root", help="Only include one artifact target root."),
    owner: str | None = typer.Option(None, "--owner", help="Only include one artifact owner."),
    artifact_path: str | None = typer.Option(None, "--path", help="Only include one artifact path."),
    mode: str | None = typer.Option(None, "--mode", help="Only include one artifact mode."),
    module_id: str | None = typer.Option(None, "--module", help="Only include artifacts that involve one module id."),
    collection_id: str | None = typer.Option(
        None,
        "--collection",
        help="Only include artifacts that involve one collection id.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print planned build artifacts for a local project."""

    try:
        payload = open_project(path).inspect(
            "artifacts",
            profile=profile,
            artifact_type=artifact_type,
            target_root=target_root,
            owner=owner,
            path=artifact_path,
            mode=mode,
            module_id=module_id,
            collection_id=collection_id,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def localization(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    language: str | None = typer.Option(None, "--language", help="Only include one localization language id or alias."),
    key: str | None = typer.Option(None, "--key", help="Only include one exact localization key."),
    key_prefix: str | None = typer.Option(None, "--key-prefix", help="Only include keys with this prefix."),
    module_id: str | None = typer.Option(None, "--module", help="Only include rows from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include rows from one collection id."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print loaded localization rows for a local project."""

    try:
        payload = open_project(path).inspect(
            "localization",
            profile=profile,
            language=language,
            key=key,
            key_prefix=key_prefix,
            module_id=module_id,
            collection_id=collection_id,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def sources(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include sources from one family."),
    module_id: str | None = typer.Option(None, "--module", help="Only include sources from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include sources from one collection id."),
    slot: str | None = typer.Option(None, "--slot", help="Only include sources from one slot."),
    loader: str | None = typer.Option(
        None,
        "--loader",
        help="Only include sources handled by one loader: pdx, loc, copy, or unknown.",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Only include sources with one status: loaded, matched, or diagnostic.",
    ),
    owner_kind: str | None = typer.Option(
        None,
        "--owner-kind",
        help="Only include sources owned by module or collection rows.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print compiler input source files for a local project."""

    try:
        payload = open_project(path).inspect(
            "sources",
            profile=profile,
            family=family,
            module_id=module_id,
            collection_id=collection_id,
            slot=slot,
            loader=loader,
            status=status,
            owner_kind=owner_kind,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="owner-kind") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("source-slots")
def source_slots(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include source slots from one family."),
    module_id: str | None = typer.Option(None, "--module", help="Only include source slots from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include source slots from one collection id."),
    slot: str | None = typer.Option(None, "--slot", help="Only include one source slot."),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Only include one slot status: satisfied, missing, empty, or diagnostic.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print expected source-slot status for discovered project sources."""

    try:
        payload = open_project(path).inspect(
            "source-slots",
            profile=profile,
            family=family,
            module_id=module_id,
            collection_id=collection_id,
            slot=slot,
            status=status,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def assets(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    module_id: str | None = typer.Option(None, "--module", help="Only include rows from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include rows from one collection id."),
    family: str | None = typer.Option(None, "--family", help="Only include rows from one module family."),
    slot: str | None = typer.Option(None, "--slot", help="Only include rows from one source slot."),
    file_format: str | None = typer.Option(None, "--format", help="Only include rows with one asset format."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print loaded static copy assets for a local project."""

    try:
        payload = open_project(path).inspect(
            "assets",
            profile=profile,
            module_id=module_id,
            collection_id=collection_id,
            family=family,
            slot=slot,
            file_format=file_format,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def sprites(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    module_id: str | None = typer.Option(None, "--module", help="Only include sprites sourced from one module id."),
    collection_id: str | None = typer.Option(
        None,
        "--collection",
        help="Only include sprites sourced from one collection id.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include sprites sourced from one module family."),
    slot: str | None = typer.Option(None, "--slot", help="Only include sprites sourced from one slot."),
    name: str | None = typer.Option(None, "--name", help="Only include one exact sprite name."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print planned interface sprite declarations for a local project."""

    try:
        payload = open_project(path).inspect(
            "sprites",
            profile=profile,
            module_id=module_id,
            collection_id=collection_id,
            family=family,
            slot=slot,
            name=name,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def diagnostics(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    severity: str | None = typer.Option(None, "--severity", help="Only include diagnostics with one severity."),
    code: str | None = typer.Option(None, "--code", help="Only include diagnostics with one code."),
    family: str | None = typer.Option(None, "--family", help="Only include diagnostics from one family."),
    owner: str | None = typer.Option(None, "--owner", help="Only include diagnostics involving one artifact owner."),
    target_root: str | None = typer.Option(
        None,
        "--target-root",
        help="Only include diagnostics for one artifact target root.",
    ),
    module_id: str | None = typer.Option(None, "--module", help="Only include diagnostics from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include diagnostics from one collection id."),
    source_path: str | None = typer.Option(None, "--source", help="Only include diagnostics for one source path."),
    slot: str | None = typer.Option(None, "--slot", help="Only include diagnostics from one source slot."),
    published: bool = typer.Option(
        False,
        "--published",
        help="Read the diagnostics manifest emitted by the most recent build without compiling again.",
    ),
    strict_metadata: bool | None = typer.Option(
        None,
        "--strict-metadata/--no-strict-metadata",
        help="Treat unknown module and collection metadata keys as blocking diagnostics. Defaults to paradev.build.strict_metadata.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print build diagnostics for a local project."""

    try:
        payload = open_project(path).inspect(
            "diagnostics",
            profile=profile,
            severity=severity,
            code=code,
            family=family,
            owner=owner,
            target_root=target_root,
            module_id=module_id,
            collection_id=collection_id,
            source_path=source_path,
            slot=slot,
            strict_metadata=strict_metadata,
            published=published,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("source-map")
def source_map(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    module_id: str | None = typer.Option(None, "--module", help="Only include rows sourced from one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Only include rows sourced from one collection id."),
    family: str | None = typer.Option(None, "--family", help="Only include rows sourced from one module family."),
    slot: str | None = typer.Option(None, "--slot", help="Only include rows sourced from one slot."),
    artifact_type: str | None = typer.Option(None, "--type", help="Only include rows for one artifact type."),
    target_root: str | None = typer.Option(None, "--target-root", help="Only include rows for one artifact target root."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print artifact-to-source traceability for a local project."""

    try:
        payload = open_project(path).inspect(
            "source-map",
            profile=profile,
            module_id=module_id,
            collection_id=collection_id,
            family=family,
            slot=slot,
            artifact_type=artifact_type,
            target_root=target_root,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def dependencies(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    source: str | None = typer.Option(None, "--source", help="Only include one dependency source."),
    target: str | None = typer.Option(None, "--target", help="Only include one dependency target."),
    kind: str | None = typer.Option(None, "--kind", help="Only include one dependency kind."),
    module_id: str | None = typer.Option(None, "--module", help="Only include dependencies from one module id."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print build dependency edges for a local project."""

    try:
        payload = open_project(path).inspect(
            "dependencies",
            profile=profile,
            source=source,
            target=target,
            kind=kind,
            module_id=module_id,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("build-graph")
def build_graph_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    module_id: str | None = typer.Option(None, "--module", help="Only include graph edges related to one module id."),
    collection_id: str | None = typer.Option(
        None,
        "--collection",
        help="Only include graph edges related to one collection id.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include graph edges related to one family."),
    slot: str | None = typer.Option(None, "--slot", help="Only include graph edges related to one source slot."),
    artifact_type: str | None = typer.Option(None, "--type", help="Only include graph edges for one artifact type."),
    target_root: str | None = typer.Option(
        None,
        "--target-root",
        help="Only include graph edges for one artifact target root.",
    ),
    edge_kind: str | None = typer.Option(None, "--kind", help="Only include one edge kind, such as emits or requires."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print source, artifact, and dependency graph edges for a local project."""

    try:
        payload = open_project(path).inspect(
            "build-graph",
            profile=profile,
            module_id=module_id,
            collection_id=collection_id,
            family=family,
            slot=slot,
            artifact_type=artifact_type,
            target_root=target_root,
            edge_kind=edge_kind,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command("build-explain")
def build_explain_cmd(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    module_id: str | None = typer.Option(None, "--module", help="Explain one module id."),
    collection_id: str | None = typer.Option(None, "--collection", help="Explain one collection id."),
    source_path: str | None = typer.Option(None, "--source", help="Explain one source file path."),
    artifact_path: str | None = typer.Option(None, "--artifact", help="Explain one artifact path."),
    diagnostic_code: str | None = typer.Option(None, "--diagnostic-code", help="Explain diagnostics with one code."),
    target_root: str | None = typer.Option(None, "--target-root", help="Only match an artifact under one target root."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print sources, artifacts, dependencies, diagnostics, and graph context for one target."""

    try:
        payload = open_project(path).inspect(
            "build-explain",
            profile=profile,
            module_id=module_id,
            collection_id=collection_id,
            source_path=source_path,
            artifact_path=artifact_path,
            diagnostic_code=diagnostic_code,
            target_root=target_root,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="target") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def families(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    family: str | None = typer.Option(None, "--family", help="Only include one family id."),
    kind: str | None = typer.Option(None, "--kind", help="Only include one compiler kind."),
    source_slot: str | None = typer.Option(None, "--source-slot", help="Only include families with one module source slot."),
    collection_source_slot: str | None = typer.Option(
        None,
        "--collection-source-slot",
        help="Only include families with one collection descriptor source slot.",
    ),
    sprite_slot: str | None = typer.Option(None, "--sprite-slot", help="Only include families with one sprite source slot."),
    route: str | None = typer.Option(None, "--route", help="Only include routed families with one route id."),
    artifact_type: str | None = typer.Option(
        None,
        "--artifact-type",
        help="Only include families and writers for one artifact type.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print registered build families for a local project profile."""

    try:
        payload = open_project(path).inspect(
            "families",
            profile=profile,
            family=family,
            kind=kind,
            source_slot=source_slot,
            collection_source_slot=collection_source_slot,
            sprite_slot=sprite_slot,
            route=route,
            artifact_type=artifact_type,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@hb_app.command("catalog-preview")
def hb_catalog_preview(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print deterministic HeavenBase-ready catalog rows without writing a workspace."""

    try:
        payload = open_project(path).inspect("catalog-preview", profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@hb_app.command("catalog-smoke")
def hb_catalog_smoke(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Validate preview rows in an in-memory HeavenBase workspace."""

    from paradev.hb import catalog_smoke

    try:
        payload = catalog_smoke(open_project(path), profile=profile)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@hb_app.command("catalog-write")
def hb_catalog_write(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Optional SQLite catalog path. Defaults to .paradev/.cache/hb/catalog.sqlite.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Write preview rows to a new local SQLite HeavenBase database."""

    from paradev.hb import catalog_write

    try:
        payload = catalog_write(open_project(path), profile=profile, database=database)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except FileExistsError as error:
        raise typer.BadParameter(str(error), param_hint="--database") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@hb_app.command("catalog-refresh")
def hb_catalog_refresh(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override the build profile. Defaults to the project game.",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Optional SQLite catalog path. Defaults to .paradev/.cache/hb/catalog.sqlite.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Replace the local SQLite HeavenBase catalog database."""

    from paradev.hb import catalog_refresh

    try:
        payload = catalog_refresh(open_project(path), profile=profile, database=database)
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except OSError as error:
        raise typer.BadParameter(str(error), param_hint="--database") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@hb_app.command("catalog-query")
def hb_catalog_query(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root or nested project path."),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Optional SQLite catalog path. Defaults to .paradev/.cache/hb/catalog.sqlite.",
    ),
    entity: str | None = typer.Option(None, "--entity", help="Only include one Catalog target entity."),
    target_id: str | None = typer.Option(None, "--target-id", help="Only include the exact target object identifier."),
    name: str | None = typer.Option(None, "--name", help="Only include Catalog rows whose name contains this text."),
    tag: str | None = typer.Option(None, "--tag", help="Only include Catalog rows with this tag."),
    limit: int = typer.Option(
        CATALOG_QUERY_DEFAULT_LIMIT,
        "--limit",
        min=1,
        max=CATALOG_QUERY_MAX_LIMIT,
        help=f"Maximum rows to return (default {CATALOG_QUERY_DEFAULT_LIMIT}, maximum {CATALOG_QUERY_MAX_LIMIT}).",
    ),
    offset: int = typer.Option(
        CATALOG_QUERY_DEFAULT_OFFSET,
        "--offset",
        min=0,
        help="Matching rows to skip before returning results.",
    ),
    include_data: bool = typer.Option(
        CATALOG_QUERY_DEFAULT_INCLUDE_DATA,
        "--data/--no-data",
        help="Hydrate one target payload; requires --limit 1.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Read rows from a written local SQLite HeavenBase catalog."""

    try:
        filters = normalize_catalog_query_filters(
            {
                "database": database,
                "entity": entity,
                "target_id": target_id,
                "name": name,
                "tag": tag,
                "limit": limit,
                "offset": offset,
                "include_data": include_data,
            }
        )
        payload = open_project(path).inspect(
            "catalog-query",
            **filters,
        )
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error), param_hint="--database") from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--limit/--offset/--data") from error
    _emit(payload, as_json=_json_enabled(ctx, json_output))


@app.command()
def project(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Project root path."),
    game: str | None = typer.Option(None, "--game", help="Override the target game package."),
    title: str | None = typer.Option(None, "--title", help="Optional display title."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Print a local project view."""

    try:
        view = open_project(path, game=game, title=title).to_view()
    except ProjectManifestError as error:
        raise typer.BadParameter(str(error), param_hint="path") from error
    _emit(view, as_json=_json_enabled(ctx, json_output))


@config_app.command("get")
def config_get_cmd(
    ctx: typer.Context,
    key: str | None = typer.Argument(None, help="Optional dotted config key."),
    scope: str | None = typer.Option(None, "--scope", help="Optional config scope."),
    raw_layer: bool = typer.Option(False, "--raw-layer", help="Read the raw scope layer instead of merged config."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """Read one config key or the full config."""

    _emit(
        config_get(key=key, scope=scope, merged=not raw_layer),
        as_json=_json_enabled(ctx, json_output),
    )


@config_app.command("list")
def config_list_cmd(
    ctx: typer.Context,
    prefix: str | None = typer.Option(None, "--prefix", help="Only include keys with this prefix."),
    scope: str | None = typer.Option(None, "--scope", help="Optional config scope."),
    raw_layer: bool = typer.Option(False, "--raw-layer", help="List the raw scope layer instead of merged config."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """List flattened config rows."""

    _emit(
        config_list(prefix=prefix, scope=scope, merged=not raw_layer),
        as_json=_json_enabled(ctx, json_output),
    )


@config_app.command("set")
def config_set_cmd(
    key: str = typer.Argument(..., help="Dotted config key."),
    value: str = typer.Argument(..., help="Config value."),
    scope: str | None = typer.Option(None, "--scope", help="Optional config scope."),
    parse: str = typer.Option("auto", "--parse", help="Value parser: auto, json, or raw."),
) -> None:
    """Set one config key."""

    if parse not in {"auto", "json", "raw"}:
        raise typer.BadParameter("parse must be one of: auto, json, raw")
    config_set(key, value, scope=scope, parse=parse)
    console.print(f"set {key}")


@config_app.command("unset")
def config_unset_cmd(
    key: str = typer.Argument(..., help="Dotted config key."),
    scope: str | None = typer.Option(None, "--scope", help="Optional config scope."),
) -> None:
    """Unset one config key."""

    config_unset(key, scope=scope)
    console.print(f"unset {key}")


@config_app.command("scopes")
def config_scopes_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """List stored config scopes."""

    _emit(config_scopes(), as_json=_json_enabled(ctx, json_output))


@config_app.command("history")
def config_history_cmd(
    ctx: typer.Context,
    scope: str | None = typer.Option(None, "--scope", help="Optional config scope."),
    limit: int = typer.Option(10, "--limit", help="Maximum history rows."),
    json_output: bool = typer.Option(False, "--json", help="Render structured output as JSON."),
) -> None:
    """List config history for a scope."""

    _emit(
        config_history(scope=scope, limit=limit),
        as_json=_json_enabled(ctx, json_output),
    )


def _project_rename_payload(previous: object, project: object) -> dict[str, object]:
    return {
        "schema": "paradev.project.rename.v1",
        "previous_title": str(getattr(previous, "title")),
        "project": project.to_view(),
    }


def _template_values(value_pairs: Sequence[str] | None) -> dict[str, str]:
    return _key_values(value_pairs, "Template value")


def _metadata_values(value_pairs: Sequence[str] | None) -> dict[str, str]:
    return _key_values(value_pairs, "Metadata value")


def _key_values(value_pairs: Sequence[str] | None, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in value_pairs or ():
        if "=" not in pair:
            raise ValueError(f"{label} {pair!r} must use KEY=VALUE.")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{label} {pair!r} must use a non-empty key.")
        values[key] = value
    return values


def _json_request_object(path: str | None, *, request_json: str | None = None, label: str = "JSON request") -> dict[str, object]:
    if path is not None and request_json is not None:
        raise ValueError(f"Pass only one {label.lower()} source: --request or --request-json.")
    if path is None and request_json is None:
        raise ValueError(f"Pass {label.lower()} with --request or --request-json.")
    payload = loads_json(request_json) if request_json is not None else _json_request_path_object(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return dict(payload)


def _module_batch_request_object(
    path: str | None,
    *,
    request_json: str | None = None,
    edit_json: Sequence[str] | None = None,
) -> dict[str, object]:
    inline_edits = list(edit_json or ())
    source_count = sum((path is not None, request_json is not None, bool(inline_edits)))
    if source_count > 1:
        raise ValueError("Pass only one module batch edit request source: --request, --request-json, or --edit-json.")
    if inline_edits:
        return {"edits": [_module_batch_edit_json_object(text, index) for index, text in enumerate(inline_edits)]}
    return _json_request_object(path, request_json=request_json, label="Module batch edit request")


def _module_batch_edit_json_object(text: str, index: int) -> dict[str, object]:
    payload = loads_json(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Module batch edit --edit-json {index} must be a JSON object.")
    return dict(payload)


def _json_request_array(body: dict[str, object], key: str, *, label: str) -> Sequence[object]:
    value = body.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{label} {key} must be an array.")
    return value


def _validate_json_request_fields(
    body: Mapping[str, object],
    allowed: frozenset[str],
    *,
    label: str,
) -> None:
    unexpected = sorted(str(key) for key in body if key not in allowed)
    if unexpected:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unexpected)}.")


def _json_request_path_object(path: str | None) -> object:
    if path == "-":
        return loads_json(typer.get_text_stream("stdin").read())
    if path is None:
        raise ValueError("JSON request path is required.")
    return load_json(str(Path(path).expanduser()), encoding="utf-8")


def _json_request_bool_default(body: dict[str, object], key: str, *, default: bool, label: str) -> bool:
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"", "0", "false", "no", "off"}:
            return False
    raise ValueError(f"{label} {key} must be a boolean.")


def _json_request_text_default(body: dict[str, object], key: str, *, default: str, label: str) -> str:
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} {key} must be a non-empty string.")
    return value.strip()


def _project_from_id(project_id: str, *, project_root: str | None = None) -> Project:
    if project_root:
        project = open_project(project_root)
        if project.project_id != project_id:
            raise ValueError(f"Project root {project_root!r} loaded project {project.project_id!r}, not {project_id!r}.")
        return project

    registry = registered_projects()
    projects = registry.get("projects", ())
    if isinstance(projects, list):
        for row in projects:
            if isinstance(row, dict) and row.get("project_id") == project_id and isinstance(row.get("root"), str):
                return open_project(row["root"])
    raise ValueError(f"Unknown project id: {project_id}")


def _module_batch_edit_message(payload: Mapping[str, object], *, verb: str) -> str:
    file_count = payload.get("file_count")
    created_count = payload.get("created_count")
    updated_count = payload.get("updated_count")
    changed_count = payload.get("changed_count")
    unchanged_count = payload.get("unchanged_count")
    if (
        isinstance(file_count, int)
        and isinstance(created_count, int)
        and isinstance(updated_count, int)
        and isinstance(changed_count, int)
        and isinstance(unchanged_count, int)
    ):
        return f"{verb} {file_count} module file(s): {changed_count} changed, {unchanged_count} unchanged; " f"{updated_count} updated, {created_count} created"
    if isinstance(file_count, int) and isinstance(created_count, int) and isinstance(updated_count, int):
        return f"{verb} {file_count} module file(s): {updated_count} updated, {created_count} created"
    return f"{verb} {file_count} module file(s)"


def _module_batch_create_message(payload: Mapping[str, object]) -> str:
    counts = payload.get("counts")
    count_values = counts if isinstance(counts, Mapping) else {}
    requested_count = payload.get("requested_count")
    plan_hash = payload.get("plan_hash")
    mode = "applied" if payload.get("applied") is True else "blocked" if payload.get("blocked") is True else "planned"
    create_label = "pending create" if mode == "blocked" else "create"
    summary = (
        f"{mode} {requested_count} module(s): "
        f"{count_values.get('create', 0)} {create_label}, "
        f"{count_values.get('created', 0)} created, "
        f"{count_values.get('unchanged', 0)} unchanged, "
        f"{count_values.get('blocked', 0)} blocked; "
        f"plan hash {plan_hash}"
    )
    diagnostics: list[object] = []
    payload_diagnostics = payload.get("diagnostics")
    if isinstance(payload_diagnostics, list):
        diagnostics.extend(payload_diagnostics)
    modules = payload.get("modules")
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, Mapping):
                continue
            module_diagnostics = module.get("diagnostics")
            if isinstance(module_diagnostics, list):
                diagnostics.extend(module_diagnostics)
    details: list[str] = []
    seen: set[tuple[str | None, str]] = set()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, Mapping):
            continue
        code = diagnostic.get("code")
        message = diagnostic.get("message")
        if not isinstance(message, str):
            continue
        key = (code if isinstance(code, str) else None, message)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(code, str):
            details.append(f"{code}: {message}")
        else:
            details.append(message)
    return f"{summary}; {' | '.join(details)}" if details else summary


def _lsp_text(text: str | None, text_file: str | None) -> str:
    if text is not None and text_file is not None:
        raise ValueError("--text and --text-file are mutually exclusive.")
    if text is not None:
        return text
    if text_file is not None:
        return Path(text_file).expanduser().read_text(encoding="utf-8")
    raise ValueError("Pass --text or --text-file.")


def build_app() -> typer.Typer:
    """Return the Typer app for tests or embedding."""

    return app


def _use_utf8_streams() -> None:
    """Make stdout and stderr carry UTF-8 whatever the console codepage is.

    CLI payloads are YAML or JSON built from project content, so they routinely hold
    characters no legacy codepage can encode. On a non-UTF-8 Windows locale a plain
    `print` of such a payload raises `UnicodeEncodeError`, and since that happens
    after the work is finished the command exits non-zero having actually succeeded.
    Redirecting to a file, piping, CI and MSYS shells all take that path; only a
    UTF-8-capable console escapes it.

    Streams already on UTF-8 are left alone, as is any stream that cannot be
    reconfigured (pytest capture, a replaced stdout).
    """

    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", None) or "").replace("-", "_").lower()
        if encoding in {"utf_8", "utf8"}:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - stream refuses
            continue


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ParaDev CLI."""

    _use_utf8_streams()
    args = list(argv) if argv is not None else None
    app(args=args)
    return 0
