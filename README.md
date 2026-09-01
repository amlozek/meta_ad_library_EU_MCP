# Meta Ad Library MCP Server

A Claude-compatible MCP server for searching and inspecting the Meta Ad Library
(the public archive behind https://www.facebook.com/ads/library/). Built for
EU use: it defaults to returning the DSA transparency fields Meta added for
ads reaching the EU (total reach, targeting, and `beneficiary_payers` - who
pays for and who benefits from an ad).

It uses only the public Ad Library API with a Meta App access token - no ad
account access, no Business Manager permissions needed.

This deployment defaults every search to Hungary (`ad_reached_countries`
defaults to `["HU"]` if not specified). Override
`META_AD_LIBRARY_DEFAULT_COUNTRY` in the server's env config to change the
default, or pass `ad_reached_countries` explicitly per call to search
elsewhere.

## Tools

- **search_ads** - search by country (defaults to Hungary), keyword/company/
  Page name, ad type (all ads, or the special categories: housing,
  employment, financial products, political/issue), date range, language,
  platform, media type, and more. Supports pagination.
- **get_ad_by_id** - fetch full detail for one ad by its Ad Library ID.

Both tools are read-only and always hit the live API (no local caching).

## Prerequisites

- Python 3.10+
- A Meta App, and a **personal long-lived User access token** for it (see
  "Token requirements" below - this is not the same token type used by other
  Marketing API tools).

## Token requirements (important, read before debugging auth errors)

`/ads_archive` has a different authorization model than standard Marketing
API endpoints (ad accounts, campaigns, insights). It only accepts a
**personal, long-lived User access token from an identity- and
location-verified individual Facebook account** - the same verification
required to run political/social-issue ads, completed at
`facebook.com/ID`.

**A System User token will NOT work here**, even if it never expires and
works fine for other Marketing API calls. System Users aren't real people
and can't complete the identity verification this endpoint requires. Calls
with a System User (or App, or Page) token fail with:

```json
{
  "error": {
    "message": "Application does not have permission for this action",
    "type": "OAuthException",
    "code": 10,
    "error_subcode": 2332002,
    "error_user_msg": "To access the API, you'll need to follow the steps at facebook.com/ads/library/api."
  }
}
```

If you run other Meta MCP tools (e.g. a Marketing API server) against a
System User token, that token stays as-is and unaffected - you need a
**second, separate** token just for this server.

### Generating the correct token

1. **Confirm identity first (one-time).** Log into the personal Facebook
   account that will own this token, go to `facebook.com/ID`, and complete
   identity + location confirmation (government ID, address). Can take 1-2
   days to clear; skip if already verified.
2. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
   and log in with that same verified personal account.
3. Select your app in the top-right "Meta App" dropdown.
4. In "User or Page", choose **User Token** (not Page Token, not App Token,
   not a System User).
5. Leave permissions default - `/ads_archive` doesn't require `ads_read` or
   any special scope.
6. Click "Generate Access Token" and complete the login prompt.
7. **Exchange it for a long-lived token immediately** (short-lived tokens
   expire in ~1 hour): click "Open in Access Token Debugger" -> "Extend
   Access Token", or call:
   ```
   GET https://graph.facebook.com/v26.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id={app-id}
     &client_secret={app-secret}
     &fb_exchange_token={short-lived-token}
   ```
   This returns a token valid for roughly 60 days.
8. Verify it in the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/):
   `Type` should be `User`, `User ID` should match the verified account,
   `Expires` should be ~60 days out, no System User flag.
9. Put this token in `META_AD_LIBRARY_ACCESS_TOKEN` in the config below.

**This token expires in ~60 days and cannot be made permanent** - set a
reminder around day 50-55 to repeat steps 6-9 (identity verification itself,
once done, doesn't need repeating unless it lapses).

If a tool call ever returns a "wrong token type" error, that's this exact
issue - regenerate the token per the steps above rather than retrying with
different search parameters, changing parameters will not fix it.

## Setup

From the repo root:

```bash
python -m venv .venv
```

**Windows (PowerShell):**

PowerShell blocks running scripts by default, which includes the venv's own
activation script. Before activating, allow scripts for just this terminal
session (this does not change any system-wide or user-wide policy, and
reverts once you close the window):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

Then activate the environment and install dependencies:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS/Linux:**
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure Claude

Add this to your Claude Desktop config (`claude_desktop_config.json`), or the
equivalent MCP config for your Claude client. Replace the `command` path if
your checkout lives somewhere else, and replace
`YOUR_META_AD_LIBRARY_ACCESS_TOKEN_HERE` with your actual access token.

**Windows:**
```json
{
  "mcpServers": {
    "meta-ad-library": {
      "command": "C:\\Users\\user\\Documents\\GitHub\\meta_ad_library_EU_MCP\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\user\\Documents\\GitHub\\meta_ad_library_EU_MCP\\meta_ad_library_mcp\\server.py"
      ],
      "env": {
        "META_AD_LIBRARY_ACCESS_TOKEN": "YOUR_META_AD_LIBRARY_ACCESS_TOKEN_HERE",
        "META_GRAPH_API_VERSION": "v26.0"
      }
    }
  }
}
```

**macOS/Linux:**
```json
{
  "mcpServers": {
    "meta-ad-library": {
      "command": "/absolute/path/to/meta_ad_library_EU_MCP/.venv/bin/python",
      "args": [
        "/absolute/path/to/meta_ad_library_EU_MCP/meta_ad_library_mcp/server.py"
      ],
      "env": {
        "META_AD_LIBRARY_ACCESS_TOKEN": "YOUR_META_AD_LIBRARY_ACCESS_TOKEN_HERE",
        "META_GRAPH_API_VERSION": "v26.0"
      }
    }
  }
}
```

The token is only ever read from this environment variable server-side - it
is never a tool argument, so it's never visible to the model or logged in
conversation.

`META_GRAPH_API_VERSION` is hard-coded to `v26.0` above rather than left
unset, so the config always pins an explicit, known-good version instead of
silently trailing whatever the server code's fallback default happens to be.
**Graph API versions get deprecated over time** (roughly every two years) -
if calls start failing with a deprecated-version error, check the current
supported versions at
https://developers.facebook.com/docs/graph-api/changelog/ and bump this
value accordingly.

Restart Claude Desktop after editing the config.

## Notes on fields

- `search_terms` matches ad creative text and page_name, but loosely - see
  "Search accuracy" below before treating results as ads from one specific
  company.
- `spend`, `impressions`, `demographic_distribution`, `bylines`, and
  `estimated_audience_size` are only ever populated for
  `ad_type=POLITICAL_AND_ISSUE_ADS`; requesting them for other ad types
  returns null.
- `eu_total_reach`, `age_country_gender_reach_breakdown`,
  `beneficiary_payers`, `target_ages`, `target_gender`, `target_locations`,
  and `total_reach_by_location` are DSA transparency fields available for any
  ad type, populated when the ad reached the EU/UK.

## Avoid `ad_reached_countries: ["ALL"]` for general ads

Testing found that searching with `ad_reached_countries: ["ALL"]` can
silently return zero results for `ad_type=ALL` (general, non-political ads)
even when matching ads genuinely exist and turn up immediately once an
explicit country code is used instead. `["ALL"]` does appear to work for
`ad_type=POLITICAL_AND_ISSUE_ADS`. Because of this, always pass real country
codes for general ad searches - which is also why this server defaults to
`["HU"]` rather than requiring the caller to specify a country. If a
`search_ads` call with `["ALL"]` and a non-political ad_type comes back
empty, the response's `notes` field will flag this rather than treating it
as a confirmed "no ads found."

## Search accuracy: `search_terms` is fuzzy, not an exact filter

Testing found that `search_terms` is a loose keyword match against ad copy
and `page_name`, not a precise "ads from this company" filter:

- Short or common words can pull in a broad set of unrelated advertisers
  along with the intended one (e.g. searching a two-word brand name where
  one word is also a common business term can return mostly unrelated
  results).
- It does **not** search `beneficiary_payers` or other legal/funding entity
  names. A company can be the confirmed real-money payer behind a Page's ads
  (visible in that Page's `beneficiary_payers` field) and still return zero
  results when searched by that legal entity's name directly - the legal
  entity and the public-facing Page name are indexed differently.

To get results tied to one specific company:

1. Run `search_ads` with `search_terms` set to the brand/company name, and
   treat the results as a candidate list, not a final answer.
2. Discard any result whose `page_name` doesn't actually match the target
   before presenting it as "ads from `<company>`".
3. If you're looking for a legal/funding entity rather than a public Page,
   and a direct name search returns nothing, that does not mean it has no
   ads running - search its known Page/brand name(s) instead, or pull a
   broader country-level result set and check each ad's `beneficiary_payers`
   field for the target entity.
4. Once you have a confirmed `page_id`, prefer `search_page_ids` (accepts up
   to 10 IDs) over `search_terms` for follow-up queries - it's a hard filter,
   not a fuzzy text match.

`search_ads` responses include a `notes` array that resurfaces this guidance
whenever `search_terms` was used, as a reminder at the point results are
being interpreted.

## `ad_snapshot_url` and "Not Found" pages

Meta's API embeds a live access token in every `ad_snapshot_url` it returns.
This server strips that token out of tool responses before they reach the
model or get logged, since it's the same long-lived personal token used for
every call and shouldn't end up sitting in conversation history.

Independent of that, opening an `ad_snapshot_url` link only renders the ad
while the browser tab is logged into the Facebook account backing this
server's configured token (the identity-verified personal account) - not
logged in, or logged into a different account, shows a blank/"Not Found"
page instead of an error. This is expected and isn't something a retry or a
different query will fix. It can also fail for ads that have since been
paused, rejected, or removed, which is more likely for older
`ad_delivery_start_time` values.

There's no plain, session-independent static image/asset URL available from
this API - don't rely on `ad_snapshot_url` for downstream automation (e.g.
scraping ad images) beyond manual viewing while logged in correctly.
