#!/usr/bin/env python3
"""
MCP server for the Meta Ad Library API (ads_archive).

Exposes read-only tools for searching and inspecting publicly archived ads
(the same data shown at https://www.facebook.com/ads/library/), including
the EU/DSA transparency fields Meta added for ads reaching EU countries.

Authentication is a single Meta App access token, read from the
META_AD_LIBRARY_ACCESS_TOKEN environment variable. The token is never
accepted as a tool argument, so it never appears in model context or logs.
Meta's API embeds this same token in every `ad_snapshot_url` it returns;
this server strips it back out of tool responses before they reach the
model (see `_strip_url_token`).
"""

import json
import os
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

mcp = MCPServer("meta_ad_library_mcp", instructions=__doc__)

GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "v26.0")
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
ACCESS_TOKEN_ENV_VAR = "META_AD_LIBRARY_ACCESS_TOKEN"
REQUEST_TIMEOUT = 30.0

# This deployment is for a single-country (Hungary) internal use case, so
# ad_reached_countries defaults here instead of being required on every call.
# Override via env if this server is ever reused for another market.
DEFAULT_COUNTRY = os.environ.get("META_AD_LIBRARY_DEFAULT_COUNTRY", "HU")

# Fields that work regardless of ad_type or ad_reached_countries.
CORE_FIELDS = [
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
]

# DSA transparency fields Meta added for ads reaching the EU/UK, available
# for any ad_type (not just political ads) when the ad reached those regions.
EU_TRANSPARENCY_FIELDS = [
    "eu_total_reach",
    "age_country_gender_reach_breakdown",
    "beneficiary_payers",
    "target_ages",
    "target_gender",
    "target_locations",
    "total_reach_by_location",
]

# Only populated for ad_type=POLITICAL_AND_ISSUE_ADS; requesting them for
# other ad types is allowed but the values will be empty/null.
POLITICAL_ONLY_FIELDS = [
    "bylines",
    "currency",
    "spend",
    "impressions",
    "estimated_audience_size",
    "demographic_distribution",
    "delivery_by_region",
]

DEFAULT_FIELDS = CORE_FIELDS + EU_TRANSPARENCY_FIELDS
ALL_FIELDS = CORE_FIELDS + EU_TRANSPARENCY_FIELDS + POLITICAL_ONLY_FIELDS


class AdActiveStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ALL = "ALL"


class AdType(str, Enum):
    ALL = "ALL"
    EMPLOYMENT_ADS = "EMPLOYMENT_ADS"
    FINANCIAL_PRODUCTS_AND_SERVICES_ADS = "FINANCIAL_PRODUCTS_AND_SERVICES_ADS"
    HOUSING_ADS = "HOUSING_ADS"
    POLITICAL_AND_ISSUE_ADS = "POLITICAL_AND_ISSUE_ADS"


class MediaType(str, Enum):
    ALL = "ALL"
    IMAGE = "IMAGE"
    MEME = "MEME"
    VIDEO = "VIDEO"
    NONE = "NONE"


class SearchType(str, Enum):
    KEYWORD_UNORDERED = "KEYWORD_UNORDERED"
    KEYWORD_EXACT_PHRASE = "KEYWORD_EXACT_PHRASE"


class PublisherPlatform(str, Enum):
    FACEBOOK = "FACEBOOK"
    INSTAGRAM = "INSTAGRAM"
    AUDIENCE_NETWORK = "AUDIENCE_NETWORK"
    MESSENGER = "MESSENGER"
    WHATSAPP = "WHATSAPP"
    OCULUS = "OCULUS"
    THREADS = "THREADS"
    STREAMING_SERVICES = "STREAMING_SERVICES"


class FieldSet(str, Enum):
    """Convenience presets for the `fields` argument."""

    DEFAULT = "default"  # core + EU transparency fields
    ALL = "all"  # everything, including political-only fields
    CORE = "core"  # minimal creative/delivery fields only


class SearchAdsInput(BaseModel):
    """Input model for searching the Meta Ad Library."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    ad_reached_countries: List[str] = Field(
        default_factory=lambda: [DEFAULT_COUNTRY],
        min_length=1,
        description=(
            f"ISO 3166-1 alpha-2 country codes the ads must have reached, e.g. ['HU', 'DE', 'FR']. "
            f"Defaults to ['{DEFAULT_COUNTRY}'] since this deployment is for Hungary. Pass other "
            "codes to search elsewhere. AVOID ['ALL']: for general (non-political) ads it has been "
            "observed to silently return zero results even when matching ads exist -- pass explicit "
            "country codes instead. ['ALL'] does appear to work for ad_type=POLITICAL_AND_ISSUE_ADS."
        ),
    )
    search_terms: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Keyword search (max 100 chars). Matches ad creative text and, in practice, "
            "advertiser/Page names -- this is the primary way to search by company or "
            "brand name (e.g. 'Nike', 'H&M'). Space-separated terms are ANDed together. "
            "Leave empty to browse by country/date/type filters alone."
        ),
    )
    search_page_ids: Optional[List[str]] = Field(
        default=None,
        max_length=10,
        description="Up to 10 specific Facebook Page IDs to restrict the search to, if already known.",
    )
    search_type: Optional[SearchType] = Field(
        default=None,
        description="KEYWORD_UNORDERED (default, any order) or KEYWORD_EXACT_PHRASE.",
    )
    ad_active_status: AdActiveStatus = Field(
        default=AdActiveStatus.ACTIVE,
        description="Filter by whether the ad is currently ACTIVE, INACTIVE, or ALL.",
    )
    ad_type: AdType = Field(
        default=AdType.ALL,
        description=(
            "Restrict to a special ad category, or ALL (default) for every ad type. "
            "Note: spend/impressions/demographic_distribution/bylines/estimated_audience_size "
            "are only ever populated for POLITICAL_AND_ISSUE_ADS."
        ),
    )
    ad_delivery_date_min: Optional[str] = Field(
        default=None, description="Only ads delivered on/after this date, format YYYY-MM-DD."
    )
    ad_delivery_date_max: Optional[str] = Field(
        default=None, description="Only ads delivered on/before this date, format YYYY-MM-DD."
    )
    languages: Optional[List[str]] = Field(
        default=None, description="Filter by ISO 639-1 language codes found in the ad (plus 'cmn', 'yue')."
    )
    media_type: Optional[MediaType] = Field(default=None, description="Filter by media type present in the ad.")
    publisher_platforms: Optional[List[PublisherPlatform]] = Field(
        default=None, description="Restrict to ads that appeared on these Meta platforms."
    )
    bylines: Optional[List[str]] = Field(
        default=None,
        description="Filter by 'paid for by' disclaimer byline text. Political/issue ads only.",
    )
    estimated_audience_size_min: Optional[int] = Field(
        default=None, description="Minimum estimated audience size. Political/issue ads only."
    )
    estimated_audience_size_max: Optional[int] = Field(
        default=None, description="Maximum estimated audience size. Political/issue ads only."
    )
    unmask_removed_content: bool = Field(
        default=False, description="If true, reveal content Meta removed for policy violations."
    )
    fields: FieldSet = Field(
        default=FieldSet.DEFAULT,
        description=(
            "Which result fields to return. 'default' = creative/delivery info plus EU DSA "
            "transparency fields (reach, targeting, beneficiary_payers). 'core' = minimal "
            "creative/delivery fields only. 'all' = everything, including the political-ads-only "
            "spend/impressions/demographic fields (null for non-political ads)."
        ),
    )
    limit: int = Field(default=25, ge=1, le=250, description="Max number of ads to return in this page.")
    after: Optional[str] = Field(
        default=None,
        description="Pagination cursor from a previous search_ads call's `next_cursor`, to fetch the next page.",
    )

    @field_validator("ad_reached_countries", "languages", "bylines")
    @classmethod
    def _uppercase_codes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        return [item.strip() for item in v]


class GetAdByIdInput(BaseModel):
    """Input model for fetching a single archived ad by its Ad Library ID."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    ad_id: str = Field(..., min_length=1, description="The Ad Library numeric ID, e.g. '123456789012345'.")
    fields: FieldSet = Field(
        default=FieldSet.ALL,
        description="Which fields to return. Defaults to 'all' since a single-ad lookup is cheap.",
    )


def _resolve_fields(field_set: FieldSet) -> str:
    mapping = {
        FieldSet.CORE: CORE_FIELDS,
        FieldSet.DEFAULT: DEFAULT_FIELDS,
        FieldSet.ALL: ALL_FIELDS,
    }
    return ",".join(mapping[field_set])


def _strip_url_token(url: str) -> str:
    """Remove the embedded `access_token` query param Meta puts on ad_snapshot_url.

    Meta's ads_archive response embeds the same access token used to make the
    request in every ad's snapshot link. Since that token is long-lived and
    tied to a real identity-verified person, leaving it in tool output would
    put a live secret into model context/conversation history on every call.
    Testing has also shown the link's viewability depends on the browser
    being logged into the correct Facebook account, not on this token, so
    stripping it costs no functionality.
    """
    parts = urlsplit(url)
    filtered_query = [(k, v) for k, v in parse_qsl(parts.query) if k != "access_token"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered_query), parts.fragment))


def _sanitize_ad(ad: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(ad.get("ad_snapshot_url"), str):
        ad["ad_snapshot_url"] = _strip_url_token(ad["ad_snapshot_url"])
    return ad


SNAPSHOT_URL_NOTE = (
    "ad_snapshot_url only renders while the browser is logged into the Facebook account "
    "backing this server's configured access token (a personal, identity-verified account). "
    "A blank or 'Not Found' page when opened logged out, or logged into a different account, "
    "is expected and is not a failed search or a broken link."
)

SEARCH_TERMS_NOTE = (
    "search_terms is a loose keyword match against ad copy (ad_creative_bodies) and page_name, "
    "not an exact company filter -- short or common terms can pull in unrelated advertisers. "
    "Before presenting results as 'ads from <company>', discard any ad whose page_name doesn't "
    "actually match the target. It also does NOT search beneficiary_payers/legal funding entity "
    "names: searching a legal entity rather than its public Page name can return zero results "
    "even when that entity funds ads on some Page -- to find those, search the known Page/brand "
    "name(s) instead, or fetch a broader result set and inspect each ad's beneficiary_payers "
    "field. Once you have a confirmed page_id, prefer search_page_ids over search_terms for "
    "exact, reliable follow-up queries."
)

ZERO_RESULTS_WITH_ALL_NOTE = (
    "This query used ad_reached_countries=['ALL'] with a non-political ad_type and returned zero "
    "results. That combination has been observed to silently return nothing even when matching "
    "ads exist -- it is not reliable evidence the advertiser has no ads running. Retry with "
    f"explicit country codes (e.g. ['{DEFAULT_COUNTRY}']) before concluding there are no results."
)


def _get_access_token() -> str:
    token = os.environ.get(ACCESS_TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"Missing {ACCESS_TOKEN_ENV_VAR}. Set it in the MCP server's env config "
            "with a valid Meta App access token that has Ad Library access."
        )
    return token


async def _graph_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Shared GET helper for the Graph API. Injects the access token; never returns it."""
    query = {k: v for k, v in params.items() if v is not None}
    query["access_token"] = _get_access_token()

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{GRAPH_API_BASE_URL}/{path}", params=query, timeout=REQUEST_TIMEOUT)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(_format_graph_error(e)) from e

    return response.json()


def _format_graph_error(e: httpx.HTTPStatusError) -> str:
    status = e.response.status_code
    try:
        body = e.response.json()
        message = body.get("error", {}).get("message", e.response.text)
        code = body.get("error", {}).get("code")
        subcode = body.get("error", {}).get("error_subcode")
    except ValueError:
        message = e.response.text
        code = None
        subcode = None

    if subcode == 2332002 or (code == 10 and "ads/library/api" in message):
        return (
            "Error: WRONG TOKEN TYPE for the Ad Library API (not a retriable request problem). "
            f"{ACCESS_TOKEN_ENV_VAR} is set to a token that Meta rejected with "
            f"'{message}'. The /ads_archive endpoint only accepts a personal, long-lived User "
            "access token from an identity- and location-verified individual Facebook account "
            "(the verification at facebook.com/ID, also required for political ads). System User "
            "tokens, App tokens, and Page tokens are never valid here, no matter what permissions "
            "they have -- this is true even if the same token works fine for other Marketing API "
            "tools (ad accounts, campaigns, insights). "
            "Do NOT retry with different parameters, countries, or search terms -- that will not "
            "fix it. A human needs to generate a new personal long-lived User token from a "
            "verified account via the Graph API Explorer and update the server config. "
            "Surface this explanation to the user instead of retrying."
        )
    if status == 401 or code == 190:
        return (
            f"Error: Meta API rejected the access token (401). It may be expired, revoked, or "
            f"missing Ad Library access. Update {ACCESS_TOKEN_ENV_VAR} in your MCP config. "
            f"Details: {message}"
        )
    if code == 613 or status == 429:
        return f"Error: Meta API rate limit exceeded (code 613). Wait a bit before retrying. Details: {message}"
    if status == 400:
        return (
            "Error: Meta API rejected the request (400), usually an invalid parameter combination "
            "(e.g. a bad country code, or requesting a political-ads-only field with an "
            f"incompatible ad_type). Details: {message}"
        )
    return f"Error: Meta API request failed with status {status}. Details: {message}"


@mcp.tool(
    name="search_ads",
    annotations={
        "title": "Search Meta Ad Library",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def search_ads(params: SearchAdsInput) -> str:
    """Search the public Meta Ad Library (ads_archive) for ads matching filters.

    Use this to find ads by advertiser/company/Page name (via `search_terms`),
    by country, by ad type (all ads, or the special categories: housing,
    employment, financial products, political/issue), by date range, language,
    platform, or media type. This is the same public data available at
    https://www.facebook.com/ads/library/ -- no ad-account access is required,
    only a valid Meta App access token with Ad Library permissions.

    For ads reaching EU countries, results include DSA transparency fields:
    total EU reach, targeting criteria, and `beneficiary_payers` (who paid for
    and who benefits from the ad) -- useful for identifying who is actually
    funding an ad campaign.

    IMPORTANT -- `search_terms` is a loose keyword match, not an exact company
    filter: it matches ad copy and page_name text, can pull in unrelated
    advertisers on short/common terms, and does NOT search beneficiary_payers
    or other legal/funding entity names. When a request asks for ads from one
    specific company, do not present raw results as final: discard any ad
    whose page_name doesn't actually match the target, and once you have a
    confirmed page_id, re-query with search_page_ids for an exact filter. If
    searching a legal entity's name returns zero results, that does not mean
    the entity has no ads -- search its known Page/brand name(s) instead, or
    pull a broader result set and inspect beneficiary_payers per ad. See the
    `notes` field in the response for a reminder of this at query time.

    IMPORTANT -- `ad_reached_countries` defaults to Hungary (['HU']) for this
    deployment; pass other ISO country codes to search elsewhere. Avoid
    ['ALL']: for general (non-political) ads it has been observed to silently
    return zero results even when matching ads exist, always pass explicit
    country codes for that ad_type instead.

    Args:
        params (SearchAdsInput): Validated search filters. All fields are
            optional (ad_reached_countries defaults to Hungary); narrow further
            with search_terms, ad_type, date range, etc. as needed.

    Returns:
        str: JSON-formatted string with the schema:
        {
            "count": int,                # number of ads in this page
            "ads": [ {..fields requested..} ],  # ad_snapshot_url has its access_token stripped
            "next_cursor": str | null,    # pass as `after` to fetch the next page
            "has_more": bool,
            "notes": [str]                # caveats relevant to this specific query, if any
        }

        Or "Error: <message>" if the request failed (e.g. invalid access token,
        rate limit, or bad parameter combination) -- the message explains the
        likely cause and how to fix it.

    Examples:
        - "What ads is Company X running in Hungary right now?" -> search_terms="Company X",
          ad_reached_countries=["HU"], ad_active_status="ACTIVE" -- then filter results to
          page_name matches before answering.
        - "Show political ads in Germany from the last 30 days" -> ad_reached_countries=["DE"],
          ad_type="POLITICAL_AND_ISSUE_ADS", ad_delivery_date_min="<30 days ago>"
        - Don't use when you already have a specific ad's ID -- use get_ad_by_id instead.
    """
    try:
        query = {
            "ad_reached_countries": json.dumps(params.ad_reached_countries),
            "search_terms": params.search_terms,
            "search_page_ids": json.dumps(params.search_page_ids) if params.search_page_ids else None,
            "search_type": params.search_type.value if params.search_type else None,
            "ad_active_status": params.ad_active_status.value,
            "ad_type": params.ad_type.value,
            "ad_delivery_date_min": params.ad_delivery_date_min,
            "ad_delivery_date_max": params.ad_delivery_date_max,
            "languages": json.dumps(params.languages) if params.languages else None,
            "media_type": params.media_type.value if params.media_type else None,
            "publisher_platforms": json.dumps([p.value for p in params.publisher_platforms])
            if params.publisher_platforms
            else None,
            "bylines": json.dumps(params.bylines) if params.bylines else None,
            "estimated_audience_size_min": params.estimated_audience_size_min,
            "estimated_audience_size_max": params.estimated_audience_size_max,
            "unmask_removed_content": params.unmask_removed_content,
            "fields": _resolve_fields(params.fields),
            "limit": params.limit,
            "after": params.after,
        }

        data = await _graph_get("ads_archive", query)

        ads = [_sanitize_ad(ad) for ad in data.get("data", [])]
        cursors = data.get("paging", {}).get("cursors", {})
        next_cursor = cursors.get("after") if data.get("paging", {}).get("next") else None

        notes = []
        if params.search_terms:
            notes.append(SEARCH_TERMS_NOTE)
        if ads:
            notes.append(SNAPSHOT_URL_NOTE)
        if not ads and "ALL" in params.ad_reached_countries and params.ad_type != AdType.POLITICAL_AND_ISSUE_ADS:
            notes.append(ZERO_RESULTS_WITH_ALL_NOTE)

        result = {
            "count": len(ads),
            "ads": ads,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "notes": notes,
        }
        return json.dumps(result, indent=2)

    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Error: Unexpected error occurred: {type(e).__name__}: {e}"


@mcp.tool(
    name="get_ad_by_id",
    annotations={
        "title": "Get Ad Library Ad by ID",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_ad_by_id(params: GetAdByIdInput) -> str:
    """Fetch full details for a single archived ad by its Ad Library ID.

    Use this when you already have an ad's numeric Library ID (from a prior
    search_ads call, or from an ad_snapshot_url / facebook.com/ads/library
    link) and want its complete data, including EU transparency and (if
    political) spend/impressions fields.

    Args:
        params (GetAdByIdInput): Validated input containing:
            - ad_id (str): The Ad Library numeric ID.
            - fields (FieldSet): Which fields to return, defaults to 'all'.

    Returns:
        str: JSON-formatted string of the ad object (its `ad_snapshot_url`, if
        present, has the access_token stripped -- see notes below), or
        "Error: <message>" if the ad doesn't exist, isn't accessible, or the
        request failed.

    Examples:
        - Use when: you found an ad via search_ads and want its full targeting/reach detail.
        - Don't use when: you don't have a specific ad ID yet -- use search_ads instead.

    Note:
        If the returned ad_snapshot_url shows a blank/"Not Found" page when
        opened, that's expected unless the browser is logged into the
        Facebook account backing this server's configured access token --
        it is not a sign the ad_id was wrong.
    """
    try:
        data = _sanitize_ad(await _graph_get(params.ad_id, {"fields": _resolve_fields(params.fields)}))
        return json.dumps(data, indent=2)
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        return f"Error: Unexpected error occurred: {type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
