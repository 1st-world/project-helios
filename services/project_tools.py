"""Declare the fixed local MCP project-reading contract shared by host and server."""

from copy import deepcopy

from jsonschema import Draft202012Validator, ValidationError

from services.project_access import ProjectReadError

PROTOCOL_VERSION = "2026-07-28"
SERVER_VERSION = "0.1"
TOOL_NAMES = ("list_files", "search_text", "read_file")
INTEGER = {"type": "integer", "minimum": 0}
LINE = {"type": "integer", "minimum": 1}
VERSION = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
POSITION = {
    "type": "object", "properties": {"line": LINE, "column": INTEGER, "byte_offset": INTEGER},
    "required": ["line", "column", "byte_offset"], "additionalProperties": False,
}
ITEM_SCHEMAS = {
    "list_files": {
        "type": "object", "properties": {"path": {"type": "string"},
                                             "type": {"enum": ["file", "directory"]}},
        "required": ["path", "type"], "additionalProperties": False,
    },
    "search_text": {
        "type": "object", "properties": {
            "path": {"type": "string"}, "line": LINE, "column": INTEGER,
            "text": {"type": "string"}, "snippet_column": INTEGER,
            "snippet_is_partial": {"type": "boolean"}, "file_version": VERSION,
        },
        "required": ["path", "line", "column", "text", "snippet_column",
                     "snippet_is_partial", "file_version"], "additionalProperties": False,
    },
    "read_file": {
        "type": "object", "properties": {
            "line": LINE, "column": INTEGER, "byte_start": INTEGER,
            "byte_end": INTEGER, "text": {"type": "string"},
        },
        "required": ["line", "column", "byte_start", "byte_end", "text"],
        "additionalProperties": False,
    },
}
COUNTS = {"type": "object", "propertyNames": {"pattern": "^[a-z_]+$"},
          "additionalProperties": INTEGER}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_version": {"const": "0.1"},
        "scope": {"type": "string"},
        "items": {"type": "array", "maxItems": 256,
                  "items": {"oneOf": list(ITEM_SCHEMAS.values())}},
        "next_cursor": {"type": ["string", "null"], "pattern": "^[a-f0-9]{32}$"},
        "complete": {"type": "boolean"}, "omissions": COUNTS,
        "bytes_processed": INTEGER,
        "file_version": VERSION, "order": {"enum": ["filesystem", "traversal"]},
        "requested_range": {
            "type": "object", "properties": {"start_line": LINE, "line_count": LINE},
            "required": ["start_line", "line_count"], "additionalProperties": False,
        },
        "reached_eof": {"type": "boolean"},
        "next_position": {"anyOf": [POSITION, {"type": "null"}]},
        "omission_totals": COUNTS,
        "policy": {"type": "string"},
        "error": {"type": "object", "properties": {"code": {"type": "string", "pattern": "^[a-z_]+$"}},
                  "required": ["code"], "additionalProperties": False},
    },
    "required": ["contract_version"], "additionalProperties": False,
    "oneOf": [
        {"required": ["scope", "items", "next_cursor", "complete", "omissions",
                      "bytes_processed", "order", "policy", "omission_totals"], "not": {"required": ["error"]}},
        {"required": ["error"], "not": {"required": ["items"]}},
    ],
}


def tool_definitions() -> list[dict]:
    """Return fresh fixed schemas with no host-path or permission-expansion parameter."""
    common = {
        "path": {"type": "string", "default": ".", "maxLength": 4096},
        "cursor": {"type": ["string", "null"], "default": None,
                   "pattern": "^[a-f0-9]{32}$"},
    }
    definitions = []
    descriptions = {
        "list_files": "List one directory in filesystem order. Follow next_cursor; this is not a recursive project tree.",
        "search_text": "Search visible, nonignored UTF-8 project files for a case-sensitive literal. Follow empty progress pages too.",
        "read_file": "Read a UTF-8 line range as bounded fragments. Columns are zero-based Unicode positions; byte ranges exclude the end offset.",
    }
    for name in TOOL_NAMES:
        properties = deepcopy(common)
        required = []
        if name == "read_file":
            properties.update({
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            })
            required = ["path"]
        else:
            properties["page_size"] = {
                "type": "integer", "minimum": 1,
                "maximum": 256 if name == "list_files" else 128,
                "default": 100 if name == "list_files" else 20,
            }
        if name == "search_text":
            properties["query"] = {"type": "string", "minLength": 1, "maxLength": 4096}
            required = ["query"]
        definitions.append({
            "name": name, "description": descriptions[name],
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False},
            "outputSchema": deepcopy(OUTPUT_SCHEMA),
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": False, "openWorldHint": False},
        })
    return definitions


def validate_arguments(name: str, arguments: dict) -> None:
    """Reject unknown tools and arguments before sending or executing an MCP request."""
    definition = next((item for item in tool_definitions() if item["name"] == name), None)
    if definition is None:
        raise ProjectReadError("unknown_tool")
    try:
        Draft202012Validator(definition["inputSchema"]).validate(arguments)
    except ValidationError:
        raise ProjectReadError("invalid_arguments") from None


def validate_result(name: str, arguments: dict, payload: dict, normalize) -> None:
    """Check returned ranges and relative paths before retaining a local tool result."""
    try:
        Draft202012Validator(OUTPUT_SCHEMA).validate(payload)
        if "error" in payload:
            if set(payload) != {"contract_version", "error"}:
                raise ValueError()
            return
        scope = normalize(arguments.get("path", "."))
        if (payload["scope"] != scope
                or payload["complete"] != (payload["next_cursor"] is None)
                or payload["order"] != ("filesystem" if name == "list_files" else "traversal")):
            raise ValueError()
        for item in payload["items"]:
            Draft202012Validator(ITEM_SCHEMAS[name]).validate(item)
            if "path" in item:
                path = normalize(item["path"])
                if path != item["path"] or path == ".":
                    raise ValueError()
                if scope != "." and path != scope and not path.startswith(scope + "/"):
                    raise ValueError()
                relative = path if scope == "." else path[len(scope) + 1:]
                if name == "list_files" and (path == scope or "/" in relative):
                    raise ValueError()
            if name == "search_text":
                offset = item["column"] - item["snippet_column"]
                query = arguments["query"]
                if offset < 0 or item["text"][offset:offset + len(query)] != query:
                    raise ValueError()
            if name == "read_file":
                start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
                if (not start <= item["line"] < start + count
                        or item["byte_end"] - item["byte_start"] != len(item["text"].encode("utf-8"))):
                    raise ValueError()
        if name == "read_file":
            if (not {"file_version", "requested_range", "reached_eof", "next_position"} <= payload.keys()
                    or payload["requested_range"] != {
                        "start_line": arguments.get("start_line", 1),
                        "line_count": arguments.get("line_count", 200),
                    }):
                raise ValueError()
        elif {"file_version", "requested_range", "reached_eof", "next_position"} & payload.keys():
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, ValidationError):
        raise ProjectReadError("mcp_invalid_tool_result") from None
