"""
deploy_genie_space.py

Runs as a DABs postdeploy script.
Creates or updates a Genie space via the Databricks REST API.

Auth: uses DATABRICKS_HOST and either DATABRICKS_TOKEN or service-principal
OAuth env vars: DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET.
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode
import requests

# ── Config ────────────────────────────────────────────────────────────────────

def bundle_config():
    raw_config = os.environ.get("GENIE_BUNDLE_CONFIG")
    if not raw_config:
        return {}

    try:
        return json.loads(raw_config)
    except json.JSONDecodeError as exc:
        print(f"GENIE_BUNDLE_CONFIG is not valid JSON: {exc}")
        sys.exit(1)


def bundle_variable(config, name, default=None):
    value = config.get("variables", {}).get(name, {}).get("value")
    if value is None:
        value = config.get("variables", {}).get(name, {}).get("default")
    return value if value is not None else default


BUNDLE_CONFIG = bundle_config()
HOST         = os.environ["DATABRICKS_HOST"].rstrip("/")
BUNDLE_TARGET = os.environ.get(
    "BUNDLE_TARGET",
    BUNDLE_CONFIG.get("bundle", {}).get("target", "development"),
)
WAREHOUSE_ID = os.environ.get(
    "WAREHOUSE_ID",
    bundle_variable(BUNDLE_CONFIG, "warehouse_id", "81debc346ccf4458"),
)
PARENT_PATH  = os.environ.get(
    "GENIE_PARENT_PATH",
    bundle_variable(BUNDLE_CONFIG, "genie_parent_path", f"/Shared/Analytics"),
)
GENIE_CATALOG = os.environ.get("GENIE_CATALOG", "energy_production")
BASE_SPACE_TITLE = os.environ.get("GENIE_SPACE_TITLE", "Shelf Analytics")
SPACE_TITLE  = f"{BASE_SPACE_TITLE} {BUNDLE_TARGET}"
GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID")
GENIE_ORGANIZATIONS = os.environ.get(
    "GENIE_ORGANIZATIONS",
    bundle_variable(BUNDLE_CONFIG, "organizations"),
)
DASHBOARD_PATH = (
    Path(__file__).resolve().parent.parent
    / "client"
    / "dashboards"
    / "shelf_analytics.lvdash.json"
)

MEASURE_EXPRESSION_RE = re.compile(
    r"\b(MEASURE|SUM|COUNT|AVG|MIN|MAX|MIN_BY|MAX_BY|ANY_VALUE)\s*\(",
    re.IGNORECASE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_databricks_token():
    token = os.environ.get("DATABRICKS_TOKEN")
    if token:
        return token

    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
    if client_id and client_secret:
        return get_oauth_token(client_id, client_secret)

    print(
        "Databricks auth is not configured. Set DATABRICKS_TOKEN or both "
        "DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET."
    )
    sys.exit(1)


def get_oauth_token(client_id, client_secret):
    try:
        metadata = requests.get(
            f"{HOST}/oidc/.well-known/oauth-authorization-server",
            timeout=30,
        )
        metadata.raise_for_status()
        token_endpoint = metadata.json()["token_endpoint"]

        token_response = requests.post(
            token_endpoint,
            auth=(client_id, client_secret),
            data={
                "grant_type": "client_credentials",
                "scope": "all-apis",
            },
            timeout=30,
        )
        token_response.raise_for_status()
    except requests.RequestException as exc:
        print("Databricks OAuth token lookup failed.")
        print(exc)
        sys.exit(1)

    return token_response.json()["access_token"]


def headers():
    return {
        "Authorization": f"Bearer {get_databricks_token()}",
        "Content-Type": "application/json",
    }


def api(method, path, body=None):
    url = f"{HOST}{path}"
    resp = requests.request(method, url, headers=headers(), json=body)
    if not resp.ok:
        print(f"API error {resp.status_code}: {resp.text}")
        sys.exit(1)
    if not resp.content:
        return {}
    return resp.json()


def api_ok(method, path, body=None):
    url = f"{HOST}{path}"
    resp = requests.request(method, url, headers=headers(), json=body)
    if resp.ok:
        if not resp.content:
            return True, {}
        return True, resp.json()
    return False, resp.text


def paginated_api(path, collection_key):
    results = []
    page_token = None

    while True:
        page_path = path
        if page_token:
            separator = "&" if "?" in path else "?"
            page_path = f"{path}{separator}{urlencode({'page_token': page_token})}"

        response = api("GET", page_path)
        results.extend(response.get(collection_key, []))

        page_token = response.get("next_page_token")
        if not page_token:
            return results


def short_id():
    """Generate a lowercase 32-hex ID as required by the Genie API."""
    return uuid.uuid4().hex


def slug_alias(display_name):
    return re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")


def expression_slug_alias(display_name):
    return re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")


def is_measure_expression(expression):
    if not expression:
        return False

    # Lakeview calculated dimensions are stored beside measures. Only aggregate
    # or MEASURE()-based calculated columns should become Genie measures.
    return bool(MEASURE_EXPRESSION_RE.search(expression))


def shelf_metrics_columns():
    try:
        dashboard = json.loads(DASHBOARD_PATH.read_text())
    except OSError as exc:
        print(f"Failed to read dashboard JSON at {DASHBOARD_PATH}: {exc}")
        sys.exit(1)

    shelf_dataset = next(
        (
            dataset
            for dataset in dashboard.get("datasets", [])
            if dataset.get("name") == "shelf_metrics"
        ),
        None,
    )
    if not shelf_dataset:
        print("Dashboard JSON does not contain a shelf_metrics dataset.")
        sys.exit(1)

    return shelf_dataset.get("columns", [])


def dashboard_grain_flags():
    return [
        {
            "id": short_id(),
            "alias": "catalog_once",
            "display_name": "Catalog Once",
            "synonyms": ["unique product flag", "catalog row flag"],
            "sql": [
                "ROW_NUMBER() OVER (PARTITION BY catalog_uuid ORDER BY 1) = 1"
            ]
        },
        {
            "id": short_id(),
            "alias": "subsection_once",
            "display_name": "Subsection Once",
            "synonyms": ["unique subsection flag", "outlet row flag"],
            "sql": [
                "ROW_NUMBER() OVER (PARTITION BY subsection_uuid ORDER BY 1) = 1"
            ]
        },
        {
            "id": short_id(),
            "alias": "result_once",
            "display_name": "Result Once",
            "synonyms": ["unique image flag", "image row flag"],
            "sql": [
                "ROW_NUMBER() OVER (PARTITION BY result_uuid ORDER BY 1) = 1"
            ]
        },
        {
            "id": short_id(),
            "alias": "promo_once",
            "display_name": "Promo Once",
            "synonyms": ["unique promo flag", "promotion row flag"],
            "sql": [
                "CASE WHEN fs.catalog_uuid='Unknown' OR fs.price_uuid='Unknown' THEN 1=1 ELSE ROW_NUMBER() OVER (PARTITION BY fs.subsection_uuid,fs.catalog_uuid,fs.price_uuid ORDER BY 1)=1 END"
            ]
        },
        {
            "id": short_id(),
            "alias": "packaging_group_once",
            "display_name": "Packaging Group Once",
            "synonyms": ["unique packaging group flag", "packaging group flag"],
            "sql": [
                "ROW_NUMBER() OVER (PARTITION BY result_uuid, catalog_uuid, roi_id ORDER BY 1) = 1"
            ]
        }
    ]


def script_extra_measure_snippets():
    return [
        {
            "id": short_id(),
            "alias": "unique_products",
            "display_name": "Unique Products",
            "synonyms": ["unique skus", "product count", "sku count"],
            "sql": ["SUM(1) FILTER (WHERE catalog_once)"]
        },
        {
            "id": short_id(),
            "alias": "images_processed",
            "display_name": "Images Processed",
            "synonyms": ["image count", "processed images"],
            "sql": ["SUM(1) FILTER (WHERE result_once)"]
        },
        {
            "id": short_id(),
            "alias": "unique_promos",
            "display_name": "Unique Promos",
            "synonyms": ["unique promos", "promo count"],
            "sql": ["SUM(1) FILTER (WHERE promo_once)"]
        },
        {
            "id": short_id(),
            "alias": "unique_subsections",
            "display_name": "Unique Subsections",
            "synonyms": ["unique subsections", "subsections count"],
            "sql": ["SUM(1) FILTER (WHERE subsection_once)"]
        }
    ]


def dashboard_measure_snippets():
    measures = []
    for column in shelf_metrics_columns():
        display_name = column.get("displayName")
        expression = column.get("expression")
        if not display_name or not is_measure_expression(expression):
            continue

        measure = {
            "id": short_id(),
            "alias": slug_alias(display_name),
            "display_name": display_name,
            "sql": [expression],
        }
        measures.append(measure)

    return measures


def dashboard_expression_snippets():
    expressions = []
    for column in shelf_metrics_columns():
        display_name = column.get("displayName")
        expression = column.get("expression")
        if not display_name or not expression or is_measure_expression(expression):
            continue

        expressions.append(
            {
                "id": short_id(),
                "alias": expression_slug_alias(display_name),
                "display_name": display_name,
                "sql": [expression],
            }
        )

    return expressions


def join_spec(left, right, condition, relationship_type, comment, instruction):
    return {
        "id": short_id(),
        "left": left,
        "right": right,
        "sql": [
            condition,
            f"--rt=FROM_RELATIONSHIP_TYPE_{relationship_type}--",
        ],
        "comment": [comment],
        "instruction": [instruction],
    }


def normalize_workspace_path(path):
    return path.rstrip("/") if path else path


def workspace_api_path(path):
    path = normalize_workspace_path(path)
    if path and path.startswith("/Workspace/"):
        return path[len("/Workspace") :]
    return path


def ensure_workspace_dir(path):
    path = normalize_workspace_path(path)
    if not path:
        return

    print(f"Ensuring Genie parent path exists: {path!r}.")
    ok, response = api_ok("POST", "/api/2.0/workspace/mkdirs", {"path": path})
    if ok:
        return

    api_path = workspace_api_path(path)
    if api_path != path:
        print(
            "Workspace mkdirs rejected the configured path; retrying with "
            f"Workspace API path {api_path!r}."
        )
        ok, response = api_ok(
            "POST",
            "/api/2.0/workspace/mkdirs",
            {"path": api_path},
        )
        if ok:
            return

    print(f"Failed to create Genie parent path {path!r}: {response}")
    sys.exit(1)


def space_parent_path(space):
    for key in ("parent_path", "folder_path"):
        value = space.get(key)
        if value:
            return normalize_workspace_path(value)

    workspace_path = normalize_workspace_path(space.get("workspace_path"))
    if workspace_path:
        title = space.get("title")
        if title and workspace_path.endswith(f"/{title}"):
            return workspace_path[: -(len(title) + 1)]
        return workspace_path

    return None


def path_matches(candidate_path, expected_path):
    if not candidate_path:
        return False

    candidate_path = normalize_workspace_path(candidate_path)
    expected_path = normalize_workspace_path(expected_path)
    return candidate_path == expected_path or candidate_path.startswith(
        f"{expected_path}/"
    )


def get_space(space_id, include_serialized_space=False):
    path = f"/api/2.0/genie/spaces/{space_id}"
    if include_serialized_space:
        path = f"{path}?include_serialized_space=true"
    response = api("GET", path)
    return response.get("space", response)


def find_space_by_path_and_title(parent_path, title):
    expected_parent_path = normalize_workspace_path(parent_path)
    spaces = paginated_api("/api/2.0/genie/spaces", "spaces")
    title_matches = [space for space in spaces if space.get("title") == title]
    print(
        f"Looking for Genie space titled {title!r} under {expected_parent_path!r}; "
        f"found {len(title_matches)} title match(es)."
    )

    matches = []
    unresolved = []
    for listed_space in title_matches:
        space_id = listed_space.get("space_id")
        if not space_id:
            continue

        detailed_space = get_space(space_id)
        candidate = {**listed_space, **detailed_space}
        candidate_parent_path = space_parent_path(candidate)
        print(
            "Candidate Genie space: "
            f"id={space_id}, title={candidate.get('title')!r}, "
            f"parent_path={candidate_parent_path!r}, "
            f"workspace_path={candidate.get('workspace_path')!r}"
        )

        if path_matches(candidate_parent_path, expected_parent_path):
            matches.append(candidate)
        elif candidate_parent_path is None:
            unresolved.append(candidate)

    if len(matches) > 1:
        print(
            f"Found multiple Genie spaces at {expected_parent_path!r} titled "
            f"{title!r}; refusing to guess. Set GENIE_SPACE_ID explicitly."
        )
        for space in matches:
            print(f"- {space.get('space_id')}")
        sys.exit(1)

    if matches:
        return matches[0]

    if len(unresolved) == 1 and len(title_matches) == 1:
        print(
            "Genie API did not return a parent path for the matching space; "
            "using the only space with the requested title."
        )
        return unresolved[0]

    if unresolved:
        print(
            f"Found Genie spaces titled {title!r}, but the API did not expose "
            "a parent path to disambiguate them. Set GENIE_SPACE_ID explicitly."
        )
        for space in unresolved:
            print(f"- {space.get('space_id')}")
        sys.exit(1)

    return None


def find_space(parent_path, title, space_id=None):
    if space_id:
        space = get_space(space_id)
        print(f"Using GENIE_SPACE_ID={space_id}.")
        return {**space, "space_id": space_id}

    return find_space_by_path_and_title(parent_path, title)


def set_permissions(space_id):
    print("Setting permissions...")
    api("PATCH", f"/api/2.0/permissions/genie/{space_id}", {
        "access_control_list": [
            {"group_name": "account users", "permission_level": "CAN_RUN"},
        ]
    })


def existing_serialized_space(space_id):
    current_space = get_space(space_id, include_serialized_space=True)
    serialized_space = current_space.get("serialized_space")
    if not serialized_space:
        print(
            f"Existing Genie space {space_id} did not return serialized_space. "
            "Refusing to patch because UI edits could be lost."
        )
        sys.exit(1)

    return serialized_space


def script_sql_snippets(snippet_type, catalog):
    generated_space = json.loads(build_serialized_space(catalog))
    return generated_space["instructions"]["sql_snippets"][snippet_type]


def merge_script_sql_snippets(serialized_space, snippet_types, catalog):
    space = json.loads(serialized_space)
    sql_snippets = space.setdefault("instructions", {}).setdefault(
        "sql_snippets", {}
    )

    for snippet_type in snippet_types:
        snippets = sql_snippets.setdefault(snippet_type, [])
        snippets_by_alias = {
            snippet.get("alias"): snippet for snippet in snippets if snippet.get("alias")
        }
        snippets_by_display_name = {
            snippet.get("display_name"): snippet
            for snippet in snippets
            if snippet.get("display_name")
        }

        merged_count = 0
        for script_snippet in script_sql_snippets(snippet_type, catalog):
            existing_snippet = snippets_by_alias.get(
                script_snippet.get("alias")
            ) or snippets_by_display_name.get(script_snippet.get("display_name"))

            if existing_snippet:
                existing_snippet.update(
                    {
                        key: value
                        for key, value in script_snippet.items()
                        if key != "id"
                    }
                )
            else:
                snippets.append(script_snippet)
                snippets_by_alias[script_snippet.get("alias")] = script_snippet
                snippets_by_display_name[
                    script_snippet.get("display_name")
                ] = script_snippet
            merged_count += 1

        print(f"Merged {merged_count} script-defined SQL {snippet_type}.")

    sort_instructions(space)
    return json.dumps(space)


def sort_data_sources(space):
    data_sources = space.get("data_sources", {})
    for table in data_sources.get("tables", []):
        if "column_configs" in table:
            table["column_configs"].sort(key=lambda column: column.get("column_name", ""))

    for source_type in ("tables", "metric_views"):
        data_sources.get(source_type, []).sort(
            key=lambda source: source.get("identifier", "")
        )


def sort_instructions(space):
    config = space.get("config", {})
    config.get("sample_questions", []).sort(
        key=lambda question: question.get("id", "")
    )

    instructions = space.get("instructions", {})
    for instruction_type in (
        "example_question_sqls",
        "join_specs",
        "text_instructions",
    ):
        instructions.get(instruction_type, []).sort(
            key=lambda instruction: instruction.get("id", "")
        )

    sql_snippets = instructions.get("sql_snippets", {})
    for snippet_type in ("expressions", "filters", "measures"):
        sql_snippets.get(snippet_type, []).sort(
            key=lambda snippet: snippet.get("id", "")
        )


# ── Genie space definition ────────────────────────────────────────────────────

def build_serialized_space(catalog):
    """
    Defines the full Genie space config:
    - data sources (tables + metric views)
    - instructions
    - example SQL queries
    - join relationships
    - SQL snippets (measures, filters, expressions)
    """
    gold = f"{catalog}.gold"
    space = {
        "version": 2,
        "config": {
            "sample_questions": []
        },
        "data_sources": {
            "tables": [
                {
                    "identifier": f"{gold}.facing",
                    "description": [
                        "Dashboard filter spine. One row per catalog, result, shelf, "
                        "and price combination after non-promo annotations are grouped. "
                        "Use this as the starting table for questions that must respect "
                        "brand, SKU, subsection, city, or date filters."
                    ],
                    "column_configs": [
                        {
                            "column_name": "catalog_uuid",
                            "enable_format_assistance": True,
                            "synonyms": ["catalog id"]
                        },
                        {
                            "column_name": "subsection_uuid",
                            "enable_format_assistance": True
                        },
                        {
                            "column_name": "result_uuid",
                            "enable_format_assistance": True,
                            "synonyms": ["image id"]
                        },
                        {
                            "column_name": "realogram_item_shelf_id",
                            "enable_format_assistance": True,
                            "synonyms": ["shelf row"]
                        },
                        {
                            "column_name": "result_dt_created",
                            "enable_format_assistance": True,
                            "synonyms": ["visit date"]
                        }
                    ]
                },
                {
                    "identifier": f"{gold}.catalog",
                    "description": [
                        "Product catalog dimension. One row per catalog_uuid and product "
                        "attributes such as catalog_name, catalog_brand, flavor, size, "
                        "container type, and annotation_type."
                    ]
                },
                {
                    "identifier": f"{gold}.subsection",
                    "description": [
                        "Outlet and campaign subsection dimension. One row per "
                        "subsection_uuid with subsection_name, city, region, country_code, "
                        "latitude, longitude, retail outlet, and campaign name."
                    ],
                    "column_configs": [
                        {
                            "column_name": "subsection_name",
                            "enable_format_assistance": True,
                            "enable_entity_matching": True,
                            "synonyms": ["Outlet and campaign name"]
                        },
                        {
                            "column_name": "city",
                            "enable_format_assistance": True,
                            "enable_entity_matching": True,
                            "synonyms": ["region"]
                        }
                    ]
                },
                {
                    "identifier": f"{gold}.shelf",
                    "description": [
                        "Shelf grain table. One row per result_uuid and realogram_item_shelf_id. Contains known, unknown, gap, "
                        "distinct facing, distinct brand, and unknown-in-shelf metrics."
                    ]
                },
                {
                    "identifier": f"{gold}.image",
                    "description": [
                        "Image grain table. One row per catalog_uuid, result_uuid, roi_id and "
                        "result_dt_created. Use for images processed, eye-level SKU, and "
                        "distinct facings or brands per image."
                    ]
                },
                {
                    "identifier": f"{gold}.promo",
                    "description": [
                        "Promotion grain table. One row per catalog_uuid, subsection_uuid, "
                        "and price_uuid. Contains price, quantity, promo date range, promo "
                        "duration, promo count, and crop fields for strip, poster, and promo "
                        "annotations."
                    ]
                }
            ],
            "metric_views": []
        },
        "instructions": {
            "text_instructions": [
                {
                    "id": short_id(),
                    "content": [
                        "This space mirrors the Shelf Analytics Lakeview dashboard for ",
                        f"{catalog}. ",
                        "Use gold.facing as the filtered spine, then join to catalog, ",
                        "subsection, shelf, image, and promo at their natural grains. ",
                        "Do not join all gold tables first and then count raw rows; that ",
                        "will fan out shelf, image, subsection, catalog, and promo metrics. ",
                        "Use ROW_NUMBER()=1 once flags like catalog_once, subsection_once, ",
                        "result_once, promo_once, and packaging_group_once when counting entities from ",
                        "the joined dashboard-shaped dataset. "
                    ]
                }
            ],
            "example_question_sqls": [

            ],
            "join_specs": [
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.catalog", "alias": "catalog"},
                    "`facing`.`catalog_uuid` = `catalog`.`catalog_uuid`",
                    "MANY_TO_ONE",
                    "Join facing rows to product catalog attributes.",
                    "Use this join for brand, SKU, flavor, size, and product metadata.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.subsection", "alias": "subsection"},
                    "`facing`.`subsection_uuid` = `subsection`.`subsection_uuid`",
                    "MANY_TO_ONE",
                    "Join facing rows to outlet and campaign subsection attributes.",
                    "Use this join for subsection, city, region, outlet, campaign, and map questions.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.shelf", "alias": "shelf"},
                    "`facing`.`result_uuid` = `shelf`.`result_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to shelf rows on result UUID.",
                    "Use with the other facing-to-shelf join conditions for shelf-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.shelf", "alias": "shelf"},
                    "`facing`.`realogram_item_shelf_id` = `shelf`.`realogram_item_shelf_id`",
                    "MANY_TO_MANY",
                    "Join facing rows to shelf rows on realogram shelf ID.",
                    "Use with the other facing-to-shelf join conditions for shelf-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.shelf", "alias": "shelf"},
                    "`facing`.`result_dt_created` = `shelf`.`result_dt_created`",
                    "MANY_TO_MANY",
                    "Join facing rows to shelf rows on result timestamp.",
                    "Use with the other facing-to-shelf join conditions for shelf-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.image", "alias": "image"},
                    "`facing`.`catalog_uuid` = `image`.`catalog_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to image rows on catalog UUID.",
                    "Use with the other facing-to-image join conditions for image-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.image", "alias": "image"},
                    "`facing`.`result_uuid` = `image`.`result_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to image rows on result UUID.",
                    "Use with the other facing-to-image join conditions for image-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.image", "alias": "image"},
                    "`facing`.`result_dt_created` = `image`.`result_dt_created`",
                    "MANY_TO_MANY",
                    "Join facing rows to image rows on result timestamp.",
                    "Use with the other facing-to-image join conditions for image-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.image", "alias": "image"},
                    "`facing`.`roi_id` = `image`.`roi_id`",
                    "MANY_TO_MANY",
                    "Join facing rows to image rows on roi_id of Bay ID.",
                    "Use with the other facing-to-image join conditions for image-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.promo", "alias": "promo"},
                    "`facing`.`catalog_uuid` = `promo`.`catalog_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to promo rows on catalog UUID.",
                    "Use with the other facing-to-promo join conditions for promo-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.promo", "alias": "promo"},
                    "`facing`.`subsection_uuid` = `promo`.`subsection_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to promo rows on subsection UUID.",
                    "Use with the other facing-to-promo join conditions for promo-level metrics.",
                ),
                join_spec(
                    {"identifier": f"{gold}.facing", "alias": "facing"},
                    {"identifier": f"{gold}.promo", "alias": "promo"},
                    "`facing`.`price_uuid` = `promo`.`price_uuid`",
                    "MANY_TO_MANY",
                    "Join facing rows to promo rows on price UUID.",
                    "Use with the other facing-to-promo join conditions for promo-level metrics.",
                ),
            ],
            "sql_snippets": {
                "measures": script_extra_measure_snippets() + dashboard_measure_snippets(),
                "filters": [],
                "expressions": dashboard_grain_flags() + dashboard_expression_snippets()
            }
        }
    }
    sort_data_sources(space)
    sort_instructions(space)

    # API expects serialized_space as an escaped JSON string
    return json.dumps(space)


# ── Main ──────────────────────────────────────────────────────────────────────

def catalog_for_org(org):
    if org.get("catalog"):
        return org["catalog"]

    org_name = org.get("org_name")
    if not org_name:
        print(f"Organization entry is missing org_name: {org}")
        sys.exit(1)

    return f"{org_name}_{BUNDLE_TARGET}"


def title_for_org(org):
    org_name = org.get("org_name")
    if not org_name:
        print(f"Organization entry is missing org_name: {org}")
        sys.exit(1)

    return f"{BASE_SPACE_TITLE} {org_name} {BUNDLE_TARGET}"


def deployment_targets():
    if not GENIE_ORGANIZATIONS:
        return [
            {
                "catalog": GENIE_CATALOG,
                "title": SPACE_TITLE,
                "space_id": GENIE_SPACE_ID,
            }
        ]

    try:
        organizations = json.loads(GENIE_ORGANIZATIONS)
    except json.JSONDecodeError as exc:
        print(f"GENIE_ORGANIZATIONS is not valid JSON: {exc}")
        sys.exit(1)

    if not isinstance(organizations, list):
        print("GENIE_ORGANIZATIONS must be a JSON array.")
        sys.exit(1)

    if GENIE_SPACE_ID and len(organizations) != 1:
        print("GENIE_SPACE_ID can only be used when deploying one Genie space.")
        sys.exit(1)

    targets = []
    for org in organizations:
        if not isinstance(org, dict):
            print(f"Organization entry must be an object: {org}")
            sys.exit(1)

        targets.append(
            {
                "catalog": catalog_for_org(org),
                "title": title_for_org(org),
                "space_id": GENIE_SPACE_ID,
            }
        )

    return targets


def deploy_space(target):
    catalog = target["catalog"]
    title = target["title"]
    ensure_workspace_dir(PARENT_PATH)
    payload = {
        "title":            title,
        "description":      (
            "Shelf Analytics Genie space aligned to the Lakeview dashboard: "
            "share of shelf, promo, realogram, execution, and operations KPIs"
        ),
        "warehouse_id":     WAREHOUSE_ID,
        "parent_path":      PARENT_PATH,
    }

    print(f"Deploying Genie space {title!r} for catalog {catalog!r}.")
    existing_space = find_space(PARENT_PATH, title, target.get("space_id"))
    if existing_space:
        space_id = existing_space["space_id"]
        print(
            f"Found existing Genie space {space_id} at {PARENT_PATH!r} titled "
            f"{title!r}."
        )
        payload["serialized_space"] = merge_script_sql_snippets(
            existing_serialized_space(space_id),
            ("expressions", "measures"),
            catalog,
        )
        print(
            "Updating existing space while preserving current serialized_space "
            "and merging script-defined SQL expressions and measures."
        )
        result = api("PATCH", f"/api/2.0/genie/spaces/{space_id}", payload)
        print(f"Updated Genie space: {result.get('space_id')}")
    else:
        print("Creating Genie space...")
        payload["serialized_space"] = build_serialized_space(catalog)
        result = api("POST", "/api/2.0/genie/spaces", payload)
        space_id = result["space_id"]
        print(f"Created Genie space: {space_id}")

    set_permissions(space_id)
    print(f"Done. Space URL: {HOST}/genie/rooms/{space_id}")


def main():
    for target in deployment_targets():
        deploy_space(target)


if __name__ == "__main__":
    main()
