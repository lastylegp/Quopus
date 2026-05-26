"""Assembly64 browser - query the hackerswithstyle.se backend for
C64 releases aggregated from CSDB, HVSC, c64.org, OneLoad64,
Gamebase64 and others.

The API lives at https://hackerswithstyle.se/leet/* and is what the
Ultimate firmware (>= 3.11) and the sandlbn/ultimate64-manager
desktop app talk to via Fredrik Aaberg's public service. Thanks!

This module:
- ASM64Client: thin wrapper around the search / detail / download
  endpoints. Endpoints are best-effort guesses based on observed
  behavior of the Ultimate firmware and the official Manager.
  The exact path names may vary across server versions; if a path
  is wrong, ASM64Client logs it and falls back to alternatives.
- ASM64BrowserDialog: PyQt6 modal browser with filter form, results
  table, details panel, and run/mount/download actions.
- favorites: JSON file in CONFIG_DIR/asm64_favorites.json
- saved searches: JSON file in CONFIG_DIR/asm64_searches.json

Path-tuning tips for the user:
- If a search returns HTTP 404, click "Endpoints..." in the dialog
  toolbar and override the path templates by hand. The dialog
  remembers the override in quopus.cfg under asm64_endpoints.
- The 'Host not in allowlist' (HTTP 403) error means your IP is
  not whitelisted by hackerswithstyle.se. This is rare for normal
  home connections but happens for cloud / VPN ranges.

This module intentionally has zero hard dependencies on Qt at
module-load time. The Qt UI imports happen lazily inside
ASM64BrowserDialog so the API client can be tested headless.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional
from .config import scaled_font_px


# Default endpoints. Override via ASM64Client(base=..., paths=...) if
# the server moves them around. Inferred from the Ultimate firmware
# and the sandlbn/ultimate64-manager Rust source - exact form may
# vary by server version.
DEFAULT_BASE = "https://hackerswithstyle.se/leet"

# Endpoints discovered from the Ultimate 64 firmware's assembly.cc:
#   #define URL_SEARCH    "/leet/search/aql?query="
#   #define URL_PATTERNS  "/leet/search/aql/presets"
#   #define URL_ENTRIES   "/leet/search/entries"
#   #define URL_DOWNLOAD  "/leet/search/bin"
# AQL = Assembly Query Language, e.g. (name:"jumpman") & (type:prg)
# The U64 firmware identifies itself with:
#   User-Agent: Assembly Query
#   Client-Id: Ultimate
# Quopus uses its own identification (Quopus Commander / Quopus)
# so the backend can apply Quopus-specific routing or quota.
# The Client-Id header may be relevant for server-side filtering.
DEFAULT_PATHS = {
    # Search via AQL query string. The whole AQL goes after ?query=
    # and gets URL-encoded.
    "search":         "/search/aql",
    # Static preset list (categories, types, sources, ...) returned
    # as a single JSON blob.
    "presets":        "/search/aql/presets",
    # Files inside a release. URL is /search/entries/{id}/{cat}
    "entries":        "/search/entries",
    # Binary download. URL is /search/bin/{id}/{cat}/{idx}  OR
    #                          /search/bin/{path}/{filename}
    "download":       "/search/bin",
    # Top-rated entries per category - returns the server's
    # pre-computed top list (typically Top 200). URL form is
    # /charts/{category} where category is the aqlKey from the
    # /presets 'category' section ("demos", "games", "music",
    # "intros", "graphics", "tools", "mags", "charts", "bbs",
    # ...). Path confirmed by the Assembly64 dev team. Empty
    # category gives the overall Top 200.
    "charts":         "/charts",
}


# Pre-baked fallback preset lists - used when the server-driven
# /categories etc. endpoints fail. Mirror what the U64 firmware menu
# shows + what the Manager hardcodes. The server's response is
# preferred when available.
# Legacy preset list - kept around for callers that import it, but
# the dialog no longer uses it. The real preset list comes from
# /search/aql/presets when needed.
FALLBACK_PRESETS = {
    "types": [
        "",   # = any
        "Game",
        "Demo",
        "Music",
        "Tool",
        "Magazine",
        "Intro",
        "Crack",
        "Graphics",
    ],
    "sources": [
        "",   # = any
        "CSDB",
        "HVSC",
        "OneLoad64",
        "Gamebase64",
        "c64.org",
        "c64.com",
    ],
    "ratings": [
        ("", "Any rating"),
        ("1", "Stars >= 1"),
        ("3", "Stars >= 3"),
        ("5", "Stars >= 5"),
        ("7", "Stars >= 7"),
        ("9", "Stars >= 9"),
    ],
    "sorts": [
        ("", "Most relevant"),
        ("name", "Name (A-Z)"),
        ("year", "Year (newest first)"),
        ("year_asc", "Year (oldest first)"),
        ("rating", "Rating (highest first)"),
        ("recent", "Recently added"),
    ],
    "recency": [
        ("", "Any time"),
        ("1d", "Last 24 hours"),
        ("7d", "Last week"),
        ("30d", "Last month"),
        ("365d", "Last year"),
    ],
}


# CSDB release-type ids - what Assembly64 stores in the
# `siteCategory` field of every search result. These ids come
# from CSDB.dk's release database (the same as what their
# advancedresult.php?rrelease_type[]=N filter accepts).
#
# Confirmed values (verified by URL-probing csdb.dk):
#    20 = "C64 Crack"
#
# The other names come from the CSDB documentation + the
# csdb-downloader project's default Types list and the meta
# descriptions on real CSDB release pages. Numeric ids are best-
# guess based on CSDB's standard release_type ordering - if a
# user reports a wrong mapping, just fix the entry here. Unknown
# ids show through as "Type #N" instead of breaking.
_CSDB_RELEASE_TYPES = {
    # User-confirmed values (from csdb.dk advanced search URLs):
    1:  "C64 Demo",
    2:  "C64 One-File Demo",
    3:  "C64 Intro",
    4:  "C64 4K Intro",
    5:  "C64 Crack Intro",
    6:  "REU Release",
    7:  "C64 Music",
    8:  "C64 Music Collection",
    9:  "C64 Graphics",
    10: "C64 Graphics Collection",
    11: "C64 Game",
    13: "C64 Diskmag",
    14: "C64 Charts",
    15: "C64 Tool",
    16: "C64 Invitation",
    17: "C64 Misc",
    18: "C64 1K Intro",
    19: "C64 Game Preview",
    20: "C64 Crack",
    22: "C64 Basic Demo",
    24: "C64 Fake Demo",
    25: "C64 Tool Collection",
    42: "BBS Software",
    43: "BBS Graphic",
    44: "C64 Intro Collection",
    46: "EasyFlash Release",
    # Other ids can be added as users report them - until then
    # the resolver returns "Type #N" and the background learner
    # will fetch them from csdb.dk on demand.
}


# Hardcoded fallback for the Assembly64 /presets 'category'
# section, used only when /presets is unreachable. Captured
# live from hackerswithstyle.se circa 2026-05. These are the
# 13 filter-bucket names that Assembly64 exposes in its UI -
# NOT the per-release content type (that's siteCategory above).
_DEFAULT_CATEGORY_NAMES = {
    # aqlKey (string) -> friendly name. The filter combobox
    # and the AQL builder use these as the source of truth.
    "demos":     "Demos",
    "games":     "Games",
    "intros":    "Intros",
    "c128":      "C128",
    "bbs":       "BBS",
    "charts":    "Charts",
    "mags":      "Mags",
    "easyflash": "Easyflash",
    "graphics":  "Graphics",
    "misc":      "Misc",
    "music":     "Music",
    "reu":       "Reu",
    "tools":     "Tools",
}


@dataclass
class ASM64Entry:
    """One row in the search-result list.

    Fields match the JSON returned by /search/aql. The server's
    `category` field is normally a STRING aqlKey ('csdb',
    'gamebase', 'demo', ...) on modern Assembly64 servers.
    Older firmwares sent an int id. We accept either and store
    the raw form in `category` (typed as object to allow both),
    plus a resolved display name in `category_name` if the
    server already provided one - some response shapes inline
    the friendly name next to the key, others require a
    /presets lookup.
    """
    id: str = ""
    name: str = ""
    group: str = ""
    handle: str = ""
    year: int = 0
    category: object = ""       # aqlKey string OR int id - raw
    category_name: str = ""     # display name if pre-resolved
    site_category: int = 0      # CSDB-style category code
    rating: int = 0
    site_rating: int = 0
    updated: str = ""
    released: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, j: dict) -> "ASM64Entry":
        def gi(*keys, default=0):
            for k in keys:
                if k in j and j[k] is not None:
                    try:
                        return int(j[k])
                    except (TypeError, ValueError):
                        pass
            return default

        def gs(*keys, default=""):
            for k in keys:
                if k in j and j[k] is not None:
                    return str(j[k])
            return default

        # Category: keep RAW form. Modern server sends e.g.
        # "csdb" or "demo"; older servers sent an int. We store
        # whichever we got - the renderer feeds it to
        # get_category_name() which handles both cases.
        cat_raw = j.get("category")
        if cat_raw is None:
            cat_raw = ""
        # Some response shapes include the resolved display name
        # under a sibling field. Try common names.
        cat_name = ""
        for nk in ("categoryName", "category_name",
                    "categoryLabel", "categoryDescription"):
            if nk in j and j[nk]:
                cat_name = str(j[nk]).strip()
                break

        # Handle field: server's /search/aql sends ONE primary
        # handle per release as a plain string. Multiple credits
        # are not in the search response - if we need them we
        # have to fetch /webservice/?type=release&id=N from CSDB
        # which has the full <Credits> block. The detail panel
        # does that lazily.
        handle_raw = j.get("handle")
        handle_str = ""
        if handle_raw is None or handle_raw == "":
            handle_str = ""
        elif isinstance(handle_raw, list):
            parts = []
            for h in handle_raw:
                if isinstance(h, str):
                    parts.append(h.strip())
                elif isinstance(h, dict):
                    # Try common name fields
                    n = (h.get("name") or h.get("handle")
                          or h.get("label") or "")
                    if n:
                        parts.append(str(n).strip())
            handle_str = ", ".join(p for p in parts if p)
        else:
            handle_str = str(handle_raw).strip()

        return cls(
            id=gs("id"),
            name=gs("name"),
            group=gs("group"),
            handle=handle_str,
            year=gi("year"),
            category=cat_raw,
            category_name=cat_name,
            site_category=gi("siteCategory", "site_category"),
            rating=gi("rating"),
            site_rating=gi("siteRating", "site_rating"),
            updated=gs("updated"),
            released=gs("released"),
            raw=j,
        )


@dataclass
class ASM64File:
    """One downloadable file inside a release.

    Returned by /search/entries/{id}/{cat} which gives:
        {"contentEntry":[
            {"path":"jumpmanjunior-wcs.d64", "id":0},
            ...],
         "isContentByItself":false}
    """
    id: int = 0           # index used by /search/bin/{id}/{cat}/{idx}
    name: str = ""        # the "path" field, used as filename
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, j: dict) -> "ASM64File":
        try:
            idx = int(j.get("id", 0))
        except (TypeError, ValueError):
            idx = 0
        return cls(
            id=idx,
            name=str(j.get("path", j.get("name", ""))),
            raw=j,
        )

    @property
    def extension(self) -> str:
        if "." in self.name:
            return self.name.rsplit(".", 1)[-1].lower()
        return ""

    @property
    def size(self) -> int:
        # The server doesn't return file sizes in the entries
        # response. We populate this after download.
        try:
            return int(self.raw.get("size", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def type(self) -> str:
        return self.extension

    @property
    def url(self) -> str:
        # Not used - download goes through /search/bin/{id}/{cat}/{idx}
        return ""


# ---------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------


class ASM64Client:
    """HTTP wrapper around the Assembly64 API at hackerswithstyle.se.

    Endpoints (from the Ultimate 64 firmware):
        GET /leet/search/aql?query=<AQL>     -> JSON array of hits
        GET /leet/search/aql/presets         -> presets / categories
        GET /leet/search/entries/{id}/{cat}  -> files in a release
        GET /leet/search/bin/{id}/{cat}/{i}  -> raw file bytes
        GET /leet/search/bin/{path}/{name}   -> raw file bytes (alt)

    AQL grammar (Assembly Query Language) examples:
        (name:"jumpman")
        (name:"jumpman") & (type:prg)
        (group:censor) & (year:1990)
        (handle:tasco)

    Usage:
        c = ASM64Client()
        entries = c.search(name="jumpman", file_type="prg")
        for e in entries:
            print(e.name, "by", e.group, e.year)
            files = c.list_files(e.id, e.category)
            for f in files:
                print("  ", f.name, f"(idx {f.id})")
                # Download
                data = c.download_file(e.id, e.category, f.id)
    """

    # Standard headers identifying Quopus as the client. The
    # Assembly64 backend uses Client-Id to discriminate between
    # different consumers of the API (the U64 firmware, third-
    # party tools, etc) for rate-limiting, telemetry, and
    # feature flags. Quopus identifies as itself so the backend
    # can adjust quota or routes specifically for us if needed.
    DEFAULT_HEADERS = {
        "User-Agent": "Assembly Query",
        "Client-Id": "Quopus",
        "Accept": "application/json, */*",
    }

    def __init__(self, base: str = DEFAULT_BASE,
                 paths: Optional[dict] = None,
                 timeout: float = 15.0,
                 user_agent: Optional[str] = None):
        self.base = base.rstrip("/")
        self.paths = dict(DEFAULT_PATHS)
        if paths:
            self.paths.update(paths)
        self.timeout = timeout
        self._headers = dict(self.DEFAULT_HEADERS)
        if user_agent:
            self._headers["User-Agent"] = user_agent
        # Track last error / status for UI display
        self.last_error = ""
        self.last_status = 0
        # Lazy-loaded lookup for category-id -> name mapping.
        # Populated on first call to get_category_name(); subsequent
        # calls are O(1) dict reads. None means "not loaded yet";
        # an empty dict after a failed load means "loaded, but
        # server returned nothing useful" - we won't re-try in
        # that session to avoid spamming a flaky endpoint.
        self._categories_by_id: Optional[dict] = None

    # -------- AQL query builder ------------------------------------

    @staticmethod
    def build_aql(name: str = "", group: str = "",
                  handle: str = "", year: str = "",
                  file_type: str = "", category: str = "") -> str:
        """Build an AQL expression from individual filter fields.
        Empty fields are dropped. Strings get double-quoted, year
        and integer-ish fields go through bare.

        AQL prefix conventions (May 2026 server update from the
        Assembly64 dev team):
          - plain text     -> contains-match (default)
          - "@something"   -> exact-match
          - "-something"   -> any-field wildcard: searches the
                              term in name, group, handle AND
                              event simultaneously. Emitted as
                              the (any:term) AQL tag. Only
                              recognised in the Name field
                              (and at this layer we mirror
                              that - typing -foo into Group
                              just becomes a literal "-foo"
                              group match, since the prefix
                              is anchored to name semantics).

        Examples:
          name="mason"     -> (name:"mason")
          name="@mason"    -> (name:@"mason")    exact match
          name="-mason"    -> (any:mason)        wildcard across
                                                   name+group+
                                                   handle+event
        """
        parts = []
        if name:
            # Strip any double-quotes the user typed - we add our own
            n = name.replace('"', '').strip()
            if n:
                if n.startswith('-') and len(n) > 1:
                    # New (any:X) wildcard. Trim the leading
                    # dash, send the rest as the any-field
                    # search term. Spaces inside the term go
                    # through verbatim - the server tokenizes.
                    rest = n[1:].strip()
                    if rest:
                        parts.append(f'(any:{rest})')
                elif n.startswith('@') and len(n) > 1:
                    # Exact-match on name. The server treats
                    # @ as "this is the literal full name,
                    # don't do substring".
                    rest = n[1:].strip()
                    if rest:
                        parts.append(f'(name:@"{rest}")')
                else:
                    parts.append(f'(name:"{n}")')
        if group:
            g = group.replace('"', '').strip()
            if g:
                # Same @ exact-match support as name. Group
                # field doesn't take the (any:X) prefix per
                # the dev team note, only name does.
                if g.startswith('@') and len(g) > 1:
                    rest = g[1:].strip()
                    if rest:
                        parts.append(f'(group:@"{rest}")')
                else:
                    parts.append(f'(group:"{g}")')
        if handle:
            h = handle.replace('"', '').strip()
            if h:
                if h.startswith('@') and len(h) > 1:
                    rest = h[1:].strip()
                    if rest:
                        parts.append(f'(handle:@"{rest}")')
                else:
                    parts.append(f'(handle:"{h}")')
        if year:
            y = str(year).strip()
            if y:
                parts.append(f'(year:{y})')
        if file_type:
            t = file_type.replace('"', '').strip().lower()
            if t:
                parts.append(f'(type:{t})')
        if category:
            c = str(category).strip()
            if c:
                parts.append(f'(category:{c})')
        return " & ".join(parts) if parts else '(name:"*")'

    # -------- low-level GET ----------------------------------------

    def _get(self, full_path: str,
             accept: str = "application/json"):
        """Make a GET request to base + full_path. Returns parsed
        JSON on 200 + JSON content-type, raw bytes on 200 + binary,
        or raises ASM64APIError on non-2xx.

        full_path is appended verbatim to self.base. Caller is
        responsible for URL-encoding query parameters.
        """
        url = self.base + full_path
        headers = dict(self._headers)
        headers["Accept"] = accept
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.last_status = r.status
                data = r.read()
                ctype = r.headers.get('Content-Type', '')
                if 'json' in ctype.lower():
                    try:
                        return json.loads(
                            data.decode('utf-8', errors='replace'))
                    except json.JSONDecodeError:
                        # Server returned malformed JSON. Return raw.
                        return data
                return data
        except urllib.error.HTTPError as e:
            self.last_status = e.code
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                body = ""
            # Spring Boot's standard 404 body is JSON; surface the
            # 'detail' field if present.
            detail = ""
            if body.startswith("{"):
                try:
                    j = json.loads(body)
                    detail = j.get("detail") or j.get("title") or ""
                except Exception:
                    pass
            self.last_error = (
                f"HTTP {e.code}"
                + (f": {detail}" if detail else f": {body[:200]}"))
            raise ASM64APIError(self.last_error) from e
        except urllib.error.URLError as e:
            self.last_error = f"network: {e.reason}"
            raise ASM64APIError(self.last_error) from e
        except (ValueError, OSError) as e:
            self.last_error = str(e)
            raise ASM64APIError(self.last_error) from e

    # -------- search -----------------------------------------------

    def search_aql(self, aql: str) -> list:
        """Run a raw AQL query string. Returns list of ASM64Entry.

        This hits the /leet/search/aql endpoint without
        pagination, which the server caps at its default page
        size (typically 50-250 entries). Use search_aql_page()
        for the paginated variant that lets you stream through
        all results."""
        path = self.paths["search"] + "?query=" + \
               urllib.parse.quote(aql, safe='')
        try:
            data = self._get(path)
        except ASM64APIError:
            raise
        return self._parse_entries_response(data)

    def search_aql_page(self, aql: str, start: int,
                        count: int, sort: str = "",
                        order: str = "", recency: str = "") -> list:
        """Paginated AQL search. Returns one page of entries.

        Uses the /leet/search/aql/<start>/<count>?query=... form
        that the official Assembly64 client uses to walk through
        large result sets. Server-side ordering is preserved, so
        consecutive pages give you the full sorted list - no
        client-side year-bucketing or dedup needed.

        Optional sort / order / recency get appended as query
        string parameters when set. Values come from the server's
        /presets sections of the same name:
          sort:    name|group|handle|event|year|rating
          order:   asc|desc
          recency: 1days|2days|4days|1week|2weeks|3weeks|1month|
                   2months|3months|6months|1year|2years

        Convention: returning an empty list means "no more pages
        available", which the paginated worker uses as its
        termination signal.
        """
        # The paginated endpoint takes start/count as PATH segments,
        # not query string params: /aql/<start>/<count>?query=...
        path = (self.paths["search"]
                + f"/{int(start)}/{int(count)}"
                + "?query="
                + urllib.parse.quote(aql, safe=''))
        # Append sort/order/recency as standard URL params. The
        # server tolerates extra params it doesn't recognise, so
        # this is safe even on older Assembly64 builds.
        for k, v in (("sort", sort), ("order", order),
                      ("recency", recency)):
            if v:
                path += (f"&{k}="
                          + urllib.parse.quote(str(v), safe=''))
        try:
            data = self._get(path)
        except ASM64APIError:
            raise
        return self._parse_entries_response(data)

    def get_charts(self, category: str = "") -> list:
        """Fetch the server's pre-computed top-rated entries for
        a given category. Returns a list of ASM64Entry items in
        rank order (highest rated first).

        Endpoint: /charts/{category} - the Assembly64 server
        does the rating sort + cut-off itself, so no client-side
        sorting is needed and the response is typically already
        capped at Top 200.

        category should be an aqlKey from the /presets 'category'
        section ('demos', 'games', 'music', 'intros', 'graphics',
        'tools', 'mags', 'charts', 'bbs', 'easyflash', 'reu',
        'c128', 'misc'). Empty string gives the overall Top 200.

        Raises ASM64APIError on transport/server failures so the
        caller can fall back to the AQL-based Top preset path.
        """
        path = self.paths["charts"]
        if category:
            path = path + "/" + urllib.parse.quote(
                category, safe='')
        data = self._get(path)
        return self._parse_entries_response(data)

    def search(self, name: str = "", group: str = "",
               handle: str = "", year: str = "",
               file_type: str = "", category: str = "",
               # Legacy kwargs kept for backwards compat with the old
               # UI - silently ignored if passed.
               source: str = "", rating: str = "",
               sort: str = "", recency: str = "",
               page: int = 0, size: int = 50,
               # Allow the dialog to pass type_ (Python keyword-ish)
               type_: str = ""):
        """Build an AQL from filter fields and run it.

        Most fields go straight into the AQL. `source`, `rating`,
        `sort`, `recency`, `page`, `size` are NOT part of the AQL
        and the server doesn't support them in /search/aql; we keep
        them as no-ops so the existing dialog code still works.
        """
        if type_ and not file_type:
            file_type = type_
        aql = self.build_aql(
            name=name, group=group, handle=handle,
            year=year, file_type=file_type, category=category)
        return self.search_aql(aql)

    def _parse_entries_response(self, data) -> list:
        """Normalize a search response to list[ASM64Entry]. The
        AQL endpoint returns a top-level array."""
        # Pull out the raw entry list
        raw_entries = None
        if isinstance(data, list):
            raw_entries = data
        elif isinstance(data, dict):
            for k in ("results", "entries", "data", "items"):
                if k in data and isinstance(data[k], list):
                    raw_entries = data[k]
                    break
        if raw_entries is None:
            return []
        return [ASM64Entry.from_json(j) for j in raw_entries]

    # -------- entry details / files --------------------------------

    def list_files(self, entry_id: str, category=0) -> list:
        """Return the list of files belonging to one release.

        URL is /search/entries/{id}/{cat} per the U64 firmware.
        `category` can be an int id (legacy) OR a string aqlKey
        (modern Assembly64 servers) - we URL-encode it either
        way and let the server resolve it.

        The response is {"contentEntry":[...],
        "isContentByItself":bool}.
        """
        if not entry_id:
            return []
        # Stringify the category and URL-encode it. We don't try
        # int() any more because the server now uses string keys
        # like 'csdb' / 'gamebase' that aren't numeric. URL-encode
        # protects against any odd characters in the key.
        cat_str = urllib.parse.quote(
            str(category) if category not in (None, "") else "0",
            safe='')
        path = (self.paths["entries"]
                + "/" + urllib.parse.quote(str(entry_id), safe='')
                + "/" + cat_str)
        try:
            data = self._get(path)
        except ASM64APIError:
            raise
        return self._parse_files_response(data)

    def _parse_files_response(self, data) -> list:
        if isinstance(data, dict):
            ce = data.get("contentEntry")
            if isinstance(ce, list):
                return [ASM64File.from_json(j) for j in ce]
            for k in ("files", "items", "results", "data"):
                if k in data and isinstance(data[k], list):
                    return [ASM64File.from_json(j) for j in data[k]]
        if isinstance(data, list):
            return [ASM64File.from_json(j) for j in data]
        return []

    def download_file(self, entry_id, category=0, file_idx=0,
                       file_info=None) -> bytes:
        """Download a single file. Two call styles:

        - download_file(entry_id, category, file_idx) - uses the
          /search/bin/{id}/{cat}/{idx} URL form
        - download_file(file_info=<ASM64File>) - convenience form
          for callers that already have a file_info; we still need
          the parent entry_id+category so this signature mostly
          exists for backwards compat with the dialog code.
        """
        if file_info is not None and not entry_id:
            # Old call style; we don't know the parent any more.
            raise ASM64APIError(
                "download_file requires entry_id + category + idx")
        idx = file_idx
        if file_info is not None:
            idx = file_info.id
        # Category may be a string aqlKey or int. URL-encode
        # whichever it is.
        cat_str = urllib.parse.quote(
            str(category) if category not in (None, "") else "0",
            safe='')
        path = (self.paths["download"]
                + "/" + urllib.parse.quote(str(entry_id), safe='')
                + "/" + cat_str
                + "/" + str(int(idx)))
        data = self._get(path, accept="*/*")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        # Server may have returned JSON-encoded error
        raise ASM64APIError(
            f"download returned non-binary: {str(data)[:200]}")

    # -------- preset lists -----------------------------------------

    def get_presets(self) -> dict:
        """Fetch the /search/aql/presets blob. Returns whatever the
        server gives us - typically a dict with categories, types,
        groups etc. Caller is responsible for picking fields."""
        try:
            data = self._get(self.paths["presets"])
            if isinstance(data, (dict, list)):
                return data
        except ASM64APIError:
            pass
        return {}

    def get_category_name(self, category_id) -> str:
        """Return the display name for a category, or empty
        string when unknown.

        Accepts either an int id (legacy server format) OR a
        string key like 'csdb' (modern Assembly64 servers, which
        send aqlKey strings instead of numeric ids).

        First call lazy-loads the full lookup from /presets and
        caches it for the rest of the session. Subsequent
        lookups are O(1) dict reads.
        """
        if category_id is None or category_id == "" \
                or category_id == 0:
            return ""
        if self._categories_by_id is None:
            self._categories_by_id = (
                self._build_category_lookup())
        # Try the value as-is first (lowercase string form is
        # the canonical key in modern lookups). Then try as int
        # for the legacy int-keyed entries that older parsers
        # populate.
        if isinstance(category_id, str):
            v = self._categories_by_id.get(
                category_id.strip().lower(), "")
            if v:
                return v
            # Maybe the server uses an integer in string form
            try:
                v2 = self._categories_by_id.get(
                    int(category_id), "")
                if v2:
                    return v2
            except (TypeError, ValueError):
                pass
            return ""
        # int / other
        try:
            cat_id = int(category_id)
        except (TypeError, ValueError):
            return ""
        if cat_id <= 0:
            return ""
        return self._categories_by_id.get(cat_id, "") or ""

    def get_release_type_name(self, type_id) -> str:
        """Return the CSDB release-type display name for a
        numeric id, or empty string when unknown.

        The `siteCategory` field on every search result is a
        CSDB release_type id (e.g. 20 = "C64 Crack", 1 =
        "C64 Demo"). This is the per-release content type and
        what most users want in the results column.

        Resolution order:
          1. Hardcoded _CSDB_RELEASE_TYPES table (covers the
             user-confirmed release types)
          2. Session learned cache (filled by
             learn_release_type from real CSDB lookups)
          3. Disk cache (persisted across sessions in
             config/asm64_release_types.json)
          4. Empty string -> caller renders "Type #N"
        """
        if type_id is None or type_id == "" or type_id == 0:
            return ""
        try:
            tid = int(type_id)
        except (TypeError, ValueError):
            return ""
        if tid <= 0:
            return ""
        # Hardcoded first - fastest, most reliable
        v = _CSDB_RELEASE_TYPES.get(tid)
        if v:
            return v
        # Session + disk cache from auto-learning
        learned = self._get_learned_types()
        return learned.get(tid, "")

    def _learned_types_path(self):
        """Path to the disk file caching auto-learned CSDB
        release-type mappings. Lives in config/ so it survives
        Quopus updates."""
        try:
            from .config import CONFIG_DIR
            return CONFIG_DIR / "asm64_release_types.json"
        except Exception:
            return None

    def _get_learned_types(self) -> dict:
        """Lazy-load the persisted release-type lookup. Returns
        a dict {int_id: str_name}. Falls back to empty dict on
        any error - missing entries just leave the result list
        showing 'Type #N' for those ids until learned."""
        if getattr(self, "_learned_types_cache", None) is not None:
            return self._learned_types_cache
        out = {}
        p = self._learned_types_path()
        if p is not None and p.is_file():
            try:
                import json as _json
                with open(p, "r", encoding="utf-8") as f:
                    raw = _json.load(f)
                # Stored as {str: str} since JSON keys must be
                # strings. Convert keys back to int.
                for k, v in raw.items():
                    try:
                        out[int(k)] = str(v)
                    except (TypeError, ValueError):
                        continue
            except (OSError, ValueError):
                pass
        self._learned_types_cache = out
        return out

    def learn_release_type(self, type_id, name: str):
        """Record a newly-discovered release_type -> name mapping.
        Persists to disk so the same lookup doesn't have to hit
        CSDB again on the next session. No-op if either argument
        is empty.
        """
        if not type_id or not name:
            return
        try:
            tid = int(type_id)
        except (TypeError, ValueError):
            return
        # Don't bother re-learning what we already have
        if tid in _CSDB_RELEASE_TYPES:
            return
        cache = self._get_learned_types()
        if cache.get(tid) == name:
            return
        cache[tid] = name
        self._learned_types_cache = cache
        # Persist to disk
        p = self._learned_types_path()
        if p is None:
            return
        try:
            import json as _json
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {str(k): v for k, v in cache.items()}
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(payload, f, indent=2, sort_keys=True)
        except OSError:
            pass

    def lookup_csdb_release_type(self, release_id):
        """Fetch a single release from csdb.dk's webservice and
        return its <Type> string, e.g. 'C64 Crack' or
        'C64 One-File Demo'. Returns empty string on any error.

        This is the slow path - one HTTP call to csdb.dk - so
        callers should only invoke it for release ids whose
        siteCategory isn't in the hardcoded table yet, and
        should cache the result via learn_release_type().
        """
        details = self.lookup_csdb_release_details(release_id)
        return details.get("type", "")

    def lookup_csdb_release_details(self, release_id):
        """Fetch a release from csdb.dk's webservice and return
        a dict with 'type', 'credits' (list of dicts with
        'credit_type' and 'handle'), and 'groups' (list of
        strings). Returns empty dict on any error.

        Used for two things:
          - Type discovery (filling _CSDB_RELEASE_TYPES gaps)
          - The detail panel showing all credits and groups,
            since /search/aql only returns ONE handle per
            release.
        """
        out = {"type": "", "credits": [], "groups": []}
        if not release_id:
            return out
        try:
            import urllib.request, urllib.error
            import re as _re
            req = urllib.request.Request(
                f"https://csdb.dk/webservice/"
                f"?type=release&id={int(release_id)}",
                headers={"User-Agent": "Quopus/Asm64"})
            with urllib.request.urlopen(
                    req, timeout=5) as r:
                body = r.read().decode(
                    "utf-8", errors="replace")
        except (urllib.error.URLError, ValueError, OSError):
            return out

        # Type
        m = _re.search(r"<Type>([^<]+)</Type>", body)
        if m:
            out["type"] = m.group(1).strip()

        # Build a handle-id -> handle-name lookup first by
        # scanning the full XML for any <Handle><ID>N</ID>
        # <Handle>NAME</Handle> blocks. Some <Credit> entries
        # reference handles by ID only (without re-supplying the
        # name) - we resolve those via this lookup so all
        # credits get their handle name.
        handle_id_to_name = {}
        for m in _re.finditer(
                r"<Handle><ID>(\d+)</ID>\s*"
                r"<Handle>([^<]+)</Handle>",
                body):
            try:
                handle_id_to_name[int(m.group(1))] = (
                    m.group(2).strip())
            except (TypeError, ValueError):
                continue

        # Credits block: each <Credit> has <CreditType> and
        # a <Handle><ID>N</ID> reference. The handle name may
        # be inline or referenced by ID only (resolved via the
        # lookup above).
        for credit_block in _re.finditer(
                r"<Credit>(.*?)</Credit>", body, _re.DOTALL):
            blob = credit_block.group(1)
            ctype_m = _re.search(
                r"<CreditType>([^<]+)</CreditType>", blob)
            # Try inline name first
            handle_m = _re.search(
                r"<Handle><ID>\d+</ID>\s*<Handle>([^<]+)</Handle>",
                blob)
            handle = ""
            if handle_m:
                handle = handle_m.group(1).strip()
            else:
                # Reference by ID only - look up
                id_m = _re.search(
                    r"<Handle><ID>(\d+)</ID>", blob)
                if id_m:
                    try:
                        handle = handle_id_to_name.get(
                            int(id_m.group(1)), "")
                    except (TypeError, ValueError):
                        handle = ""
            if not handle:
                continue
            ctype = (ctype_m.group(1).strip()
                      if ctype_m else "")
            out["credits"].append({
                "credit_type": ctype,
                "handle": handle,
            })

        # Groups: <ReleasedBy><Group><Name>X</Name></Group></ReleasedBy>
        for grp_m in _re.finditer(
                r"<Group><ID>\d+</ID>\s*<Name>([^<]+)</Name>",
                body):
            name = grp_m.group(1).strip()
            if name and name not in out["groups"]:
                out["groups"].append(name)

        return out

    def _build_category_lookup(self) -> dict:
        """Walk the /presets payload and pull out the category
        key->name mapping. Returns empty dict if the endpoint is
        unreachable or its shape is unrecognised.

        Modern Assembly64 servers (hackerswithstyle.se circa
        2024+) return /presets as a LIST of section dicts:

            [
              {"type": "repo",
               "description": "Repository",
               "values": [
                 {"aqlKey": "csdb",     "name": "CSDB"},
                 {"aqlKey": "gamebase", "name": "Gamebase64"},
                 ...]},
              {"type": "category",
               "description": "Category",
               "values": [...]},
              {"type": "type",
               ...},
              ...
            ]

        We walk every section regardless of `type` and add its
        aqlKey -> name pairs to one big string-keyed lookup.
        That way the same lookup works for resolving the
        category field, the type field, the source field, etc -
        whichever string the server hands us, we can find a
        display name for it.

        Older servers used dict-of-categories or int-keyed list
        forms; those are still handled below for back-compat.
        """
        out: dict = {}  # string-keyed (aqlKey -> display name)
        presets = self.get_presets()

        # --- Modern shape: list of sections ---------------
        # We pick the ONE section that represents categories (by
        # type or description) and use just its values. Mixing
        # multiple sections (repo + category + subcat ...) into
        # one lookup happens to work as long as the aqlKeys are
        # unique - but it's brittle. Scoping cleanly avoids
        # accidental collisions and matches what the server
        # intends. Plus: the same code path now also handles
        # the "no category section" case by returning empty.
        if isinstance(presets, list):
            cat_section = None
            for sec in presets:
                if not isinstance(sec, dict):
                    continue
                t = str(sec.get("type", "")).lower()
                d = str(sec.get("description", "")).lower()
                if (t in ("category", "categories")
                        or d.startswith("categor")):
                    cat_section = sec
                    break
            if cat_section is not None:
                for v in cat_section.get("values") or []:
                    if not isinstance(v, dict):
                        continue
                    key = (v.get("aqlKey")
                            or v.get("key")
                            or v.get("id")
                            or "")
                    name = (v.get("name")
                             or v.get("label")
                             or v.get("title")
                             or "")
                    if key and name:
                        # Lowercase key for case-insensitive
                        # lookup ('CSDB' / 'csdb' both work).
                        out[str(key).strip().lower()] = (
                            str(name).strip())
            if out:
                return out

        # --- Legacy shapes (older servers) ----------------
        if isinstance(presets, dict):
            for key, blob in presets.items():
                if blob is None:
                    continue
                # Only consider category-ish keys at this layer
                key_lower = str(key).lower()
                if ("cat" not in key_lower
                        and "type" not in key_lower
                        and "repo" not in key_lower
                        and "source" not in key_lower):
                    continue
                if isinstance(blob, list):
                    for i, item in enumerate(blob):
                        if isinstance(item, dict):
                            iid = (item.get("aqlKey")
                                    or item.get("id", i))
                            name = (item.get("name")
                                     or item.get("label")
                                     or item.get("title")
                                     or "")
                            if iid is not None and name:
                                out[str(iid).strip().lower()] = (
                                    str(name).strip())
                                # Also accept int form for old
                                # int-keyed callers
                                try:
                                    out[int(iid)] = (
                                        str(name).strip())
                                except (TypeError, ValueError):
                                    pass
                        elif isinstance(item, str):
                            out[i] = item.strip()
                elif isinstance(blob, dict):
                    for k, v in blob.items():
                        out[str(k).strip().lower()] = (
                            str(v).strip())
                        try:
                            out[int(k)] = str(v).strip()
                        except (TypeError, ValueError):
                            pass

        if not out:
            # /presets unreachable or empty - use the hardcoded
            # snapshot of the 13 known categories. Won't catch
            # new ones added after this snapshot but at least
            # users see "Demos" / "Games" / etc rather than raw
            # aqlKeys for the common cases.
            out = dict(_DEFAULT_CATEGORY_NAMES)
        return out

    @staticmethod
    def csdb_release_url(csdb_id) -> str:
        """Build the public CSDB deep-link for a release id. The
        Assembly64 IDs ARE the CSDB IDs so this works directly."""
        if not csdb_id:
            return ""
        try:
            return f"https://csdb.dk/release/?id={int(csdb_id)}"
        except (TypeError, ValueError):
            return ""


class ASM64APIError(Exception):
    """Raised for any API / network failure."""


# ---------------------------------------------------------------------
# Favorites + saved searches (JSON files in CONFIG_DIR)
# ---------------------------------------------------------------------


def _favorites_path():
    from .config import CONFIG_DIR
    return CONFIG_DIR / "asm64_favorites.json"


def _searches_path():
    from .config import CONFIG_DIR
    return CONFIG_DIR / "asm64_searches.json"


def load_favorites() -> list:
    """Return list of favorited ASM64Entry dicts. Empty list on
    missing or unparseable file."""
    import os
    path = _favorites_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f) or []
    except (OSError, ValueError):
        return []


def save_favorites(favorites: list):
    """Persist the favorites list. Argument is a list of dicts
    (typically `asdict(ASM64Entry)`)."""
    import os
    path = _favorites_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(favorites, f, indent=2)


def load_saved_searches() -> list:
    """Return list of {name, params} dicts."""
    import os
    path = _searches_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f) or []
    except (OSError, ValueError):
        return []


def save_saved_searches(searches: list):
    import os
    path = _searches_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(searches, f, indent=2)


# ---------------------------------------------------------------------
# Last-search-result persistence (auto-restore on dialog reopen)
# ---------------------------------------------------------------------


def _last_results_path():
    """Where we cache the most recent search results so the dialog
    can repopulate them on next open. Only ONE snapshot is kept;
    every new successful search overwrites it."""
    from .config import CONFIG_DIR
    return CONFIG_DIR / "asm64_last_results.json"


def load_last_results() -> dict:
    """Return the last cached search session as
        {"params": {...}, "entries": [ASM64Entry dicts], "timestamp": ISO}
    or an empty dict if no cache exists yet. Caller decides whether
    to repopulate the UI from this."""
    import os
    path = _last_results_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_last_results(params: dict, entries: list):
    """Persist the most recent successful search.

    params:   the filter dict that produced these results
              (name, group, handle, year, file_type, category)
    entries:  list of ASM64Entry instances - we serialize the .raw
              dict from each so re-parsing gives identical objects.
    """
    import os, datetime
    path = _last_results_path()
    try:
        os.makedirs(
            os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = {
            "params": dict(params or {}),
            "timestamp": datetime.datetime.now().isoformat(),
            "entries": [
                # Use the original server JSON when available so the
                # round-trip through from_json reproduces every field.
                # Fall back to asdict() if .raw is missing.
                (e.raw if getattr(e, "raw", None) else asdict(e))
                for e in entries
            ],
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
    except (OSError, TypeError, ValueError):
        # Persistence is best-effort - never crash the search flow
        # just because we couldn't write the cache.
        pass


# ---------------------------------------------------------------------
# Named search-result snapshots
# ---------------------------------------------------------------------
#
# Different from save_last_results() which keeps ONE auto-cache for
# session restore. Named snapshots are user-curated: "save the
# current 600-entry list as 'demo_compo_2026' so I can come back
# to it next week".
#
# Storage format:
#   config/asm64_saved_results.json = {
#     "<user_name>": {
#       "saved_at": "2026-05-21T10:30:00",
#       "params": {...},          # original search filters
#       "entries": [...],          # list of entry dicts
#     },
#     ...
#   }
#
# We keep the entries inline rather than spreading across many
# files because:
#   - typical user has <50 saved snapshots, each <1MB
#   - one file is easier to back up / inspect / share
#   - atomic write (temp + rename) avoids corruption on crash


def _saved_results_path():
    """Where named result snapshots are persisted. Separate from
    asm64_last_results.json (the auto-cache) and asm64_searches.json
    (saved filter sets) so each has its own clean schema."""
    from .config import CONFIG_DIR
    return CONFIG_DIR / "asm64_saved_results.json"


def load_saved_results_index() -> dict:
    """Return the full saved-results dict, keyed by user name.
    Empty dict if the file is missing or unparseable. Each entry
    is {'saved_at': iso, 'params': dict, 'entries': list}."""
    import os
    path = _saved_results_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_saved_results_index(data: dict) -> None:
    """Atomic write of the whole snapshot dict. Uses a temp file
    + rename so a crash mid-write leaves the previous valid
    version intact."""
    import os
    path = _saved_results_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)),
                exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def save_named_results(name: str, params: dict,
                       entries: list) -> None:
    """Save the current result set under `name`. Overwrites any
    existing snapshot with the same name.

    Entry serialisation prefers the .raw dict that ASM64Entry
    stores when it was constructed from JSON - that's the
    server's original payload and round-trips perfectly through
    ASM64Entry.from_json on load. asdict() fallback is for
    entries built manually (e.g. tests).
    """
    import datetime
    data = load_saved_results_index()
    data[name] = {
        "saved_at": datetime.datetime.now().isoformat(),
        "params": dict(params or {}),
        "entries": [
            (e.raw if getattr(e, "raw", None) else asdict(e))
            for e in entries
        ],
    }
    save_saved_results_index(data)


def load_named_results(name: str) -> tuple:
    """Return (params_dict, entry_list) for the named snapshot.
    entry_list contains ASM64Entry instances reconstructed from
    the stored dicts. Raises KeyError if `name` isn't in the
    index."""
    data = load_saved_results_index()
    if name not in data:
        raise KeyError(name)
    snap = data[name]
    params = dict(snap.get("params") or {})
    entries = [
        ASM64Entry.from_json(d)
        for d in snap.get("entries") or []
    ]
    return params, entries


def delete_named_results(name: str) -> bool:
    """Drop one named snapshot. Returns True if it existed."""
    data = load_saved_results_index()
    if name not in data:
        return False
    del data[name]
    save_saved_results_index(data)
    return True


# ---------------------------------------------------------------------
# Qt UI - imported lazily inside the class
# ---------------------------------------------------------------------


class ASM64BrowserDialog:
    """Factory function returning a Qt dialog. We avoid Qt at module
    load so headless tests can still import ASM64Client.

    Use:
        from .asm64_browser import make_browser_dialog
        dlg = make_browser_dialog(parent, on_run=..., on_mount=...)
        dlg.exec()
    """
    pass


def make_browser_dialog(parent=None,
                        on_run=None,
                        on_mount=None,
                        on_save=None):
    """Build and return the Assembly64 browser QDialog.

    Callbacks (any can be None - the corresponding action is then
    just disabled or saves locally):

      on_run(file_bytes, filename):
          Run a PRG/CRT/SID directly on the device.
      on_mount(file_bytes, filename, drive='a', mode='readonly'):
          Mount a D64/D71/D81 on a drive.
      on_save(file_bytes, filename):
          Save a downloaded file to the lister's current directory.
    """
    from PyQt6.QtCore import (
        Qt, QThread, pyqtSignal, QSize,
    )
    from PyQt6.QtGui import QDesktopServices, QPixmap
    from PyQt6.QtCore import QUrl
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QLineEdit, QComboBox, QTreeWidget, QTreeWidgetItem,
        QSplitter, QApplication, QFileDialog, QMessageBox,
        QInputDialog, QListWidget, QListWidgetItem, QGroupBox,
        QFormLayout, QHeaderView, QMenu, QWidget,
    )

    class _SearchWorker(QThread):
        """Run a search in the background so the UI stays
        responsive."""
        done = pyqtSignal(bool, list, str)
        # ok, entries, error

        def __init__(self, client, params, parent=None):
            super().__init__(parent)
            self.client = client
            self.params = params

        def run(self):
            try:
                results = self.client.search(**self.params)
                self.done.emit(True, results, "")
            except ASM64APIError as e:
                self.done.emit(False, [], str(e))
            except Exception as e:
                self.done.emit(False, [], f"unexpected: {e}")

    class _PaginatedSearchWorker(QThread):
        """Walk through all pages of an Assembly64 AQL search.

        This is what the official Assembly64 client uses to
        return more than the server's per-query cap of ~50-250
        entries. We hit /leet/search/aql/<start>/<count> with
        page_size up front, increment `start` by page_size each
        round, and stop when the server returns a short page
        (fewer items than requested) or an empty page.

        Compared to the old _YearSweepWorker:
          - 1 request per page instead of 47 (one per year)
          - server-side sort is preserved (no client-side dedup
            needed since pages are non-overlapping)
          - faster on every query: typical "name:demo" returns
            a few hundred hits in 2-4 page requests, ~1 second
          - matches what the official Assembly64 client does

        Streaming: emits chunk(state_dict) after each page so the
        UI can append rows to the results tree as they arrive.
        """
        chunk = pyqtSignal(dict)
        done = pyqtSignal(bool, list, str)

        def __init__(self, client, aql: str,
                     page_size: int = 250,
                     max_pages: int = 200,
                     parent=None):
            super().__init__(parent)
            self.client = client
            self.aql = aql
            self.page_size = page_size
            # Safety cap: ~200 pages × 250 entries = 50000 results,
            # well beyond any sensible query. Stops runaway
            # downloads if the server lies about page size.
            self.max_pages = max_pages
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            merged = []
            seen_ids = set()
            start = 0
            page_no = 0
            try:
                while page_no < self.max_pages:
                    if self._cancel:
                        self.done.emit(
                            True, merged, "(cancelled)")
                        return
                    try:
                        page = self.client.search_aql_page(
                            self.aql, start, self.page_size)
                    except ASM64APIError as e:
                        # If page 0 itself fails, surface the
                        # error. If we already have some pages,
                        # treat it as end-of-data.
                        if page_no == 0:
                            self.done.emit(
                                False, [], str(e))
                            return
                        break
                    # Dedup defensively: in theory the paginated
                    # endpoint never returns duplicates across
                    # pages, but better to filter than to ship
                    # the same release twice.
                    new_in_page = []
                    for e in page:
                        if e.id and e.id in seen_ids:
                            continue
                        if e.id:
                            seen_ids.add(e.id)
                        merged.append(e)
                        new_in_page.append(e)
                    page_no += 1
                    self.chunk.emit({
                        'page': page_no,
                        'new_entries': new_in_page,
                        'total_unique': len(merged),
                        'page_size': self.page_size,
                        'last_page_count': len(page),
                    })
                    # Server returned fewer than requested =
                    # final page. Stop.
                    if len(page) < self.page_size:
                        break
                    start += self.page_size
                self.done.emit(True, merged, "")
            except Exception as e:
                self.done.emit(
                    False, merged, f"unexpected: {e}")

    class _YearSweepWorker(QThread):
        """Workaround for the Assembly64 server's per-query result
        cap. Replays the same filter once per year, plus one bonus
        pass for entries with year=0 (unknown release year - the
        server commonly returns these from CSDB imports).

        Iterates newest-first by default (reverse=True) so the
        user sees current-decade releases at the top of the list
        within the first second or two; pre-2000 stuff trickles
        in later. Matches what Assembly64's own UI does. Set
        reverse=False to get oldest-first.

        Two ways to consume results:

          chunk(state_dict)  - streaming: emitted after each year
                               with the NEW entries for that year
                               (already deduplicated). UI uses
                               this to incrementally append rows
                               while the sweep is still running.

          done(ok, list, err) - final: emitted once after the
                                last year, with the complete
                                merged list. Used by code paths
                                that don't care about streaming.

        Duplicates across years (same entry returned for both
        year:0 and year:2026 by some servers) are filtered by id
        in the merge step.
        """
        # Per-year results stream out incrementally so the UI can
        # paint matches as they arrive instead of waiting for the
        # whole sweep to finish. The dict-format payload is sticky
        # state: 'new_entries' is just the deltas from this year,
        # while 'total_unique' lets the UI display "N results so
        # far" without re-counting.
        chunk = pyqtSignal(dict)
        # {'year': int, 'new_entries': list[ASM64Entry],
        #  'total_unique': int, 'years_done': int,
        #  'years_total': int}
        progress = pyqtSignal(int, int, int)
        # legacy 3-tuple kept for the manual sweep button
        done = pyqtSignal(bool, list, str)

        def __init__(self, client, base_params,
                     year_start, year_end, parent=None,
                     reverse=True, pace_seconds=0.0):
            super().__init__(parent)
            self.client = client
            # Strip any user-provided year filter - we override it
            # per iteration.
            self.base_params = {k: v for k, v in base_params.items()
                                 if k != "year"}
            self.year_start = year_start
            self.year_end = year_end
            self._cancel = False
            # reverse=True means iterate newest-first so the user
            # sees current-decade releases before 80s ones. That
            # matches what Assembly64's own UI does and matches
            # what most users actually want to find (a release
            # they saw mentioned today is almost certainly recent).
            self.reverse = reverse
            # Sleep between requests. The original was 100ms which
            # adds ~5s of pure idle time over the full sweep on
            # top of the network round-trips. The Assembly64 server
            # handles parallel queries from many clients fine, so
            # going to 0 is OK on a normal connection. We expose
            # the knob so a future settings option could throttle
            # for users with metered connections.
            self.pace_seconds = pace_seconds

        def cancel(self):
            self._cancel = True

        def run(self):
            """Sweep N years using a small thread pool of HTTP
            workers. The Assembly64 server handles concurrent
            requests fine and parallel issuing dominates the
            wall-clock time - sequential 47 years at 200ms each
            is 10 seconds, parallel-4 cuts that to ~2.5s.

            We deliberately keep the pool small (4 threads) so
            we don't slam the server. The original Assembly64
            client uses similar concurrency; what makes it feel
            fast there is exactly this overlap.

            Year ordering matters because of streaming: we emit
            chunks as years finish in completion order, but
            iterate the SUBMISSION order in reverse (newest-
            first) so the first batch of in-flight requests
            covers 2026..2023 - those are what the user almost
            always wants to see first.
            """
            import time
            from concurrent.futures import (
                ThreadPoolExecutor, as_completed)
            seen_ids = set()
            merged = []
            # Iteration order: as before - newest-first in
            # reverse mode, then year=0 last.
            years_range = list(
                range(self.year_start, self.year_end + 1))
            if self.reverse:
                years_range.reverse()
                years_to_query = years_range + [0]
            else:
                years_to_query = [0] + years_range
            total_years = len(years_to_query)
            total_done = 0

            def _query_one(y):
                """Fetch one year. Returns (year, list_of_entries)
                even on error - per-year API failures aren't
                fatal, we just skip that year and continue."""
                p = dict(self.base_params)
                p["year"] = str(y)
                try:
                    return (y, self.client.search(**p))
                except ASM64APIError:
                    return (y, [])
                except Exception:
                    return (y, [])

            try:
                # 4-thread pool is the sweet spot: enough overlap
                # to hide round-trip latency, few enough to not
                # look like a denial-of-service attempt to the
                # server. Tunable via Quopus config in future.
                with ThreadPoolExecutor(
                        max_workers=4) as pool:
                    # Submit ALL years up front. The pool handles
                    # the concurrency; we just wait on futures
                    # as they complete and emit chunks for each.
                    # Submission order doesn't change completion
                    # order - we sort each emitted chunk by year
                    # for status display only, the actual results
                    # tree is sorted by whatever column the user
                    # clicked.
                    futures = [pool.submit(_query_one, y)
                               for y in years_to_query]
                    for fut in as_completed(futures):
                        if self._cancel:
                            # Cancel: drop remaining futures and
                            # return whatever we have so far.
                            for f in futures:
                                f.cancel()
                            self.done.emit(
                                True, merged, "(cancelled)")
                            return
                        try:
                            y, results = fut.result(timeout=60)
                        except Exception:
                            results = []
                            y = -1
                        new_in_year = []
                        for e in results:
                            if e.id and e.id in seen_ids:
                                continue
                            if e.id:
                                seen_ids.add(e.id)
                            merged.append(e)
                            new_in_year.append(e)
                        total_done += 1
                        self.chunk.emit({
                            'year': y,
                            'new_entries': new_in_year,
                            'total_unique': len(merged),
                            'years_done': total_done,
                            'years_total': total_years,
                        })
                        self.progress.emit(
                            y, total_done, len(merged))
                        if self.pace_seconds > 0:
                            time.sleep(self.pace_seconds)
                self.done.emit(True, merged, "")
            except Exception as e:
                self.done.emit(False, merged, f"unexpected: {e}")

    class _FilesWorker(QThread):
        """Fetch the files-in-release for a selected entry."""
        done = pyqtSignal(bool, list, str)

        def __init__(self, client, entry_id, category, parent=None):
            super().__init__(parent)
            self.client = client
            self.entry_id = entry_id
            self.category = category

        def run(self):
            try:
                files = self.client.list_files(
                    self.entry_id, self.category)
                self.done.emit(True, files, "")
            except ASM64APIError as e:
                self.done.emit(False, [], str(e))

    class _CSDBDetailWorker(QThread):
        """Fetch full release details (credits, groups, type)
        from csdb.dk for the currently-selected entry. The
        /search/aql endpoint only returns ONE handle per release
        - if the user wants to see all credits (Code by X,
        Music by Y, Graphics by Z) we have to ask csdb.dk
        directly.

        Cached per release_id so re-selecting the same entry is
        instant.
        """
        done = pyqtSignal(int, dict)  # release_id, details_dict

        # Class-level cache shared across all worker instances
        # in the same process. Keyed on int release_id. Doesn't
        # persist to disk - credits change occasionally and a
        # session-only cache is enough since the user rarely
        # re-opens the exact same release after restart.
        _cache = {}

        def __init__(self, client, release_id, parent=None):
            super().__init__(parent)
            self.client = client
            try:
                self.release_id = int(release_id)
            except (TypeError, ValueError):
                self.release_id = 0

        def run(self):
            if not self.release_id:
                self.done.emit(0, {})
                return
            # Cache hit: emit immediately
            cached = type(self)._cache.get(self.release_id)
            if cached is not None:
                self.done.emit(self.release_id, cached)
                return
            try:
                details = (
                    self.client.lookup_csdb_release_details(
                        self.release_id))
                type(self)._cache[self.release_id] = details
                self.done.emit(self.release_id, details)
            except Exception:
                self.done.emit(self.release_id, {})

    class _TypeLearnerWorker(QThread):
        """Resolves unknown CSDB release_type ids by sampling
        one release per id from csdb.dk.

        Input: {type_id: sample_release_id} - one example
        release for each unknown type_id. We fetch that release
        from csdb.dk, parse out the <Type> string, and persist
        it via client.learn_release_type() so future searches
        get the friendly name from cache without hitting csdb.

        Emits `learned` with a {type_id: name} dict so the UI
        can re-render the result list with the new names.
        """
        learned = pyqtSignal(dict)

        def __init__(self, client, unknown_map, parent=None):
            super().__init__(parent)
            self.client = client
            # Copy so we don't share state with the UI thread
            self.unknown_map = dict(unknown_map)

        def run(self):
            out = {}
            for type_id, release_id in self.unknown_map.items():
                try:
                    name = self.client.lookup_csdb_release_type(
                        release_id)
                    if name:
                        self.client.learn_release_type(
                            type_id, name)
                        out[type_id] = name
                except Exception:
                    # Best-effort - one failure shouldn't kill
                    # the whole batch.
                    continue
                # Be polite to csdb.dk
                import time as _t
                _t.sleep(0.25)
            self.learned.emit(out)

    class _PreviewWorker(QThread):
        """Fetch a release's screenshot/preview image.

        Tries several Assembly64 image endpoint paths in order
        (the production server's exact route has shifted between
        versions), falling back to CSDB's screen-image page if
        the API doesn't have an image. Returns the raw PNG/JPG
        bytes plus a short label that the UI can show next to
        the preview ('api hit', 'cache hit', 'csdb fallback',
        'no image').

        Caches successful fetches in cache/asm64_screenshots/
        keyed on entry id - re-selecting the same release is
        instant. A negative cache (empty 0-byte file) is also
        written so we don't repeatedly hammer the server for
        entries that genuinely have no screenshot.
        """
        # ok, image_bytes, label, error
        done = pyqtSignal(bool, object, str, str)

        # Image endpoint paths to probe, in order. Different
        # Assembly64 server versions / mirrors expose the image
        # at different URLs - we try each one before giving up.
        # These are stitched onto the configured base URL (the
        # same base used by /search/aql).
        _ENDPOINT_TEMPLATES = (
            "/search/image/{id}/{cat}",
            "/search/getimage/{id}/{cat}",
            "/search/screenshot/{id}/{cat}",
        )

        def __init__(self, client, entry_id, category,
                     cache_dir, parent=None):
            super().__init__(parent)
            self.client = client
            self.entry_id = entry_id
            self.category = category
            self.cache_dir = cache_dir
            self.cancelled = False

        def cancel(self):
            """Signal the worker to abandon its result. The HTTP
            request can't be aborted mid-flight but we won't emit
            done() once cancelled, so any in-flight selection
            change is safe."""
            self.cancelled = True

        def run(self):
            try:
                # Cache key: just the entry id - same release
                # always has the same screenshot.
                cache_file = (self.cache_dir
                              / f"{self.entry_id}_"
                                f"{self.category}.bin")
                neg_file = (self.cache_dir
                            / f"{self.entry_id}_"
                              f"{self.category}.none")
                # Negative cache check - we already know there's
                # no image for this release. We tag the negative
                # cache files with a marker line ('v2' for the
                # current viewpic-based code path) so an old
                # 'no image' verdict from a broken earlier
                # version doesn't permanently lock us out of a
                # release that actually has a screenshot. Old
                # markerless .none files are auto-invalidated
                # the first time we re-check.
                neg_marker = b"quopus_neg_cache_v2\n"
                if neg_file.is_file():
                    try:
                        head = neg_file.read_bytes()[:32]
                    except Exception:
                        head = b""
                    if neg_marker.strip() in head:
                        if not self.cancelled:
                            self.done.emit(
                                False, None,
                                "cache hit (no image)", "")
                        return
                    # Old/missing marker - delete and re-probe
                    try:
                        neg_file.unlink()
                    except Exception:
                        pass
                if cache_file.is_file() and cache_file.stat().st_size > 0:
                    data = cache_file.read_bytes()
                    if not self.cancelled:
                        self.done.emit(True, data,
                                       "cache hit", "")
                    return

                # Assembly64's API doesn't actually expose a
                # screenshot endpoint. Skip the speculative
                # template loop and go straight to the CSDB
                # fallback, which is where the screenshots
                # actually live.
                data = None
                used_label = ""
                last_error = ""
                import urllib.request, urllib.error

                if data is None:
                    # CSDB screenshot fetch via viewpic.php +
                    # og:image meta tag (most reliable), with
                    # /gfx/releases/<bucket>/<id>.<ext> path
                    # construction as fallback.
                    try:
                        csdb_data, csdb_url = (
                            self._try_csdb_screenshot())
                        if csdb_data:
                            data = csdb_data
                            used_label = f"csdb"
                    except Exception as e:
                        last_error = (last_error
                                      + f"; csdb: {e}")

                if data is None:
                    try:
                        self.cache_dir.mkdir(
                            parents=True, exist_ok=True)
                        # Marker line lets future code know this
                        # negative entry was written by the
                        # current logic (viewpic.php + multi-ext
                        # probe), so we trust it instead of
                        # re-probing on every selection.
                        neg_file.write_bytes(neg_marker)
                    except Exception:
                        pass
                    if not self.cancelled:
                        self.done.emit(
                            False, None, "no image found",
                            last_error)
                    return

                # Write to cache
                try:
                    self.cache_dir.mkdir(
                        parents=True, exist_ok=True)
                    cache_file.write_bytes(data)
                except Exception:
                    # Cache write fail isn't fatal - we still
                    # got the image, just won't cache it.
                    pass

                if not self.cancelled:
                    self.done.emit(True, data, used_label, "")
            except Exception as e:
                if not self.cancelled:
                    self.done.emit(False, None, "", str(e))

        def _try_csdb_screenshot(self):
            """Scrape the CSDB release viewpic page for a screenshot.

            Assembly64 entry ids ARE CSDB release ids. We hit
            https://csdb.dk/release/viewpic.php?id=<id>&zoom=1
            instead of /release/?id=<id> because:

              - viewpic.php's <meta property="og:image"> always
                points at the actual screenshot URL like
                https://csdb.dk/gfx/releases/259000/259546.gif
              - the regular release page's og:image is often the
                site logo, not a release screenshot, so we'd
                false-positive into "no screenshot" for releases
                that DO have one

            Returns (image_bytes, source_url) on success, or
            (None, None) if no screenshot found. Doesn't raise
            for network errors - caller wraps in try/except.

            CSDB serves screenshots as .gif (most common - they
            preserve original screen captures), .png (modern
            converters), or .jpg (rare). The og:image tag tells
            us which, but if og parsing fails we probe all three
            as the last resort.
            """
            import urllib.request, urllib.error, re
            page_url = (f"https://csdb.dk/release/viewpic.php"
                        f"?id={self.entry_id}&zoom=1")
            req = urllib.request.Request(
                page_url,
                headers={
                    "User-Agent": "Quopus",
                    "Accept": "text/html,*/*",
                })
            try:
                with urllib.request.urlopen(
                        req, timeout=self.client.timeout) as r:
                    if r.status != 200:
                        return (None, None)
                    html = r.read().decode(
                        "utf-8", errors="replace")
            except (urllib.error.HTTPError,
                    urllib.error.URLError):
                return (None, None)

            # Primary path: og:image in the viewpic.php page is
            # always the canonical screenshot URL.
            og_re = re.compile(
                r'<meta\s+(?:property|name)\s*=\s*["\']og:image["\']'
                r'\s+content\s*=\s*["\']([^"\']+)["\']',
                re.IGNORECASE)
            m = og_re.search(html)
            urls_to_try = []
            if m:
                cand = m.group(1)
                # Normalise relative URLs
                if cand.startswith("//"):
                    cand = "https:" + cand
                elif cand.startswith("/"):
                    cand = "https://csdb.dk" + cand
                elif not cand.startswith("http"):
                    cand = "https://csdb.dk/" + cand
                urls_to_try.append(cand)

            # Fallback URLs: construct by convention. CSDB stores
            # screenshots in id-bucket folders, with one of three
            # extensions. Try each.
            try:
                eid = int(self.entry_id)
                bucket = (eid // 1000) * 1000
                for ext in ("gif", "png", "jpg"):
                    u = (f"https://csdb.dk/gfx/releases/"
                         f"{bucket}/{eid}.{ext}")
                    if u not in urls_to_try:
                        urls_to_try.append(u)
            except (TypeError, ValueError):
                pass

            if not urls_to_try:
                return (None, None)

            for img_url in urls_to_try:
                # Filter: reject anything not in the screenshot
                # paths (e.g. site logo placeholder).
                if not any(p in img_url for p in
                           ("/gfx/releases/", "/gfx/screens/")):
                    continue
                if self.cancelled:
                    return (None, None)
                img_req = urllib.request.Request(
                    img_url,
                    headers={
                        "User-Agent": "Quopus",
                        "Accept": "image/*",
                        "Referer": page_url,
                    })
                try:
                    with urllib.request.urlopen(
                            img_req,
                            timeout=self.client.timeout) as r:
                        if r.status != 200:
                            continue
                        ctype = (r.headers.get_content_type() or "")
                        if not ctype.startswith("image/"):
                            continue
                        return (r.read(), img_url)
                except urllib.error.HTTPError:
                    # 404 on this extension - try the next one
                    continue
                except urllib.error.URLError:
                    # Network error, give up entirely
                    return (None, None)
            return (None, None)

    class _DownloadWorker(QThread):
        """Download one file in the background."""
        done = pyqtSignal(bool, object, str, str)
        # ok, bytes_or_None, filename, error

        def __init__(self, client, entry_id, category, file_info,
                       parent=None):
            super().__init__(parent)
            self.client = client
            self.entry_id = entry_id
            self.category = category
            self.file_info = file_info

        def run(self):
            try:
                data = self.client.download_file(
                    self.entry_id, self.category,
                    file_info=self.file_info)
                self.done.emit(True, data, self.file_info.name, "")
            except ASM64APIError as e:
                self.done.emit(False, None,
                                 self.file_info.name, str(e))

    class _SortableItem(QTreeWidgetItem):
        """QTreeWidgetItem that compares by stored numeric data
        when present, falling back to text. Lets the user click
        Year / Rating headers and see real numeric ordering instead
        of lexical ("9" > "10" wrong-style)."""

        def __lt__(self, other):
            col = (self.treeWidget().sortColumn()
                   if self.treeWidget() else 0)
            a = self.data(col, Qt.ItemDataRole.UserRole + 1)
            b = other.data(col, Qt.ItemDataRole.UserRole + 1)
            if a is not None and b is not None:
                try:
                    return float(a) < float(b)
                except (TypeError, ValueError):
                    pass
            return self.text(col) < other.text(col)

    class _BrowserDialog(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("Assembly64 Browser")
            self.resize(1100, 700)
            # Restore window geometry from last session
            from quopus_lib.window_state import install_window_state
            install_window_state(self, "asm64_browser")
            self.client = ASM64Client()
            self.on_run = on_run
            self.on_mount = on_mount
            self.on_save = on_save
            self._search_worker = None
            self._files_worker = None
            self._download_worker = None
            self._year_sweep_worker = None
            self._paginated_worker = None
            self._preview_worker = None
            # Animated-preview state. When the previewed image is
            # an animated GIF / APNG we play it via QMovie, which
            # needs its source data alive for the whole playback
            # lifetime. We hold references here so Python's GC
            # doesn't sweep them mid-animation.
            self._preview_movie = None
            self._preview_buffer = None
            self._preview_buffer_bytes = None
            # The raw image bytes of the currently-shown preview,
            # if any. We keep this so the zoom popup can re-decode
            # at full resolution (or play the animated GIF/APNG
            # again in its own QMovie).
            self._preview_data = None
            self._preview_label = ""
            # Screenshot cache lives under cache/asm64_screenshots/
            # next to the existing search-results cache. Per-
            # release files keyed on entry id + category.
            from pathlib import Path as _Path
            try:
                from .config import CACHE_DIR
                self._preview_cache = (
                    _Path(CACHE_DIR) / "asm64_screenshots")
            except Exception:
                # Fall back to the package directory if config
                # isn't available - cache is best-effort anyway
                self._preview_cache = (
                    _Path(__file__).parent.parent / "cache"
                    / "asm64_screenshots")
            self._current_entry = None
            self._current_files = []
            self._download_pending_action = None
            # action: "run", "mount", "save", "savedialog"
            # Track the filter params for whatever's currently in
            # self.results - used when persisting on a fresh search.
            self._last_search_params = {}

            self._build_ui()
            self._load_favorites_into_list()
            self._load_searches_into_combo()
            # Populate the category dropdown from the server's
            # /presets endpoint. This makes a single HTTP call
            # the first time (then is cached on the client), so
            # the dialog opens promptly even if /presets is
            # slow - the combo will just say "(any)" until the
            # response arrives. Failure to load is silent: the
            # combo stays as "(any)" only.
            self._populate_category_combo()
            # Repopulate the last-shown search session, if any, so
            # reopening the dialog feels like resuming where the
            # user left off rather than starting from a blank page.
            self._restore_last_results()

        def _populate_category_combo(self):
            """Fetch /presets via the client and fill the category
            filter dropdown with the server's known categories.

            Works against the modern Assembly64 schema where
            /presets is a list of sections, each with an
            aqlKey-keyed values array. The "category" section
            (type=='category') gives us the picker items - if
            no such section exists, we fall back to ALL aqlKeys
            from all sections, which is at least useful as a
            "type something" reference.
            """
            try:
                presets = self.client.get_presets()
            except Exception:
                return
            # Force the lookup cache to populate as a side effect
            # so the renderer can resolve names later.
            try:
                _ = self.client.get_category_name("")
            except Exception:
                pass
            # Find the "category" section if present
            cat_section = None
            other_sections = []
            if isinstance(presets, list):
                for sec in presets:
                    if not isinstance(sec, dict):
                        continue
                    t = str(sec.get("type", "")).lower()
                    if t == "category" or t == "categories":
                        cat_section = sec
                    else:
                        other_sections.append(sec)
            if cat_section is None:
                # No explicit category section. Don't pollute
                # the picker with random other keys - leave it
                # at "(any)" so the user can still search by
                # name/group/year and the column renderer will
                # show server-resolved names anyway.
                return
            values = cat_section.get("values", [])
            if not isinstance(values, list):
                return
            # Save current selection so we can restore it
            current_key = self.cmb_category.currentData() or ""
            self.cmb_category.blockSignals(True)
            self.cmb_category.clear()
            self.cmb_category.addItem("(any)", "")
            for v in values:
                if not isinstance(v, dict):
                    continue
                key = (v.get("aqlKey")
                        or v.get("key")
                        or v.get("id")
                        or "")
                name = (v.get("name")
                         or v.get("label")
                         or v.get("title")
                         or "")
                if key and name:
                    self.cmb_category.addItem(
                        f"{name}", str(key))
            # Restore previous selection if it still exists
            if current_key:
                idx = self.cmb_category.findData(current_key)
                if idx >= 0:
                    self.cmb_category.setCurrentIndex(idx)
            self.cmb_category.blockSignals(False)

        def _build_ui(self):
            outer = QVBoxLayout(self)
            outer.setContentsMargins(8, 8, 8, 8)
            outer.setSpacing(6)

            # Top: filter form
            filter_box = self._build_filter_form()
            outer.addWidget(filter_box)

            # Middle: splitter with results | details
            split = QSplitter(Qt.Orientation.Horizontal)
            # Left: results tree
            left = QWidget()
            left_lay = QVBoxLayout(left)
            left_lay.setContentsMargins(0, 0, 0, 0)
            left_lay.setSpacing(2)
            # Header row above the results tree: label on the left,
            # Save / Load buttons on the right so the user can
            # snapshot the current result list to a named file or
            # restore a previous snapshot. Useful for keeping
            # curated collections ("My Quantum picks.json",
            # "Demoparty haul.json") independent of the auto-cache.
            res_header = QHBoxLayout()
            res_header.setContentsMargins(0, 0, 0, 0)
            res_header.setSpacing(4)
            res_header.addWidget(QLabel("Results:"))
            # Named snapshot picker: dropdown of previously-saved
            # snapshots + Save-as / Delete buttons. The combobox
            # has "(none)" at index 0 so the user can clear the
            # selection without firing a load.
            res_header.addWidget(QLabel("  Saved:"))
            self.cmb_saved_results = QComboBox()
            self.cmb_saved_results.setMinimumWidth(180)
            self.cmb_saved_results.setToolTip(
                "Previously saved result snapshots. Pick one to\n"
                "load it back into the table. Snapshots are\n"
                "stored in config/asm64_saved_results.json next\n"
                "to your other Quopus config.")
            self.cmb_saved_results.currentIndexChanged.connect(
                self._on_saved_results_picked)
            res_header.addWidget(self.cmb_saved_results)
            self.btn_save_named = QPushButton("Save as...")
            self.btn_save_named.setFixedWidth(80)
            self.btn_save_named.setToolTip(
                "Save the current result table under a name.\n"
                "Different from the JSON file export - named\n"
                "snapshots live inside Quopus's config and show\n"
                "up in the dropdown for one-click reload.")
            self.btn_save_named.clicked.connect(
                self._on_save_named_results)
            res_header.addWidget(self.btn_save_named)
            self.btn_delete_named = QPushButton("Delete")
            self.btn_delete_named.setFixedWidth(60)
            self.btn_delete_named.setEnabled(False)
            self.btn_delete_named.setToolTip(
                "Remove the currently-selected named snapshot.\n"
                "The .json export file (if any) is left alone.")
            self.btn_delete_named.clicked.connect(
                self._on_delete_named_results)
            res_header.addWidget(self.btn_delete_named)
            res_header.addStretch(1)
            self.btn_results_save = QPushButton("Save Results...")
            self.btn_results_save.setFixedWidth(120)
            self.btn_results_save.setToolTip(
                "Export the current result list to a named JSON\n"
                "file you pick with a save dialog. Useful for\n"
                "sharing or backup. For quick reload inside\n"
                "Quopus, use 'Save as...' to the left instead.")
            self.btn_results_save.clicked.connect(
                self._on_save_results_to_file)
            res_header.addWidget(self.btn_results_save)
            self.btn_results_load = QPushButton("Load Results...")
            self.btn_results_load.setFixedWidth(120)
            self.btn_results_load.setToolTip(
                "Load a previously exported JSON file from disk.\n"
                "Replaces what's currently in the table.")
            self.btn_results_load.clicked.connect(
                self._on_load_results_from_file)
            res_header.addWidget(self.btn_results_load)
            left_lay.addLayout(res_header)
            # Populate the named snapshot dropdown at startup.
            # Done before the rest of the UI so the first paint
            # already has the populated combobox.
            self._refresh_saved_results_combo()
            self.results = QTreeWidget()
            # Removed "Handle" and "Rating" columns from the
            # results tree. Handle was misleading (asm64's
            # /search/aql only returns one handle per release,
            # often just the primary credit). Rating is shown
            # in the detail panel instead - having it as a
            # column made the tree feel cluttered with not-very
            # actionable data.
            self.results.setHeaderLabels([
                "Name", "Group", "Year", "Category"])
            from quopus_lib.window_state import install_table_state
            install_table_state(self.results, "asm64_browser:results")
            # 4 cols now: Name (wide), Group, Year, Category.
            # Category gets a bit more room since type names
            # like "Crack Intro" / "Graphics Collection" /
            # "Music Collection" can be long.
            self.results.setColumnWidth(0, 340)
            self.results.setColumnWidth(1, 160)
            self.results.setColumnWidth(2, 55)
            self.results.setColumnWidth(3, 150)
            self.results.setRootIsDecorated(False)
            self.results.setAlternatingRowColors(True)
            # Explicit row colors via QSS. Without these, Linux
            # Qt themes (especially dark ones, but also some
            # GTK-styled defaults) compute the alternate-row
            # color from the palette in a way that ends up black
            # on black, making every second row unreadable. We
            # hard-code light-grey-on-white so the readability
            # works regardless of the user's system theme.
            self.results.setStyleSheet(
                "QTreeWidget { "
                "  background-color: #ffffff; "
                "  alternate-background-color: #f0f0f0; "
                "  color: #000000; "
                "} "
                "QTreeWidget::item:selected { "
                "  background-color: #5566ff; "
                "  color: #ffffff; "
                "}")
            # Allow click-to-sort on any column. Click "Year" to
            # see whether the result set includes older entries
            # or only the most recent ones.
            self.results.setSortingEnabled(True)
            self.results.sortByColumn(
                -1, Qt.SortOrder.AscendingOrder)  # natural order
            self.results.itemSelectionChanged.connect(
                self._on_result_selected)
            self.results.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            self.results.customContextMenuRequested.connect(
                self._on_results_context_menu)
            left_lay.addWidget(self.results, 1)
            split.addWidget(left)

            # Right: detail panel with files list
            right = QWidget()
            right_lay = QVBoxLayout(right)
            right_lay.setContentsMargins(0, 0, 0, 0)
            right_lay.setSpacing(4)
            self.lbl_detail = QLabel("(select a result for details)")
            self.lbl_detail.setWordWrap(True)
            self.lbl_detail.setStyleSheet(
                "padding: 6px; background: #f0f0f0;")
            right_lay.addWidget(self.lbl_detail)

            # Buttons row above files
            br = QHBoxLayout()
            self.btn_csdb = QPushButton("Open CSDB page")
            self.btn_csdb.setToolTip(
                "Open the release page on csdb.dk in a browser.\n"
                "Only available for CSDB-sourced entries.")
            self.btn_csdb.setEnabled(False)
            self.btn_csdb.clicked.connect(self._on_open_csdb)
            br.addWidget(self.btn_csdb)
            self.btn_fav = QPushButton("Add to favorites")
            self.btn_fav.setEnabled(False)
            self.btn_fav.clicked.connect(self._on_favorite_toggle)
            br.addWidget(self.btn_fav)
            br.addStretch(1)
            right_lay.addLayout(br)

            right_lay.addWidget(QLabel("Files in release:"))
            self.files = QTreeWidget()
            self.files.setHeaderLabels(["Name", "Type", "Size"])
            self.files.setColumnWidth(0, 280)
            self.files.setColumnWidth(1, 60)
            self.files.setRootIsDecorated(False)
            self.files.setAlternatingRowColors(True)
            # Same readable color scheme as the results tree -
            # don't let Linux Qt themes break the alternating
            # rows.
            self.files.setStyleSheet(
                "QTreeWidget { "
                "  background-color: #ffffff; "
                "  alternate-background-color: #f0f0f0; "
                "  color: #000000; "
                "} "
                "QTreeWidget::item:selected { "
                "  background-color: #5566ff; "
                "  color: #ffffff; "
                "}")
            self.files.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            self.files.customContextMenuRequested.connect(
                self._on_files_context_menu)
            right_lay.addWidget(self.files, 1)

            # File action buttons
            far = QHBoxLayout()
            self.btn_run = QPushButton("Run on U64")
            self.btn_run.setToolTip(
                "Download the selected file and run it on the U64.\n"
                "Works for PRG / CRT / SID. Disk images are auto-\n"
                "redirected to 'Mount on Drive A'.")
            self.btn_run.setEnabled(False)
            self.btn_run.clicked.connect(self._on_run)
            far.addWidget(self.btn_run)
            self.btn_mount = QPushButton("Mount on Drive A")
            self.btn_mount.setEnabled(False)
            self.btn_mount.clicked.connect(self._on_mount)
            far.addWidget(self.btn_mount)
            self.btn_dl = QPushButton("Download to disk")
            self.btn_dl.setEnabled(False)
            self.btn_dl.clicked.connect(self._on_download_to_disk)
            far.addWidget(self.btn_dl)
            # Run in emulator: downloads to temp first, then
            # launches the configured C64 emulator on the result.
            # Works on PRG / CRT / D64 / etc.; for non-C64 file
            # types in the result (e.g. plain SID we'd rather
            # play with the built-in SID player) the button is
            # still enabled but the user can also use the "Run
            # on device" path for SID hardware playback.
            self.btn_run_emu = QPushButton("Run in Emulator")
            self.btn_run_emu.setEnabled(False)
            self.btn_run_emu.setToolTip(
                "Download the selected file to a temp folder\n"
                "and launch it in your configured C64 emulator\n"
                "(VICE / x64sc / etc). Same emulator that\n"
                "Quopus uses for 'Run in Emulator' actions in\n"
                "the lister and DB browser.")
            self.btn_run_emu.clicked.connect(
                self._on_run_in_emulator)
            far.addWidget(self.btn_run_emu)
            far.addStretch(1)
            right_lay.addLayout(far)

            # Favorites quick-list (below file actions)
            right_lay.addWidget(QLabel("Favorites:"))
            self.lst_favs = QListWidget()
            self.lst_favs.setMaximumHeight(100)
            self.lst_favs.itemDoubleClicked.connect(
                self._on_favorite_loaded)
            right_lay.addWidget(self.lst_favs)

            split.addWidget(right)

            # Right-most pane: screenshot preview. Loaded lazily
            # on result-selection - we fetch the image from the
            # Assembly64 API (or fall back to CSDB if available)
            # and display it scaled. Empty placeholder until the
            # user picks something. The pane is collapsible via
            # the splitter handle so people who don't care about
            # screenshots can drag it shut.
            preview = QWidget()
            prev_lay = QVBoxLayout(preview)
            prev_lay.setContentsMargins(4, 4, 4, 4)
            prev_lay.setSpacing(4)
            prev_lay.addWidget(QLabel("Preview:"))
            # Direct QLabel, no QScrollArea: we scale the image
            # to fit the label so the scroll bars never had
            # anything to scroll. The previous QScrollArea also
            # capped the label's inner size to its sizeHint,
            # which is why the preview looked narrow regardless
            # of how wide the splitter pane got. Without the
            # scroll area, the label expands to the full pane.
            preview.setMinimumWidth(420)
            self.lbl_preview = QLabel(
                "(select a release to preview)")
            # Top-center alignment so the screenshot sits at the
            # top of the pane, with empty space below it. Without
            # this the QLabel centers vertically and the image
            # ends up floating in the middle with huge background
            # margins above and below.
            self.lbl_preview.setAlignment(
                Qt.AlignmentFlag.AlignHCenter
                | Qt.AlignmentFlag.AlignTop)
            self.lbl_preview.setStyleSheet(
                "background: #1a1a1a; color: #aaa; "
                "padding: 6px;")
            self.lbl_preview.setMinimumHeight(180)
            # Horizontal: fill the pane. Vertical: just take what
            # the content needs (Preferred) - the empty status
            # row below is fine, we don't want a 600px black box.
            from PyQt6.QtWidgets import QSizePolicy
            self.lbl_preview.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred)
            # Click-to-zoom: clicking on the preview opens a
            # popup with the screenshot at full resolution. We
            # use a custom mousePressEvent installed via a small
            # wrapper instead of subclassing QLabel - keeps the
            # constructor flat and the wiring discoverable.
            self.lbl_preview.setCursor(Qt.CursorShape.PointingHandCursor)
            self.lbl_preview.setToolTip(
                "Click to view full size")
            def _preview_clicked(ev):
                # Only trigger on left-click, ignore right-click
                # (which the user may use for native context menu)
                if ev.button() == Qt.MouseButton.LeftButton:
                    self._show_preview_zoom()
            self.lbl_preview.mousePressEvent = _preview_clicked
            prev_lay.addWidget(self.lbl_preview, 0)
            prev_lay.addStretch(1)
            # Status line for the preview pane: shows how the
            # image was sourced (API direct, CSDB fallback, or
            # cache hit) so the user knows what they're looking
            # at without opening a debug log.
            self.lbl_preview_status = QLabel("")
            self.lbl_preview_status.setStyleSheet(
                f"color: #888; font-size: {scaled_font_px(10)}px;")
            self.lbl_preview_status.setWordWrap(True)
            prev_lay.addWidget(self.lbl_preview_status)
            split.addWidget(preview)

            split.setStretchFactor(0, 2)
            split.setStretchFactor(1, 2)
            split.setStretchFactor(2, 2)
            split.setSizes([520, 380, 480])
            outer.addWidget(split, 1)

            # Bottom: status + close
            bot = QHBoxLayout()
            self.lbl_status = QLabel("ready")
            self.lbl_status.setStyleSheet(
                "padding: 4px; color: #666;")
            bot.addWidget(self.lbl_status, 1)
            btn_endp = QPushButton("Endpoints...")
            btn_endp.setToolTip(
                "Override the API URL paths if the server has\n"
                "moved them around. Mostly diagnostic.")
            btn_endp.clicked.connect(self._on_endpoints_edit)
            bot.addWidget(btn_endp)
            btn_close = QPushButton("Close")
            btn_close.clicked.connect(self.close)
            bot.addWidget(btn_close)
            outer.addLayout(bot)

        def _build_filter_form(self):
            box = QGroupBox("Search filters")
            form = QFormLayout(box)
            form.setContentsMargins(8, 6, 8, 6)
            form.setSpacing(4)

            # Free-text fields
            self.ed_name = QLineEdit()
            self.ed_name.setPlaceholderText(
                "e.g. archon, @mason, -fairlight")
            self.ed_name.setToolTip(
                "Name search supports three modes:\n"
                "  archon       - contains 'archon' "
                "(substring match)\n"
                "  @mason       - exact match: filename must "
                "be exactly 'mason'\n"
                "  -fairlight   - wildcard: searches "
                "'fairlight' in name, group, handle AND "
                "event fields all at once\n"
                "\n"
                "The wildcard form (-X) is the most powerful "
                "one - it's the fastest way to find anything "
                "related to a scener or group without having "
                "to pick the right field first.")
            self.ed_name.returnPressed.connect(self._on_search)
            self.ed_group = QLineEdit()
            self.ed_group.setPlaceholderText(
                "e.g. censor, @finnish gold")
            self.ed_group.setToolTip(
                "Group search:\n"
                "  censor       - contains 'censor'\n"
                "  @finnish gold - exact match for "
                "'finnish gold'")
            self.ed_group.returnPressed.connect(self._on_search)

            top_row = QHBoxLayout()
            top_row.addWidget(QLabel("Name:"))
            top_row.addWidget(self.ed_name, 1)
            top_row.addSpacing(10)
            top_row.addWidget(QLabel("Group:"))
            top_row.addWidget(self.ed_group, 1)
            top_widget = QWidget()
            top_widget.setLayout(top_row)
            form.addRow(top_widget)

            # AQL-supported filters: handle, year, type, category.
            # The Assembly64 API uses AQL = "Assembly Query Language"
            # of the form (name:"x") & (type:prg). The fields here
            # map 1:1 to AQL clauses.
            self.ed_handle = QLineEdit()
            self.ed_handle.setPlaceholderText(
                "scener handle (e.g. Tasco, @JCH)")
            self.ed_handle.setToolTip(
                "Handle search:\n"
                "  Tasco        - contains 'Tasco'\n"
                "  @JCH         - exact match for 'JCH'")
            self.ed_handle.returnPressed.connect(self._on_search)
            self.ed_year = QLineEdit()
            self.ed_year.setMaximumWidth(80)
            self.ed_year.setPlaceholderText("year")
            self.ed_year.returnPressed.connect(self._on_search)
            self.cmb_type = QComboBox()
            self.cmb_type.setEditable(True)
            for t in ("", "prg", "crt", "d64", "d71", "d81",
                      "sid", "mod", "tap", "t64", "zip", "txt"):
                self.cmb_type.addItem(t or "(any type)", t)
            self.cmb_type.setToolTip(
                "File extension filter passed as AQL (type:prg).\n"
                "You can also type any custom extension.")
            # Category filter: a combobox with the Assembly64
            # category names from the server's /presets endpoint.
            # Modern Assembly64 uses string aqlKey identifiers
            # ('csdb', 'gamebase', 'demo', ...) rather than
            # numeric ids; the picker stores the aqlKey as
            # itemData and sends it verbatim in the AQL
            # category:<key> filter.
            #
            # Population happens in a later step (_populate_
            # category_combo) once the client's /presets cache
            # is loaded - we can't do it here because the cache
            # is lazy-loaded on first access. Until that runs,
            # the combo just shows "(any)" so the dialog opens
            # promptly.
            self.cmb_category = QComboBox()
            self.cmb_category.setMaximumWidth(220)
            self.cmb_category.addItem("(any)", "")
            self.cmb_category.setToolTip(
                "Filter results by Assembly64 category. Pick "
                "(any) to skip this filter.\n"
                "Values come from the server's /presets endpoint "
                "and are stored as aqlKey strings.")
            self.cmb_category.currentIndexChanged.connect(
                lambda _i: self._update_aql_preview())

            mid_row = QHBoxLayout()
            mid_row.addWidget(QLabel("Handle:"))
            mid_row.addWidget(self.ed_handle, 1)
            mid_row.addSpacing(8)
            mid_row.addWidget(QLabel("Year:"))
            mid_row.addWidget(self.ed_year)
            mid_row.addSpacing(8)
            mid_row.addWidget(QLabel("Type:"))
            mid_row.addWidget(self.cmb_type)
            mid_row.addSpacing(8)
            mid_row.addWidget(QLabel("Category:"))
            mid_row.addWidget(self.cmb_category)
            mid_widget = QWidget()
            mid_widget.setLayout(mid_row)
            form.addRow(mid_widget)

            # AQL preview - shows the query that will be sent so
            # advanced users can sanity-check what's happening.
            self.lbl_aql = QLabel("AQL: (none)")
            self.lbl_aql.setStyleSheet(
                "font-family: 'Consolas', monospace; "
                f"font-size: {scaled_font_px(11)}px; color: #555; padding: 2px;")
            form.addRow(self.lbl_aql)

            # Search button + saved searches dropdown
            bottom_row = QHBoxLayout()
            self.btn_search = QPushButton("Search")
            self.btn_search.setStyleSheet(
                "QPushButton { font-weight: bold; }")
            self.btn_search.clicked.connect(self._on_search)
            bottom_row.addWidget(self.btn_search)

            self.btn_clear = QPushButton("Clear")
            self.btn_clear.clicked.connect(self._on_clear)
            bottom_row.addWidget(self.btn_clear)

            self.btn_latest = QPushButton("Latest")
            self.btn_latest.setToolTip(
                "Clear all filters and sort by 'recently added'\n"
                "to see what's just appeared on Assembly64.")
            self.btn_latest.clicked.connect(self._on_latest)
            bottom_row.addWidget(self.btn_latest)

            # Top XX dropdown. Each entry is a preset that
            # combines a category filter with sort=rating
            # descending and a row limit. The Assembly64 server
            # honours sort+order as URL params alongside the
            # AQL query; the actual "Top 200 Overall" feel
            # comes from rating-sorted browsing.
            from PyQt6.QtWidgets import QMenu, QToolButton
            self.btn_top = QToolButton()
            self.btn_top.setText("Top...")
            self.btn_top.setToolTip(
                "Quick-fire rating-sorted lists:\n"
                "Top 200 overall, Top 100 demos / games /\n"
                "music / intros / etc. Pulls from the server\n"
                "sorted by rating descending - same scoring\n"
                "you see in the official Assembly64 client.")
            self.btn_top.setPopupMode(
                QToolButton.ToolButtonPopupMode.InstantPopup)
            top_menu = QMenu(self.btn_top)
            # (label, category aqlKey, limit). category='' means
            # "all categories" -> Top Overall.
            #
            # IMPORTANT: only categories the /charts endpoint
            # actually supports are listed. Per the Assembly64
            # dev team, /charts/{cat} is currently only defined
            # for these six aqlKeys. Other categories from the
            # /presets list (intros, mags, charts, bbs, etc.)
            # return 404 or empty - we leave them out so the
            # menu doesn't list options that don't work.
            #
            # 'onefiledemos' is an extra chart slot the server
            # exposes specifically (one-file demos are a
            # CSDB release type, not a top-level category, but
            # the chart is built and served under this key).
            #
            # 'Overall' was removed - /charts without a category
            # returns 404 on this Assembly64 build.
            top_presets = [
                ("Top 100 Demos",          "demos",        100),
                ("Top 100 Onefile Demos",  "onefiledemos", 100),
                ("Top 100 Games",          "games",        100),
                ("Top 100 Music",          "music",        100),
                ("Top 100 Graphics",       "graphics",     100),
                ("Top 50 Tools",           "tools",         50),
            ]
            for label, cat_key, limit in top_presets:
                act = top_menu.addAction(label)
                # Use default-arg trick to capture loop vars
                act.triggered.connect(
                    lambda _checked, c=cat_key, n=limit,
                           lab=label:
                    self._on_top_preset(c, n, lab))
            self.btn_top.setMenu(top_menu)
            # Blue button styling - matches the rest of the
            # Quopus accent-button palette. Was yellow before;
            # user feedback was that the yellow looked off
            # alongside everything else.
            # Padding kept minimal so the QToolButton's overall
            # height matches the adjacent QPushButtons. QToolButton
            # defaults to a more vertically padded look than
            # QPushButton, which made it tower over the rest of
            # the row - explicit min-height + matching px-padding
            # equalises it.
            self.btn_top.setStyleSheet(
                "QToolButton { "
                "background: #4A90E2; color: white; "
                "padding: 1px 8px; border: 1px solid #000; "
                "font-weight: bold; min-height: 18px; "
                "min-width: 60px; } "
                "QToolButton::menu-indicator { "
                "subcontrol-origin: padding; "
                "subcontrol-position: right center; } "
                "QToolButton:pressed { "
                "background: #000; color: #4A90E2; }")
            bottom_row.addWidget(self.btn_top)

            # Note: a "Get all years" button used to live here.
            # Removed - the use case (auto-paginate a single
            # query across every year to work around the
            # server-side response cap) has been replaced by:
            #   - The /charts/{category} top endpoint for
            #     rating-sorted browsing
            #   - The paginated AQL search (50 per page)
            # If we need year-sweep again it can be wired up
            # from the saved-searches menu - the worker class
            # _YearSweepWorker and _on_year_sweep handler are
            # still in place for that.

            bottom_row.addStretch(1)

            bottom_row.addWidget(QLabel("Saved searches:"))
            self.cmb_saved = QComboBox()
            self.cmb_saved.setMinimumWidth(160)
            self.cmb_saved.currentIndexChanged.connect(
                self._on_saved_picked)
            bottom_row.addWidget(self.cmb_saved)
            btn_save_search = QPushButton("Save...")
            btn_save_search.clicked.connect(self._on_save_search)
            bottom_row.addWidget(btn_save_search)
            btn_delete_search = QPushButton("Delete")
            btn_delete_search.clicked.connect(self._on_delete_search)
            bottom_row.addWidget(btn_delete_search)

            bottom_widget = QWidget()
            bottom_widget.setLayout(bottom_row)
            form.addRow(bottom_widget)

            return box

        # -------- Search ------------------------------------------

        def _gather_params(self) -> dict:
            # Type combo is editable; prefer the typed text over the
            # combo data so the user can supply custom extensions.
            type_text = self.cmb_type.currentText().strip()
            if type_text == "(any type)":
                type_text = ""
            return {
                "name":      self.ed_name.text().strip(),
                "group":     self.ed_group.text().strip(),
                "handle":    self.ed_handle.text().strip(),
                "year":      self.ed_year.text().strip(),
                "file_type": type_text,
                # Category is a string aqlKey ('csdb', 'demo',
                # etc.) stored as itemData on the combobox row.
                # Empty string means "(any)" -> omit cat= filter.
                "category":  str(
                    self.cmb_category.currentData() or "").strip(),
            }

        def _update_aql_preview(self):
            p = self._gather_params()
            aql = ASM64Client.build_aql(**p)
            self.lbl_aql.setText(f"AQL: {aql}")

        def _on_search(self):
            if (self._search_worker is not None
                    and self._search_worker.isRunning()):
                return
            if (getattr(self, "_paginated_worker", None) is not None
                    and self._paginated_worker.isRunning()):
                return
            if (getattr(self, "_year_sweep_worker", None) is not None
                    and self._year_sweep_worker.isRunning()):
                return
            self._update_aql_preview()
            self.btn_search.setEnabled(False)
            params = self._gather_params()
            self._last_search_params = dict(params)

            # Use the official Assembly64 paginated search endpoint
            # (/leet/search/aql/<start>/<count>) that the upstream
            # client uses. The server returns pages in its natural
            # sort order; we walk through them until a short page
            # signals end-of-data. This replaces the old per-year
            # sweep workaround:
            #
            #   - 1 request per page (~250 entries each) instead
            #     of 47 requests one per year
            #   - server-side sort preserved (matches what the
            #     official Assembly64 app shows)
            #   - no client-side dedup or year-bucketing needed
            #   - much faster for typical queries (a few hundred
            #     hits = 1-2 page requests, ~1 second)
            #
            # Streaming chunks paint rows into the tree as each
            # page arrives, so the user sees results immediately
            # rather than waiting for the whole walk.
            aql = ASM64Client.build_aql(**params)
            # Sanity: if AQL is essentially empty (no filters set),
            # the full database walk is ~50000+ entries and would
            # take many seconds. Warn first.
            non_empty = [v for v in params.values() if v]
            if not non_empty:
                reply = QMessageBox.question(
                    self, "Search all entries",
                    "No filters are set, so Quopus would walk\n"
                    "the entire Assembly64 database one page at\n"
                    "a time (~50,000+ entries).\n\n"
                    "This may take 10-30 seconds.\n\n"
                    "Run the full walk anyway?\n"
                    "Choose No to do a single page-limited\n"
                    "query instead (faster, fewer results).",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    self.lbl_status.setText("searching...")
                    self._search_worker = _SearchWorker(
                        self.client, params, self)
                    self._search_worker.done.connect(
                        self._on_search_done)
                    self._search_worker.start()
                    return

            # Clear the tree so streamed chunks start fresh.
            self.results.clear()
            self.lbl_status.setText("searching...")
            self._paginated_worker = _PaginatedSearchWorker(
                self.client, aql,
                page_size=250, parent=self)
            self._paginated_worker.chunk.connect(
                self._on_paginated_chunk)
            self._paginated_worker.done.connect(
                self._on_paginated_done)
            self._paginated_worker.start()

        def _on_paginated_chunk(self, state):
            """Streaming update from the paginated search worker.
            Each chunk represents one page of results from the
            Assembly64 server."""
            new_entries = state.get('new_entries') or []
            page = state.get('page', 0)
            total = state.get('total_unique', 0)
            self.lbl_status.setText(
                f"searching... page {page}, "
                f"{total} results so far")
            if not new_entries:
                return
            self.results.setSortingEnabled(False)
            for e in new_entries:
                self.results.addTopLevelItem(
                    self._make_result_item(e))
            self.results.setSortingEnabled(True)

        def _on_paginated_done(self, ok, entries, error):
            self.btn_search.setEnabled(True)
            if not ok and not entries:
                self.lbl_status.setText(
                    f"search error: {error}")
                QMessageBox.warning(self, "Assembly64 search",
                    f"Search failed:\n{error}")
                return
            n = len(entries)
            suffix = (" (cancelled, partial)"
                      if error == "(cancelled)"
                      else "")
            self.lbl_status.setText(
                f"{n} results{suffix}")
            # If chunks already populated the tree, don't rebuild
            already = self.results.topLevelItemCount()
            if already != n:
                self.results.clear()
                self.results.setSortingEnabled(False)
                for e in entries:
                    self.results.addTopLevelItem(
                        self._make_result_item(e))
                self.results.setSortingEnabled(True)
            if entries:
                save_last_results(
                    self._last_search_params, entries)
            # Auto-learn unknown CSDB release types in background.
            # The paginated search path was missing this hook -
            # only the legacy _on_search_done had it, but most
            # actual queries go through the paginated path.
            self._kick_off_type_learning(entries)

        def _on_search_done(self, ok, entries, error):
            self.btn_search.setEnabled(True)
            if not ok:
                # On failure: keep the previously-shown results so
                # the user doesn't lose what they were looking at.
                self.lbl_status.setText(f"error: {error}")
                QMessageBox.warning(self, "Assembly64 search",
                    f"Search failed:\n\n{error}\n\n"
                    "Possible causes:\n"
                    "- Server temporarily down or rate limiting\n"
                    "- AQL syntax error (check the AQL preview)\n"
                    "- 'Host not in allowlist' if your IP is blocked\n"
                    "- No active internet connection")
                return
            # Successful query: now swap the table contents
            self.results.clear()
            # Status line shows: count + year range + a heuristic
            # truncation warning if the server appears to have
            # capped the response. Year range is the most useful
            # signal that the result set spans many years vs
            # being clipped to "newest only".
            n = len(entries)
            years_present = sorted(
                {e.year for e in entries if e.year > 0})
            if years_present:
                if years_present[0] == years_present[-1]:
                    year_part = f", year {years_present[0]}"
                else:
                    year_part = (
                        f", years {years_present[0]}-{years_present[-1]}")
            else:
                year_part = ""
            hint = ""
            # Server commonly caps at small powers of 10 / standard
            # page sizes; if we hit one of these exactly, warn the
            # user that there may be older results truncated.
            if n in (10, 20, 25, 30, 50, 100, 200, 250, 500):
                hint = (" (server limit reached - narrow filters "
                        "to see more)")
            self.lbl_status.setText(
                f"{n} results{year_part}{hint}")
            # Temporarily disable sorting while we batch-insert so
            # we don't pay the per-row sort cost N times.
            self.results.setSortingEnabled(False)
            for e in entries:
                year_str = str(e.year) if e.year > 0 else ""
                cat_str = self._category_label(
                    e.category, e.site_category,
                    e.category_name)
                item = _SortableItem([
                    e.name, e.group, year_str, cat_str,
                ])
                # Numeric sort key for the Year column so
                # click-to-sort orders chronologically instead
                # of lexicographically.
                item.setData(2, Qt.ItemDataRole.UserRole + 1, e.year)
                # Whole-entry blob attached to the Name column for
                # selection lookup.
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                self.results.addTopLevelItem(item)
            self.results.setSortingEnabled(True)
            # Persist for the next dialog reopen. Skip when the
            # query came back empty - keeping the previous cache
            # alive is more useful than overwriting it with "0".
            if entries:
                save_last_results(
                    self._last_search_params, entries)
            # Auto-learn unknown CSDB release types in the
            # background. For each unique siteCategory in the
            # results that we don't have a name for yet, we'll
            # hit csdb.dk once to discover the type name and
            # cache it. Doesn't block the UI - if it takes too
            # long, future searches just keep showing 'Type #N'
            # until the learner catches up.
            self._kick_off_type_learning(entries)

        def _kick_off_type_learning(self, entries):
            """Spin up a one-shot QThread that resolves unknown
            CSDB release_type ids by sampling one release per id
            from csdb.dk. When done, the worker emits a signal
            and we re-render the result list so the now-known
            type names replace the 'Type #N' placeholders.

            Bounded: at most 8 unknown ids per search so a single
            click never hammers csdb with dozens of requests.
            Subsequent searches will pick up the remaining ones
            naturally.
            """
            try:
                # Build map: type_id -> sample_release_id for
                # all unknown types in this batch.
                unknown = {}
                for e in entries:
                    sc = e.site_category
                    if not sc or sc <= 0:
                        continue
                    if self.client.get_release_type_name(sc):
                        continue  # already known
                    if sc not in unknown:
                        # Save one example release id we can
                        # use to look this up. Asm64's id IS
                        # the CSDB release id.
                        try:
                            unknown[sc] = int(e.id)
                        except (TypeError, ValueError):
                            continue
                    if len(unknown) >= 8:
                        break
                if not unknown:
                    return
                # Don't double-kick if a previous learner is
                # still running.
                if (getattr(self, "_type_learner", None)
                        is not None
                        and self._type_learner.isRunning()):
                    return
                self._type_learner = _TypeLearnerWorker(
                    self.client, unknown, self)
                self._type_learner.learned.connect(
                    self._on_type_learned)
                self._type_learner.start()
            except Exception:
                # Auto-learning is best-effort. Silent failure
                # is fine here - worst case is the user sees
                # 'Type #N' for unknown ids until next session.
                pass

        def _on_type_learned(self, learned_map):
            """Worker finished discovering some new type names.
            Re-render the result list so the new names show
            instead of 'Type #N' placeholders.
            """
            if not learned_map:
                return
            # Iterate every row and refresh the category column
            # using the now-extended lookup.
            try:
                for i in range(self.results.topLevelItemCount()):
                    item = self.results.topLevelItem(i)
                    e = item.data(
                        0, Qt.ItemDataRole.UserRole)
                    if e is None:
                        continue
                    cat_str = self._category_label(
                        e.category, e.site_category,
                        e.category_name)
                    item.setText(3, cat_str)
            except Exception:
                pass

        def _category_label(self, cat, site_cat,
                              cat_name_hint: str = ""):
            """Best-effort label for the release-type column.

            The `siteCategory` field is the CSDB release_type id
            (e.g. 20 = 'C64 Crack', 1 = 'C64 Demo') - this is
            the actual content type of the release and what
            users care about seeing in the results list.

            The `cat` field is something else: Assembly64's own
            filter bucket (Demos / Games / Intros / Tools etc).
            It's NOT a per-release classification and is often
            zero - we only fall back to it when siteCategory is
            unavailable.

            Resolution order:
              1. siteCategory -> CSDB release type name (the
                 main signal)
              2. cat -> Assembly64 bucket name (filter
                 fallback, only when there's no siteCategory)
              3. cat_name_hint (server-supplied sibling field)
              4. Empty cell (better than a confusing number)
            """
            try:
                site_cat = int(site_cat) if site_cat else 0
            except (TypeError, ValueError):
                site_cat = 0

            # 1. CSDB release type (the per-release content type)
            if site_cat > 0:
                name = self.client.get_release_type_name(site_cat)
                if name:
                    # Strip the "C64 " prefix - it's noise in a
                    # C64-focused tool. "Crack" reads cleaner
                    # than "C64 Crack". Keep "Other Platform"
                    # prefixes intact since they're informative.
                    if name.startswith("C64 "):
                        name = name[4:]
                    return name
                # Unknown release type - show the raw id so the
                # user can report it back for the table to be
                # extended. Better than blank.
                return f"Type #{site_cat}"

            # 2/3. Fall back to the Assembly64 bucket category
            if cat_name_hint:
                return str(cat_name_hint).strip()
            if cat not in (None, "", 0):
                try:
                    name = self.client.get_category_name(cat)
                    if name:
                        return name
                except Exception:
                    pass
                if isinstance(cat, str):
                    return cat.strip()
            return ""

        def _on_clear(self):
            self.ed_name.clear()
            self.ed_group.clear()
            self.ed_handle.clear()
            self.ed_year.clear()
            # Reset category to "(any)" - the first entry
            self.cmb_category.setCurrentIndex(0)
            self.cmb_type.setCurrentIndex(0)
            self.results.clear()
            self.lbl_status.setText("ready")
            self._update_aql_preview()
            # Drop the cached session too - if the user explicitly
            # clears the form they probably don't want the old
            # results to reappear next time they open the dialog.
            try:
                import os
                p = _last_results_path()
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass

        def _on_latest(self):
            # AQL doesn't have a 'sort by recency' clause - just clear
            # everything and run an empty search so the user sees
            # something. The server's natural ordering tends to be
            # 'most recently updated first' in practice.
            self._on_clear()
            self._on_search()

        def _on_top_preset(self, category: str, limit: int,
                             label: str):
            """Fetch a server-precomputed Top list for a category.

            Uses the /charts/{category} endpoint which returns
            the server's own rating-sorted top list (typically
            Top 200). No AQL building, no client-side sort -
            the server has done both. Empty category falls back
            to /charts (overall Top 200).

            If /charts returns an error (older server build
            without the endpoint), we fall back to an AQL-based
            rating-sorted query with client-side trim, so the
            feature still works on every server version.
            """
            # Reset the UI so the result list isn't mixed up
            # with whatever was there from a previous manual
            # search.
            self._on_clear()
            # Reflect the chosen category in the combo box so
            # the user sees what's being filtered.
            if hasattr(self, "cmb_category"):
                idx = self.cmb_category.findData(category)
                if idx >= 0:
                    self.cmb_category.setCurrentIndex(idx)
            results = None
            err = None
            # Primary path: /charts/{category} - server-side
            # rating list. The dev team confirmed this is the
            # canonical way to get a Top N.
            try:
                results = self.client.get_charts(category)
            except Exception as e:
                err = e
            # Fallback: AQL with rating sort + limit. Used if
            # /charts isn't deployed on this Assembly64 build.
            if results is None:
                aql_parts = []
                if category:
                    aql_parts.append(f'(category:{category})')
                aql_parts.append('(order:rating:desc)')
                aql_parts.append(f'(limit:{int(limit)})')
                aql = ' & '.join(aql_parts)
                try:
                    results = self.client.search_aql_page(
                        aql, start=0, count=int(limit),
                        sort="rating", order="desc")
                except Exception as e2:
                    QMessageBox.warning(
                        self, "Top preset",
                        f"Top query failed.\n\n"
                        f"/charts error: {err}\n"
                        f"AQL fallback error: {e2}\n"
                        f"AQL was: {aql}")
                    return
                # AQL path doesn't sort reliably, so client-side
                # safety net here. /charts path already in order.
                results.sort(
                    key=lambda e: (getattr(e, 'rating', 0) or 0),
                    reverse=True)
            # Always cap at the requested limit. The /charts
            # endpoint usually caps at 200 itself, but the
            # menu has options for 50, 100 too - we honour
            # those by trimming.
            results = results[:int(limit)]
            self._last_search_params = {
                "name": "", "group": "", "handle": "",
                "year": "", "file_type": "",
                "category": category,
            }
            self._on_paginated_done(True, results, "")
            # Clear sort indicator so insertion order (which is
            # rating-desc) survives. Without this Qt will reapply
            # whatever column the user last clicked-to-sort.
            try:
                self.results.sortByColumn(
                    -1, Qt.SortOrder.AscendingOrder)
            except Exception:
                pass
            self.lbl_status.setText(
                f"{label}: {len(results)} results "
                f"(server top chart)")

        def _on_year_sweep(self):
            """Run the same filter once per year from 1982 to the
            current calendar year, plus a year=0 pass for entries
            with unknown release year. Merges all results and
            de-duplicates by id.

            This is the workaround for the server's per-query
            result cap. Triggered manually because it takes ~30-60
            seconds and a normal search is usually enough."""
            if (self._search_worker is not None
                    and self._search_worker.isRunning()):
                return
            if (getattr(self, "_year_sweep_worker", None) is not None
                    and self._year_sweep_worker.isRunning()):
                return
            self._update_aql_preview()
            params = self._gather_params()
            # Strip the year - the worker iterates years itself.
            params_no_year = {k: v for k, v in params.items()
                              if k != "year"}
            # Sanity check: if no other filter is set, doing a sweep
            # would download the entire database (one year at a
            # time). Warn the user.
            non_year_values = [v for k, v in params_no_year.items()
                               if v and k not in ("year",)]
            if not non_year_values:
                reply = QMessageBox.question(
                    self, "Get all years",
                    "No filters set besides year. A full sweep\n"
                    "would query the entire Assembly64 database\n"
                    "(~46 years of requests).\n\n"
                    "This will take a long time and is rate-limit\n"
                    "rude. Continue anyway?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self._last_search_params = dict(params)
            self.btn_search.setEnabled(False)
            if hasattr(self, "btn_all_years"):
                self.btn_all_years.setEnabled(False)
            # Current calendar year as upper bound; 1982 (C64 launch)
            # as lower bound covers the entire commodore era.
            import datetime
            now_year = datetime.datetime.now().year
            # Clear so the streaming chunks have a blank slate.
            self.results.clear()
            self.lbl_status.setText(
                f"sweeping newest-first... {now_year} \u2192 1982")
            self._year_sweep_worker = _YearSweepWorker(
                self.client, params_no_year,
                1982, now_year, self,
                reverse=True, pace_seconds=0.0)
            self._year_sweep_worker.chunk.connect(
                self._on_year_sweep_chunk)
            self._year_sweep_worker.progress.connect(
                self._on_year_sweep_progress)
            self._year_sweep_worker.done.connect(
                self._on_year_sweep_done)
            self._year_sweep_worker.start()

        def _on_year_sweep_progress(self, current_year,
                                       total_done, unique_so_far):
            # Legacy 3-tuple progress signal - kept for code paths
            # that don't use the chunk-based streaming. The
            # streaming handler below updates the same label more
            # informatively, but we leave this in for the manual
            # 'All years sweep' button which still wires up.
            self.lbl_status.setText(
                f"sweeping... year {current_year}, "
                f"{total_done} queries done, "
                f"{unique_so_far} unique hits")

        def _on_year_sweep_chunk(self, state):
            """Streaming update from the year sweep worker. Add
            this year's new entries to the results tree without
            tearing down what's already there - the user sees
            matches accumulate in real time instead of staring
            at an empty list for 30 seconds.

            We disable sorting during the batch insert (sorting
            on every addTopLevelItem is O(n log n) per add, which
            adds up over thousands of rows) and re-enable at the
            end of each chunk so the user can sort the partial
            results while the sweep continues.
            """
            new_entries = state.get('new_entries') or []
            year = state.get('year', 0)
            total = state.get('total_unique', 0)
            years_done = state.get('years_done', 0)
            years_total = state.get('years_total', 1)
            # Status line: shows which year we're on and the
            # running total. Year 0 displays as "(no year)" so
            # the user understands it's the unknown-year bucket
            # rather than year 0 AD.
            year_label = "(no year)" if year == 0 else str(year)
            self.lbl_status.setText(
                f"searching... year {year_label}, "
                f"{years_done}/{years_total} queries done, "
                f"{total} results so far")
            if not new_entries:
                return
            # Append the new rows. We don't re-render the whole
            # tree; we just add to the bottom. Sort order: the
            # user can click any column to sort at any time and
            # Qt re-sorts only the items that exist.
            self.results.setSortingEnabled(False)
            for e in new_entries:
                self.results.addTopLevelItem(
                    self._make_result_item(e))
            self.results.setSortingEnabled(True)

        def _make_result_item(self, e):
            """Build a single _SortableItem for the results tree.
            Extracted from _on_year_sweep_done and the normal
            search-done path so the streaming chunk handler can
            use the same shape without duplicating the year /
            category formatting logic."""
            year_str = str(e.year) if e.year > 0 else ""
            cat_str = self._category_label(
                e.category, e.site_category,
                e.category_name)
            item = _SortableItem([
                e.name, e.group, year_str, cat_str,
            ])
            item.setData(2, Qt.ItemDataRole.UserRole + 1, e.year)
            item.setData(0, Qt.ItemDataRole.UserRole, e)
            return item

        def _on_year_sweep_done(self, ok, entries, error):
            self.btn_search.setEnabled(True)
            if hasattr(self, "btn_all_years"):
                self.btn_all_years.setEnabled(True)
            if not ok and not entries:
                self.lbl_status.setText(f"sweep error: {error}")
                QMessageBox.warning(self, "Year sweep",
                    f"Sweep failed:\n{error}")
                return
            # If chunk handlers already populated the tree, we
            # just update the status line and persist - no need
            # to rebuild what's already visible.
            already_displayed = self.results.topLevelItemCount()
            n = len(entries)
            years_present = sorted(
                {e.year for e in entries if e.year > 0})
            if years_present:
                if years_present[0] == years_present[-1]:
                    year_part = f", year {years_present[0]}"
                else:
                    year_part = (f", years {years_present[0]}-"
                                  f"{years_present[-1]}")
            else:
                year_part = ""
            suffix = (" (cancelled, partial)"
                      if error == "(cancelled)"
                      else " (all years)")
            self.lbl_status.setText(
                f"{n} results{year_part}{suffix}")
            # Only rebuild if the chunk-streaming path didn't run
            # (e.g. some old caller wired only the progress signal
            # without chunk). In normal operation the tree is
            # already populated from the chunk handler.
            if already_displayed != n:
                self.results.clear()
                self.results.setSortingEnabled(False)
                for e in entries:
                    self.results.addTopLevelItem(
                        self._make_result_item(e))
                self.results.setSortingEnabled(True)
            if entries:
                save_last_results(
                    self._last_search_params, entries)
            # Auto-learn unknown CSDB release types in background.
            # Same hook as in _on_paginated_done / _on_search_done
            # so the year-sweep path also benefits.
            self._kick_off_type_learning(entries)

        # -------- Save / Load results to file ---------------------

        def _default_results_save_dir(self):
            """Default directory for named result snapshots. Tries
            <quopus_project>/asm64_results/, creates it if missing,
            falls back to ~ if the project root isn't writable."""
            import os
            try:
                from .config import SCRIPT_DIR
                if os.access(str(SCRIPT_DIR), os.W_OK):
                    target = SCRIPT_DIR / "asm64_results"
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                    return str(target)
            except Exception:
                pass
            return os.path.expanduser("~")

        def _aql_to_filename(self, params):
            """Build a useful default filename from the AQL params.
            Returns a short slug like 'group_quantum' or
            'name_jumpman_type_prg'. Empty params -> 'all'."""
            import re
            parts = []
            for k in ("name", "group", "handle", "year",
                      "file_type", "category"):
                v = (params or {}).get(k, "")
                if v:
                    # Slugify: lowercase, alphanumeric + underscore
                    slug = re.sub(r'[^a-z0-9]+', '_',
                                   str(v).lower()).strip('_')
                    if slug:
                        parts.append(f"{k}_{slug}")
            return "_".join(parts) if parts else "all"

        # ----- Named result snapshots ------------------------

        def _refresh_saved_results_combo(self):
            """Rebuild the dropdown of saved-snapshot names. Block
            signals during the rebuild so the currentIndexChanged
            handler doesn't trigger a load. Selection stays on
            '(none)' after refresh to avoid surprising the user
            when they just saved a new snapshot."""
            self.cmb_saved_results.blockSignals(True)
            self.cmb_saved_results.clear()
            self.cmb_saved_results.addItem("(none)", None)
            try:
                index = load_saved_results_index()
            except Exception:
                index = {}
            # Sort by saved_at descending so newest at top
            items = sorted(
                index.items(),
                key=lambda kv: kv[1].get("saved_at", ""),
                reverse=True)
            for name, snap in items:
                n_entries = len(snap.get("entries") or [])
                # Show the entry count after the name so the user
                # can tell which snapshot is which at a glance.
                self.cmb_saved_results.addItem(
                    f"{name}  ({n_entries})", name)
            self.cmb_saved_results.blockSignals(False)
            self.btn_delete_named.setEnabled(False)

        def _on_saved_results_picked(self, index):
            """Dropdown selection changed. Index 0 = '(none)' which
            means clear selection / disable delete. Any other index
            loads the corresponding snapshot."""
            name = self.cmb_saved_results.currentData()
            if not name:
                self.btn_delete_named.setEnabled(False)
                return
            self.btn_delete_named.setEnabled(True)
            try:
                params, entries = load_named_results(name)
            except KeyError:
                QMessageBox.warning(
                    self, "Saved Results",
                    f"Snapshot '{name}' is gone from the index.\n"
                    f"Maybe deleted from another window? "
                    f"Refreshing.")
                self._refresh_saved_results_combo()
                return
            except Exception as e:
                QMessageBox.warning(
                    self, "Saved Results",
                    f"Couldn't load snapshot:\n\n{e}")
                return
            # Repopulate the results tree
            self.results.clear()
            self.results.setSortingEnabled(False)
            for e in entries:
                self.results.addTopLevelItem(
                    self._make_result_item(e))
            self.results.setSortingEnabled(True)
            self._last_search_params = dict(params)
            # Also rehydrate the filter fields so the user sees
            # which search produced these results. Skip if any
            # field is missing - we just leave the UI as-is.
            try:
                if hasattr(self, "ed_name"):
                    self.ed_name.setText(params.get("name", ""))
                if hasattr(self, "ed_group"):
                    self.ed_group.setText(params.get("group", ""))
                if hasattr(self, "ed_handle"):
                    self.ed_handle.setText(params.get("handle", ""))
                if hasattr(self, "ed_year"):
                    self.ed_year.setText(params.get("year", ""))
            except Exception:
                pass
            self.lbl_status.setText(
                f"Loaded snapshot '{name}': "
                f"{len(entries)} results")

        def _on_save_named_results(self):
            """Prompt for a name and save the current result list
            under it. Trial-gated like the JSON export."""
            try:
                from quopus_lib import license
                if not license.has_feature(
                        license.FEATURE_ASM64_SAVE):
                    QMessageBox.information(
                        self, "Save snapshot - Pro feature",
                        "Saving result snapshots is a Pro\n"
                        "feature.\n\n"
                        "Trial users can browse and download\n"
                        "individual entries. Pro users can\n"
                        "additionally save named snapshots for\n"
                        "later reload.\n\n"
                        "See BUYING.md to register.")
                    return
            except Exception:
                pass
            n = self.results.topLevelItemCount()
            if n == 0:
                QMessageBox.information(
                    self, "Save snapshot",
                    "No results to save.")
                return
            # Default name from the active filter. Keep it short
            # but descriptive so the dropdown stays readable.
            default_name = self._aql_to_filename(
                self._last_search_params)
            from PyQt6.QtWidgets import QInputDialog
            existing = load_saved_results_index()
            while True:
                name, ok = QInputDialog.getText(
                    self, "Save result snapshot",
                    f"Save the current {n} results under what "
                    f"name?\n\nLeave blank to cancel.",
                    text=default_name)
                if not ok or not name.strip():
                    return
                name = name.strip()
                if name in existing:
                    overwrite = QMessageBox.question(
                        self, "Overwrite?",
                        f"A snapshot named '{name}' already "
                        f"exists (saved "
                        f"{existing[name].get('saved_at', '?')[:19]}).\n\n"
                        f"Overwrite it?",
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel)
                    if overwrite == QMessageBox.StandardButton.Cancel:
                        return
                    if overwrite == QMessageBox.StandardButton.No:
                        # Loop back to the name prompt with a
                        # different suggestion
                        default_name = name + "_2"
                        continue
                break
            # Build entry list from the current table (in display
            # order, after any user sorting)
            entries = []
            for i in range(n):
                item = self.results.topLevelItem(i)
                e = item.data(0, Qt.ItemDataRole.UserRole)
                if e is not None:
                    entries.append(e)
            try:
                save_named_results(
                    name, self._last_search_params, entries)
            except OSError as e:
                QMessageBox.warning(
                    self, "Save snapshot",
                    f"Couldn't write snapshot file:\n\n{e}")
                return
            self._refresh_saved_results_combo()
            # Select the new entry in the dropdown so the user
            # can see it landed there
            for i in range(self.cmb_saved_results.count()):
                if self.cmb_saved_results.itemData(i) == name:
                    self.cmb_saved_results.blockSignals(True)
                    self.cmb_saved_results.setCurrentIndex(i)
                    self.cmb_saved_results.blockSignals(False)
                    self.btn_delete_named.setEnabled(True)
                    break
            self.lbl_status.setText(
                f"Saved snapshot '{name}' with {n} results")

        def _on_delete_named_results(self):
            """Drop the currently-selected snapshot from the
            index. Confirms first because deletion is permanent
            (no undo)."""
            name = self.cmb_saved_results.currentData()
            if not name:
                return
            reply = QMessageBox.question(
                self, "Delete snapshot",
                f"Permanently delete the saved snapshot\n"
                f"'{name}'?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                delete_named_results(name)
            except OSError as e:
                QMessageBox.warning(
                    self, "Delete snapshot",
                    f"Couldn't update snapshot file:\n\n{e}")
                return
            self._refresh_saved_results_combo()
            self.lbl_status.setText(
                f"Deleted snapshot '{name}'")

        def _on_save_results_to_file(self):
            """Save the current result table to a named JSON file.

            Walks the table rather than the last-search cache so
            ad-hoc inspection (sorting, filtering by clicking
            headers, manual additions later) still produces the
            displayed list. The filename defaults to one derived
            from the active filters.

            This is a PRO feature - trial users see a "buy pro"
            dialog. They can still browse and download individual
            entries, just not bulk-save the result list."""
            # Trial gate
            try:
                from quopus_lib import license
                if not license.has_feature(
                        license.FEATURE_ASM64_SAVE):
                    QMessageBox.information(
                        self, "Save Results - Pro Feature",
                        "Saving search results to disk is a Pro\n"
                        "feature.\n\n"
                        "Trial users can browse and download\n"
                        "individual entries from Assembly64. Pro\n"
                        "users can additionally save the full\n"
                        "result list as JSON for offline reference\n"
                        "or bulk processing.\n\n"
                        "See BUYING.md to register.")
                    return
            except Exception:
                pass
            import json, datetime, os
            n = self.results.topLevelItemCount()
            if n == 0:
                QMessageBox.information(
                    self, "Save Results",
                    "No results to save.")
                return
            entries_data = []
            for i in range(n):
                item = self.results.topLevelItem(i)
                e = item.data(0, Qt.ItemDataRole.UserRole)
                if e is None:
                    continue
                # Prefer the original server JSON (it has every
                # field including obscure ones we don't render);
                # fall back to asdict() if .raw is missing.
                if getattr(e, "raw", None):
                    entries_data.append(e.raw)
                else:
                    entries_data.append(asdict(e))
            # Build a useful default filename
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = self._aql_to_filename(self._last_search_params)
            default_name = f"asm64_{slug}_{ts}.json"
            start_dir = self._default_results_save_dir()
            suggested = os.path.join(start_dir, default_name)
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Assembly64 results as...",
                suggested,
                "JSON files (*.json);;All files (*)")
            if not path:
                self.lbl_status.setText("save cancelled")
                return
            payload = {
                "_meta": {
                    "timestamp": datetime.datetime.now().isoformat(),
                    "tool": "Quopus Assembly64 Browser",
                    "format_version": 1,
                },
                "params": dict(self._last_search_params or {}),
                "entries": entries_data,
            }
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
            except OSError as e:
                QMessageBox.warning(self, "Save Results",
                    f"Could not write:\n{path}\n{e}")
                self.lbl_status.setText(f"save failed: {e}")
                return
            fname = os.path.basename(path)
            self.lbl_status.setText(
                f"saved {len(entries_data)} entries to {fname}")
            QMessageBox.information(self, "Save Results",
                f"Saved {len(entries_data)} entries to:\n{path}")

        def _on_load_results_from_file(self):
            """Replace the current results with a JSON file's
            contents. Accepts both the wrapped {_meta, params,
            entries} format we write AND a raw list of entry
            dicts so a hand-edited file works too."""
            import json, os
            start_dir = self._default_results_save_dir()
            path, _ = QFileDialog.getOpenFileName(
                self, "Load Assembly64 results from...",
                start_dir,
                "JSON files (*.json);;All files (*)")
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Load Results",
                    f"Couldn't read or parse:\n{path}\n{e}")
                return
            # Accept the wrapped format OR a raw list
            if (isinstance(payload, dict)
                    and isinstance(payload.get("entries"), list)):
                raw_entries = payload["entries"]
                params = payload.get("params") or {}
                meta = payload.get("_meta", {})
            elif isinstance(payload, list):
                raw_entries = payload
                params = {}
                meta = {}
            else:
                QMessageBox.warning(self, "Load Results",
                    "JSON doesn't look like an Assembly64 result\n"
                    "dump (need a list of entries or a wrapped\n"
                    "{params, entries} object).")
                return
            if not raw_entries:
                QMessageBox.information(self, "Load Results",
                    "File contains no entries.")
                return
            # Repopulate filter form to reflect the loaded query,
            # so the user sees what produced these results.
            self.ed_name.setText(str(params.get("name", "")))
            self.ed_group.setText(str(params.get("group", "")))
            self.ed_handle.setText(str(params.get("handle", "")))
            self.ed_year.setText(str(params.get("year", "")))
            # Category is now a combobox - find the entry whose
            # itemData matches the saved int id.
            cat_val = params.get("category", 0)
            try:
                cat_id = int(cat_val) if cat_val else 0
            except (TypeError, ValueError):
                cat_id = 0
            for _i in range(self.cmb_category.count()):
                if self.cmb_category.itemData(_i) == cat_id:
                    self.cmb_category.setCurrentIndex(_i)
                    break
            else:
                self.cmb_category.setCurrentIndex(0)
            ft = params.get("file_type", params.get("type_", ""))
            self._set_combo_by_data(self.cmb_type, ft)
            self._update_aql_preview()
            self._last_search_params = dict(params)
            # Render the entries into the table using the same
            # path as _on_search_done.
            entries = [ASM64Entry.from_json(j)
                       for j in raw_entries
                       if isinstance(j, dict)]
            self.results.clear()
            self.results.setSortingEnabled(False)
            for e in entries:
                year_str = str(e.year) if e.year > 0 else ""
                cat_str = self._category_label(
                    e.category, e.site_category,
                    e.category_name)
                item = _SortableItem([
                    e.name, e.group, year_str, cat_str,
                ])
                item.setData(2, Qt.ItemDataRole.UserRole + 1,
                              e.year)
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                self.results.addTopLevelItem(item)
            self.results.setSortingEnabled(True)
            years_present = sorted(
                {e.year for e in entries if e.year > 0})
            if years_present:
                year_part = (f", years {years_present[0]}-"
                              f"{years_present[-1]}"
                              if years_present[0] != years_present[-1]
                              else f", year {years_present[0]}")
            else:
                year_part = ""
            ts = meta.get("timestamp", "")[:19].replace("T", " ")
            ts_part = f" (loaded {ts})" if ts else " (loaded)"
            self.lbl_status.setText(
                f"{len(entries)} results{year_part}{ts_part}")
            # Also overwrite the auto-cache so reopening the dialog
            # picks up the loaded set as the "last shown" view.
            save_last_results(self._last_search_params, entries)

        # -------- Result selection / files ------------------------

        def _on_result_selected(self):
            sel = self.results.selectedItems()
            if not sel:
                self._current_entry = None
                self.lbl_detail.setText("(select a result for details)")
                self.btn_csdb.setEnabled(False)
                self.btn_fav.setEnabled(False)
                self.files.clear()
                self._update_file_buttons()
                # Clear preview too
                self._clear_preview()
                return
            entry = sel[0].data(0, Qt.ItemDataRole.UserRole)
            self._current_entry = entry
            self._show_entry_detail(entry)
            self._fetch_files_for(entry)
            self._fetch_preview_for(entry)

        def _clear_preview(self):
            """Reset the preview pane to its placeholder state.
            Also cancels any in-flight preview fetch so its
            (slow) result doesn't override a newer selection."""
            if (self._preview_worker is not None
                    and self._preview_worker.isRunning()):
                self._preview_worker.cancel()
            self.lbl_preview.setPixmap(QPixmap())
            self.lbl_preview.setText(
                "(select a release to preview)")
            self.lbl_preview_status.setText("")

        def _fetch_preview_for(self, entry):
            """Kick off a background fetch of the screenshot for
            this release. Cancels any previous in-flight worker
            first so rapid up/down arrow navigation through the
            result list doesn't pile up requests."""
            # Cancel previous
            if (self._preview_worker is not None
                    and self._preview_worker.isRunning()):
                self._preview_worker.cancel()
            self.lbl_preview.setPixmap(QPixmap())
            self.lbl_preview.setText("Loading preview...")
            self.lbl_preview_status.setText(
                f"entry id={entry.id} cat={entry.category}")
            if not entry.id:
                self.lbl_preview.setText("(no entry id)")
                return
            self._preview_worker = _PreviewWorker(
                self.client, entry.id, entry.category,
                self._preview_cache, self)
            self._preview_worker.done.connect(
                self._on_preview_done)
            self._preview_worker.start()

        def _on_preview_done(self, ok, data, label, error):
            """Worker finished. Display image. For animated GIFs
            (and animated PNGs/WebPs Qt supports) we route through
            QMovie so the animation actually plays; static images
            get rendered as QPixmap as before.

            QMovie needs the image data buffered in a QBuffer for
            its whole playback lifetime - we keep references on
            self so the GC doesn't sweep them mid-animation."""
            if not ok:
                # Stop any previous animation
                self._stop_preview_animation()
                self.lbl_preview.setPixmap(QPixmap())
                self.lbl_preview.setText(
                    "(no preview available)\n\n"
                    "Try 'Open CSDB page' for screenshots.")
                tip = label or "no image"
                if error:
                    tip += f"  -  {error[:80]}"
                self.lbl_preview_status.setText(tip)
                # Nothing to zoom into
                self._preview_data = None
                self._preview_label = ""
                return

            # Cache the bytes so click-to-zoom can re-render at
            # full resolution (or play the animation in a fresh
            # QMovie).
            self._preview_data = data
            self._preview_label = label

            # Stop any previous animation so we don't leak the
            # QMovie / QBuffer pair if the user clicks through
            # several animated releases in a row.
            self._stop_preview_animation()

            # Sniff for animated formats. GIF89a animation flag is
            # tricky to detect without parsing the file structure,
            # but most GIFs that CSDB hosts ARE animated (capture
            # of a running demo), and Qt's QMovie handles static
            # GIFs fine too. So we treat all GIFs as movies.
            # Animated PNG (APNG) also goes through QMovie - Qt
            # handles it via the "apng" or "png" image plugin.
            is_gif = data[:6] in (b"GIF87a", b"GIF89a")
            # APNG sniff: PNG signature followed by an acTL chunk
            # somewhere in the first few KB
            is_apng = (data[:8] == b"\x89PNG\r\n\x1a\n"
                       and b"acTL" in data[:4096])

            if is_gif or is_apng:
                try:
                    from PyQt6.QtCore import QBuffer, QByteArray
                    from PyQt6.QtGui import QMovie
                    # QMovie needs a QIODevice; QBuffer wraps the
                    # bytes. Both have to outlive the playback so
                    # we stash them on self.
                    self._preview_buffer_bytes = QByteArray(data)
                    self._preview_buffer = QBuffer(
                        self._preview_buffer_bytes)
                    self._preview_buffer.open(
                        QBuffer.OpenModeFlag.ReadOnly)
                    self._preview_movie = QMovie(self)
                    fmt = b"gif" if is_gif else b"png"
                    self._preview_movie.setFormat(fmt)
                    self._preview_movie.setDevice(
                        self._preview_buffer)
                    # Probe size with one frame so we know how to
                    # scale; QMovie doesn't expose the frame
                    # rectangle until jumpToFrame succeeds.
                    self._preview_movie.jumpToFrame(0)
                    raw_size = self._preview_movie.currentImage().size()
                    target = self.lbl_preview.size()
                    if target.width() < 200:
                        target = QSize(
                            max(420, target.width()),
                            max(320, target.height()))
                    if target.width() < 100 or target.height() < 100:
                        target = QSize(420, 320)
                    if (raw_size.width() > 0
                            and raw_size.height() > 0):
                        # Scale preserving aspect ratio
                        scaled_w = target.width()
                        scaled_h = int(raw_size.height()
                                       * target.width()
                                       / raw_size.width())
                        if scaled_h > target.height():
                            scaled_h = target.height()
                            scaled_w = int(raw_size.width()
                                           * target.height()
                                           / raw_size.height())
                        self._preview_movie.setScaledSize(
                            QSize(scaled_w, scaled_h))
                    self.lbl_preview.setMovie(
                        self._preview_movie)
                    self.lbl_preview.setText("")
                    self._preview_movie.start()
                    frames = self._preview_movie.frameCount()
                    self.lbl_preview_status.setText(
                        f"{label}  -  "
                        f"{raw_size.width()}x"
                        f"{raw_size.height()}, "
                        f"{len(data):,} bytes, "
                        f"{frames} frame(s)"
                        + (" animated" if frames > 1 else ""))
                    return
                except Exception as e:
                    # QMovie setup failed for some reason - fall
                    # through to static QPixmap rendering which
                    # at least shows the first frame.
                    print(f"[asm64 preview] QMovie failed: {e}")

            pix = QPixmap()
            pix.loadFromData(data)
            if pix.isNull():
                self.lbl_preview.setText(
                    "(image data couldn't be decoded)")
                self.lbl_preview_status.setText(
                    f"{label}; {len(data):,} bytes received "
                    f"but not a recognized image format")
                return
            target = self.lbl_preview.size()
            if target.width() < 200:
                target = QSize(
                    max(420, target.width()),
                    max(320, target.height()))
            if target.width() < 100 or target.height() < 100:
                target = QSize(420, 320)
            scaled = pix.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.lbl_preview.setPixmap(scaled)
            self.lbl_preview.setText("")
            self.lbl_preview_status.setText(
                f"{label}  -  "
                f"{pix.width()}x{pix.height()}, "
                f"{len(data):,} bytes")

        def _stop_preview_animation(self):
            """Tear down any in-flight QMovie/QBuffer cleanly so
            we can either show a new animation or a static frame.
            Safe to call when nothing is playing."""
            m = getattr(self, "_preview_movie", None)
            if m is not None:
                try:
                    m.stop()
                    m.setDevice(None)
                except Exception:
                    pass
                self._preview_movie = None
            b = getattr(self, "_preview_buffer", None)
            if b is not None:
                try:
                    b.close()
                except Exception:
                    pass
                self._preview_buffer = None
            self._preview_buffer_bytes = None
            # QLabel keeps a reference to a QMovie when setMovie
            # was used - clear it so the next setPixmap takes
            # over cleanly.
            try:
                self.lbl_preview.setMovie(None)
            except Exception:
                pass

        def _show_preview_zoom(self):
            """Pop up a borderless dialog showing the current
            preview at full resolution. Click anywhere on the
            popup to close. Animated GIF/APNG keep playing in the
            zoom view.

            If no preview is loaded, do nothing - clicking on the
            'select a release to preview' placeholder shouldn't
            open an empty popup.
            """
            data = getattr(self, "_preview_data", None)
            if not data:
                return

            from PyQt6.QtWidgets import (
                QDialog, QLabel as _QLabel, QVBoxLayout as _VBL)
            from PyQt6.QtCore import (
                QBuffer as _QBuffer,
                QByteArray as _QByteArray)
            from PyQt6.QtGui import (
                QMovie as _QMovie,
                QPixmap as _QPixmap)

            # Detect animated formats - same sniff as _on_preview_done
            is_gif = data[:6] in (b"GIF87a", b"GIF89a")
            is_apng = (data[:8] == b"\x89PNG\r\n\x1a\n"
                       and b"acTL" in data[:4096])

            dlg = QDialog(self)
            dlg.setWindowTitle(
                f"Preview: {self._preview_label}")
            dlg.setModal(False)  # don't block the main browser
            lay = _VBL(dlg)
            lay.setContentsMargins(0, 0, 0, 0)

            zoom_lbl = _QLabel()
            zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            zoom_lbl.setStyleSheet("background: #000;")
            zoom_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(zoom_lbl)

            # Keep references on the dialog so they outlive the
            # constructor scope. QMovie playback would die without
            # them.
            dlg._zoom_movie = None
            dlg._zoom_buffer = None
            dlg._zoom_bytes = None

            if is_gif or is_apng:
                try:
                    dlg._zoom_bytes = _QByteArray(data)
                    dlg._zoom_buffer = _QBuffer(dlg._zoom_bytes)
                    dlg._zoom_buffer.open(
                        _QBuffer.OpenModeFlag.ReadOnly)
                    dlg._zoom_movie = _QMovie(dlg)
                    fmt = b"gif" if is_gif else b"png"
                    dlg._zoom_movie.setFormat(fmt)
                    dlg._zoom_movie.setDevice(dlg._zoom_buffer)
                    # Probe original size, then size the dialog
                    # to match - up to a reasonable cap so a 4K
                    # screenshot doesn't fill the desktop.
                    dlg._zoom_movie.jumpToFrame(0)
                    raw = dlg._zoom_movie.currentImage().size()
                    target_w = min(raw.width() * 2, 1280)
                    target_h = min(raw.height() * 2, 960)
                    if (raw.width() > 0 and raw.height() > 0
                            and target_w > 0):
                        scaled_h = int(raw.height() * target_w
                                       / raw.width())
                        if scaled_h > target_h:
                            scaled_h = target_h
                            target_w = int(raw.width()
                                           * target_h
                                           / raw.height())
                        dlg._zoom_movie.setScaledSize(
                            QSize(target_w, scaled_h))
                        zoom_lbl.setFixedSize(
                            target_w, scaled_h)
                    zoom_lbl.setMovie(dlg._zoom_movie)
                    dlg._zoom_movie.start()
                except Exception:
                    dlg._zoom_movie = None

            if dlg._zoom_movie is None:
                # Static image path
                pix = _QPixmap()
                pix.loadFromData(data)
                if not pix.isNull():
                    # Scale up to 2x original (typical C64 screens
                    # are 320x200; doubling makes the pixels
                    # actually visible) but cap at 1280x960 so it
                    # fits on a laptop screen.
                    target_w = min(pix.width() * 2, 1280)
                    target_h = min(pix.height() * 2, 960)
                    scaled = pix.scaled(
                        QSize(target_w, target_h),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    zoom_lbl.setPixmap(scaled)
                    zoom_lbl.setFixedSize(scaled.size())

            # Click-to-close anywhere on the popup. We reuse the
            # mousePressEvent trick from the small preview.
            def _close_on_click(ev):
                if ev.button() == Qt.MouseButton.LeftButton:
                    dlg.close()
            zoom_lbl.mousePressEvent = _close_on_click

            # Stop the movie cleanly on dialog close - otherwise
            # the buffer might be GC'd while QMovie still reads
            # from it.
            def _on_close(_ev):
                if dlg._zoom_movie is not None:
                    try:
                        dlg._zoom_movie.stop()
                        dlg._zoom_movie.setDevice(None)
                    except Exception:
                        pass
                if dlg._zoom_buffer is not None:
                    try:
                        dlg._zoom_buffer.close()
                    except Exception:
                        pass
                _ev.accept()
            dlg.closeEvent = _on_close

            dlg.show()

        def _show_entry_detail(self, e):
            parts = [f"<b>{e.name}</b>"]
            if e.group:
                parts.append(f"by <b>{e.group}</b>")
            if e.handle:
                parts.append(f"({e.handle})")
            if e.year > 0:
                parts.append(f"&middot; {e.year}")
            cat = self._category_label(e.category, e.site_category, e.category_name)
            if cat:
                parts.append(f"&middot; cat: {cat}")
            if e.rating > 0:
                parts.append(
                    f"&middot; rating: <b>{e.rating}</b>")
            if e.site_rating > 0:
                parts.append(
                    f"&middot; site rating: {e.site_rating}")
            if e.released:
                parts.append(f"&middot; released: {e.released}")
            if e.updated:
                parts.append(f"&middot; updated: {e.updated}")
            self.lbl_detail.setText(" ".join(parts))
            # Assembly64 IDs are CSDB IDs, so always enable CSDB link
            # when we have an id.
            self.btn_csdb.setEnabled(bool(e.id))
            self.btn_fav.setEnabled(True)
            self._refresh_fav_button(e)
            # Kick off lazy CSDB detail fetch for full credit list.
            # The /search/aql endpoint only gives us ONE handle
            # per release; the full credits (Code by X, Music by
            # Y, Graphics by Z, ...) come from csdb.dk directly.
            self._fetch_csdb_details_for(e)

        def _fetch_csdb_details_for(self, entry):
            """Spin up a background CSDB-detail fetch for the
            selected entry. Updates the detail label with the
            full credits list when the response arrives.
            """
            if not entry or not entry.id:
                return
            try:
                rid = int(entry.id)
            except (TypeError, ValueError):
                return
            # Cancel any in-flight previous detail worker - we
            # don't want stale credits from the previous
            # selection to override the current one.
            if (getattr(self, "_csdb_detail_worker", None)
                    is not None
                    and self._csdb_detail_worker.isRunning()):
                # Disconnect so the stale signal doesn't fire
                try:
                    self._csdb_detail_worker.done.disconnect()
                except (TypeError, RuntimeError):
                    pass
            self._csdb_detail_worker = _CSDBDetailWorker(
                self.client, rid, self)
            self._csdb_detail_worker.done.connect(
                self._on_csdb_details_done)
            self._csdb_detail_worker.start()

        def _on_csdb_details_done(self, release_id, details):
            """CSDB returned the full release details. Augment
            the detail panel with the credits list and groups,
            but ONLY if the user hasn't switched to a different
            release in the meantime.
            """
            if (self._current_entry is None
                    or not details):
                return
            try:
                cur_id = int(self._current_entry.id)
            except (TypeError, ValueError):
                return
            if cur_id != release_id:
                # Stale - user moved on
                return
            credits = details.get("credits") or []
            if not credits:
                return
            # Build a "<CreditType> by <Handles>" line per
            # credit type, joining duplicate types together.
            # E.g. "Code by HCL, Dane &middot; Music by Dane
            # &middot; Graphics by HCL, Jailbird"
            by_type = {}
            for c in credits:
                ct = c.get("credit_type") or "Credit"
                h = c.get("handle") or ""
                if not h:
                    continue
                by_type.setdefault(ct, [])
                if h not in by_type[ct]:
                    by_type[ct].append(h)
            credit_lines = []
            for ct, handles in by_type.items():
                joined = ", ".join(handles)
                credit_lines.append(f"{ct}: {joined}")
            extra = "<br>" + " &middot; ".join(credit_lines)
            cur = self.lbl_detail.text()
            self.lbl_detail.setText(cur + extra)

        def _fetch_files_for(self, entry):
            self.files.clear()
            self._current_files = []
            self._update_file_buttons()
            if not entry.id:
                return
            if (self._files_worker is not None
                    and self._files_worker.isRunning()):
                self._files_worker.terminate()
            self.lbl_status.setText(f"fetching files for {entry.name}...")
            # The U64 firmware passes entry.category as the {cat}
            # path component in /search/entries/{id}/{cat}, even
            # when category=0 (e.g. CSDB-sourced entries). The
            # siteCategory is metadata for display only - using it
            # in the URL returns HTTP 500 from the server.
            cat = entry.category
            self._files_worker = _FilesWorker(
                self.client, entry.id, cat, self)
            self._files_worker.done.connect(self._on_files_done)
            self._files_worker.start()

        def _on_files_done(self, ok, files, error):
            if not ok:
                self.lbl_status.setText(f"file list error: {error}")
                return
            self._current_files = files
            self.lbl_status.setText(
                f"{len(files)} files in release")
            for f in files:
                size_str = self._format_size(f.size)
                item = QTreeWidgetItem([f.name, f.type, size_str])
                item.setData(0, Qt.ItemDataRole.UserRole, f)
                self.files.addTopLevelItem(item)
            self.files.itemSelectionChanged.connect(
                self._update_file_buttons)
            # Auto-select the first sensible file so the user can
            # hit Run / Run-on-U64 / Mount immediately without an
            # extra click. We prefer runnable formats (PRG/CRT/D64/
            # SID) over README/TXT/NFO etc. - if a release has both
            # a "demo.prg" and a "readme.txt" the user almost
            # always wants the prg. Fall back to row 0 only if
            # nothing else qualifies.
            self._auto_select_first_useful_file()

        def _auto_select_first_useful_file(self):
            """Select the first file in the right-hand list that
            looks like something the user actually wants to run or
            mount. Skips docs and meta files when better options
            exist. Quietly does nothing if the list is empty - the
            caller paths all handle a selection of None gracefully.
            """
            n = self.files.topLevelItemCount()
            if n == 0:
                return
            # Priority order: highest score wins. Ties broken by
            # original list order (which is server's order).
            runnable = {".prg", ".crt", ".d64", ".d71",
                        ".d81", ".g64", ".g71", ".sid",
                        ".tap", ".t64", ".p00", ".mod"}
            best_score = -1
            best_row = 0
            for i in range(n):
                item = self.files.topLevelItem(i)
                f = item.data(0, Qt.ItemDataRole.UserRole)
                if f is None:
                    continue
                ext = ""
                if "." in f.name:
                    ext = "." + f.name.rsplit(".", 1)[-1].lower()
                if ext in runnable:
                    score = 10
                    # Slight preference for PRG over disk images
                    # because PRGs run instantly via /run, no mount
                    # round-trip needed. D64 still scores 9 so it
                    # wins against documentation files.
                    if ext == ".prg":
                        score = 12
                    elif ext in (".d64", ".d71", ".d81"):
                        score = 11
                else:
                    score = 0
                if score > best_score:
                    best_score = score
                    best_row = i
            # If nothing scored above zero we still pick row 0 -
            # the user can switch manually but at least the
            # buttons get enabled rather than staying greyed out.
            self.files.setCurrentItem(
                self.files.topLevelItem(best_row))

        @staticmethod
        def _format_size(n):
            if not n:
                return ""
            if n < 1024:
                return f"{n} B"
            if n < 1024 * 1024:
                return f"{n // 1024} KB"
            return f"{n / (1024 * 1024):.1f} MB"

        def _selected_file(self):
            sel = self.files.selectedItems()
            if not sel:
                return None
            return sel[0].data(0, Qt.ItemDataRole.UserRole)

        def _update_file_buttons(self):
            f = self._selected_file()
            has = f is not None
            ext = (f.name.rsplit(".", 1)[-1].lower()
                   if has and "." in f.name else "")
            is_disk = ext in ("d64", "d71", "d81", "g64", "g71",
                              "g81")
            is_runnable = ext in ("prg", "crt", "sid", "p00",
                                   "mod", "sid")
            # Run handles both directly-runnable files (PRG/CRT/SID)
            # Run and Mount are usable when:
            #   - a runnable callback is wired (streamer mode)
            #   - OR we can set one up on the fly via the device
            #     picker (standalone mode - we just need at least
            #     one U64 in the config to make this work)
            from .u64_devices import get_devices
            try:
                _mw = self.parent()
                while (_mw is not None
                        and not hasattr(_mw, "config")):
                    _mw = (_mw.parent() if hasattr(_mw, "parent")
                            else None)
                _has_u64 = bool(get_devices(
                    getattr(_mw, "config", {}) or {}))
            except Exception:
                _has_u64 = False
            can_run = has and (
                (is_runnable and (
                    self.on_run is not None or _has_u64))
                or (is_disk and (
                    self.on_mount is not None or _has_u64)))
            self.btn_run.setEnabled(can_run)
            self.btn_mount.setEnabled(
                has and is_disk and (
                    self.on_mount is not None or _has_u64))
            self.btn_dl.setEnabled(has)
            # Run-in-emulator enabled for runnable or disk-image
            # selections. We download to a temp file first then
            # hand off to the emulator launcher, so any extension
            # the emulator accepts will work.
            self.btn_run_emu.setEnabled(
                has and (is_runnable or is_disk))

        # -------- File actions ------------------------------------

        def _on_run(self):
            f = self._selected_file()
            if not f:
                return
            # Decide whether this is a directly-runnable file or a
            # disk image. PRG/CRT/SID/MOD use on_run, D64/D71/D81 use
            # on_mount (which the host-side callback follows up with a
            # reset so the C64 actually boots the disk).
            ext = (f.name.rsplit(".", 1)[-1].lower()
                   if "." in f.name else "")
            is_disk = ext in ("d64", "d71", "d81", "g64", "g71",
                              "g81")
            if is_disk:
                if self.on_mount is None:
                    QMessageBox.information(self, "Run",
                        "Mount callback not wired - can't run\n"
                        "a disk image without it.")
                    return
                # Route through the mount path - the caller's mount
                # callback is expected to reset the C64 after mount.
                self._download_pending_action = "mount"
            else:
                if self.on_run is None:
                    # Standalone browser: no callback wired. Build
                    # one on the fly that posts to the user-chosen
                    # U64 via /v1/runners/run. This is how the
                    # Asm64 browser works when launched directly
                    # from the action picker (no streamer in
                    # the chain).
                    if not self._setup_standalone_run_callback():
                        return
                self._download_pending_action = "run"
            self._start_download(f)

        def _setup_standalone_run_callback(self) -> bool:
            """When the Asm64 browser is opened standalone (no
            U64Streamer in the parent chain), there's no on_run
            callback to call after download. We build one here:
            ask the user which U64 to use (skipping the dialog if
            only one is configured), then construct a posting
            function that uploads the downloaded bytes to that
            device.

            Returns True if a callback is now in place, False if
            the user cancelled or no device is configured.
            """
            # Locate the main window's config so we can read the
            # device list. Walk up the parent chain since the
            # browser may be a few widgets deep.
            mw = self.parent()
            while (mw is not None
                   and not hasattr(mw, "config")):
                mw = (mw.parent() if hasattr(mw, "parent")
                       else None)
            cfg = getattr(mw, "config", {}) if mw else {}
            from .u64_devices import pick_device
            device = pick_device(
                self, cfg,
                title="Run on U64",
                prompt="Which Ultimate-64 should run this file?")
            if device is None:
                return False
            # Build a per-call callback that uploads the file
            # bytes to the picked device. We don't change
            # self.on_run permanently - the user might pick a
            # different device next time.
            host = device.get("host", "") or ""
            http_port = int(device.get("http_port", 80))
            password = device.get("password", "") or ""

            def _post_to_u64(file_bytes: bytes,
                             filename: str = "") -> None:
                """One-shot post of file bytes to the picked U64
                via the firmware's HTTP runner endpoint."""
                import urllib.request
                ext = (filename.rsplit(".", 1)[-1].lower()
                       if "." in filename else "")
                # Firmware-supported endpoints. crt files use
                # /v1/runners/run_crt, everything else just
                # /v1/runners/run with the appropriate content
                # type. The firmware sniffs the file format from
                # the bytes so we don't need to be too clever
                # about content-type.
                if ext == "crt":
                    path = "/v1/runners/run_crt"
                else:
                    path = "/v1/runners/run"
                url = f"http://{host}:{http_port}{path}"
                req = urllib.request.Request(
                    url, data=file_bytes, method="PUT")
                if password:
                    import base64
                    auth = base64.b64encode(
                        f":{password}".encode()).decode()
                    req.add_header(
                        "Authorization", f"Basic {auth}")
                try:
                    with urllib.request.urlopen(
                            req, timeout=10) as resp:
                        resp.read()
                except Exception as e:
                    QMessageBox.warning(
                        self, "Run on U64",
                        f"Couldn't post to {host}:{http_port}:"
                        f"\n\n{e}")

            self.on_run = _post_to_u64
            return True

        def _on_mount(self):
            f = self._selected_file()
            if not f:
                return
            if self.on_mount is None:
                # Standalone browser - set up a mount callback
                # for the user-picked U64 on the fly.
                if not self._setup_standalone_mount_callback():
                    return
            self._download_pending_action = "mount"
            self._start_download(f)

        def _setup_standalone_mount_callback(self) -> bool:
            """Like _setup_standalone_run_callback but for disk-
            image mount. Posts to /v1/runners/mount via PUT and
            then triggers a reset so the C64 actually boots the
            mounted disk."""
            mw = self.parent()
            while (mw is not None
                   and not hasattr(mw, "config")):
                mw = (mw.parent() if hasattr(mw, "parent")
                       else None)
            cfg = getattr(mw, "config", {}) if mw else {}
            from .u64_devices import pick_device
            device = pick_device(
                self, cfg,
                title="Mount disk on U64",
                prompt="Which Ultimate-64 should mount the disk?")
            if device is None:
                return False
            host = device.get("host", "") or ""
            http_port = int(device.get("http_port", 80))
            password = device.get("password", "") or ""

            def _post_mount(file_bytes: bytes,
                             filename: str = "",
                             drive: str = "a",
                             mode: str = "readonly") -> None:
                """Upload disk image to the picked U64 and
                trigger a reset so it boots from the disk."""
                import urllib.request, base64
                # Firmware accepts /v1/runners/mount?image_type=...
                # but the simplest path is /v1/runners/mount which
                # auto-detects from the bytes.
                url = (f"http://{host}:{http_port}"
                       f"/v1/runners/mount")
                req = urllib.request.Request(
                    url, data=file_bytes, method="PUT")
                if password:
                    auth = base64.b64encode(
                        f":{password}".encode()).decode()
                    req.add_header(
                        "Authorization", f"Basic {auth}")
                try:
                    with urllib.request.urlopen(
                            req, timeout=10) as resp:
                        resp.read()
                except Exception as e:
                    QMessageBox.warning(
                        self, "Mount on U64",
                        f"Couldn't mount on {host}:"
                        f"\n\n{e}")

            self.on_mount = _post_mount
            return True

        def _on_download_to_disk(self):
            f = self._selected_file()
            if not f:
                return
            self._download_pending_action = "savedialog"
            self._start_download(f)

        def _on_run_in_emulator(self):
            """Download the selected file to a temp folder then
            launch it in the configured C64 emulator. Same
            launcher used by lister's 'Run in Emulator' and the
            DB browser's Run actions, so all three respect the
            same emulator path/args config.
            """
            f = self._selected_file()
            if not f:
                return
            self._download_pending_action = "run_in_emulator"
            self._start_download(f)

        def _start_download(self, file_info):
            if (self._download_worker is not None
                    and self._download_worker.isRunning()):
                return
            entry = self._current_entry
            if entry is None:
                QMessageBox.warning(self, "Download",
                    "No release selected.")
                return
            # See _fetch_files_for: use entry.category as URL cat
            # even when 0; siteCategory is display metadata.
            cat = entry.category
            self.lbl_status.setText(f"downloading {file_info.name}...")
            self._download_worker = _DownloadWorker(
                self.client, entry.id, cat, file_info, self)
            self._download_worker.done.connect(
                self._on_download_done)
            self._download_worker.start()

        def _on_download_done(self, ok, data, filename, error):
            if not ok:
                self.lbl_status.setText(
                    f"download failed: {error}")
                QMessageBox.warning(self, "Download",
                    f"Failed to download {filename}:\n{error}")
                self._download_pending_action = None
                return
            self.lbl_status.setText(
                f"got {len(data)} bytes for {filename}")
            action = self._download_pending_action
            self._download_pending_action = None
            if action == "run":
                # Run via the host callback in a background thread.
                # For disk images this can take 4+ seconds (mount,
                # reset, wait for BASIC, type LOAD, wait for SEARCH,
                # type RUN) - blocking the UI for that long would
                # look like the app froze.
                self._run_in_background(
                    "Run", self.on_run, data, filename)
            elif action == "mount":
                self._run_in_background(
                    "Mount", self.on_mount, data, filename,
                    drive='a', mode='readonly')
            elif action == "savedialog":
                path, _ = QFileDialog.getSaveFileName(
                    self, "Save as", filename,
                    "All files (*)")
                if path:
                    try:
                        with open(path, 'wb') as fh:
                            fh.write(data)
                        self.lbl_status.setText(
                            f"saved {len(data)} bytes to {path}")
                    except OSError as e:
                        QMessageBox.warning(self, "Save",
                            f"Failed:\n{e}")
            elif action == "run_in_emulator":
                # Stash the downloaded bytes in a temp file and
                # launch the configured C64 emulator on it. We
                # use the same temp-cache layout as the DB browser
                # so subsequent runs of the same release reuse
                # the file instead of re-downloading.
                self._launch_in_local_emulator(data, filename)

        def _launch_in_local_emulator(self, data, filename):
            """Write `data` to a temp file under
            %TEMP%/quopus_asm64_run/ and start the configured
            C64 emulator on the result. Reuses the file if it
            already exists (deterministic name keyed on the
            release id + filename) so back-to-back runs of the
            same release skip the disk-write step.

            Errors are surfaced via QMessageBox - we can't just
            update lbl_status because the emulator launch may
            silently fail (bad path) and we want the user to
            see what's wrong.
            """
            import tempfile
            from pathlib import Path as _Path
            entry = self._current_entry
            entry_id = (entry.id if entry is not None else 0)
            tmp_root = (_Path(tempfile.gettempdir())
                        / "quopus_asm64_run"
                        / f"{entry_id}")
            try:
                tmp_root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(
                    self, "Run in Emulator",
                    f"Couldn't create temp folder:\n{e}")
                return
            # Sanitise filename for cross-platform safety
            safe = "".join(
                c if (c.isalnum() or c in "._- ") else "_"
                for c in filename) or "asm64_file"
            out_path = tmp_root / safe
            if not out_path.is_file() or (
                    out_path.stat().st_size != len(data)):
                try:
                    out_path.write_bytes(data)
                except OSError as e:
                    QMessageBox.warning(
                        self, "Run in Emulator",
                        f"Couldn't write temp file:\n{e}")
                    return
            self.lbl_status.setText(
                f"Launching {safe} in emulator...")
            # Reach the main window's config dict to get the
            # configured emulator path/args. Falls back to a
            # one-time prompt if not yet set (the launcher
            # handles that automatically).
            mw = self.parent()
            while mw is not None and not hasattr(mw, 'config'):
                mw = mw.parent() if hasattr(mw, 'parent') else None
            cfg = getattr(mw, 'config', {}) if mw else {}
            try:
                from .c64_disasm import run_in_c64_emulator
                from .config import save_config
                run_in_c64_emulator(
                    out_path, self, cfg,
                    lambda: save_config(cfg) if cfg else None)
            except Exception as e:
                QMessageBox.warning(
                    self, "Run in Emulator",
                    f"Couldn't launch emulator:\n\n{e}")

        # -------- Background action runner ------------------------

        def _run_in_background(self, label, callback, *args, **kwargs):
            """Run a callback (Run / Mount on the host) in a worker
            thread so a multi-step REST sequence doesn't freeze the
            dialog. Errors come back via a status update + popup."""
            self.lbl_status.setText(f"{label.lower()}ing on device...")

            class _ActionWorker(QThread):
                done = pyqtSignal(bool, str)
                # ok, error_message

                def __init__(self, cb, args, kwargs, parent=None):
                    super().__init__(parent)
                    self.cb = cb
                    self.args = args
                    self.kwargs = kwargs

                def run(self):
                    try:
                        self.cb(*self.args, **self.kwargs)
                        self.done.emit(True, "")
                    except Exception as e:
                        self.done.emit(False, str(e))

            worker = _ActionWorker(callback, args, kwargs, self)
            # Hold a reference on self so the QThread isn't garbage-
            # collected mid-run. Replacing it is fine since at most
            # one device action is in flight (run/mount are triggered
            # by user clicks that disable the buttons via Qt's normal
            # focus handling).
            self._action_worker = worker

            def _on_action_done(ok, msg):
                if ok:
                    self.lbl_status.setText(f"{label.lower()}: OK")
                else:
                    self.lbl_status.setText(
                        f"{label.lower()} failed: {msg}")
                    QMessageBox.warning(self, label,
                        f"{label} failed:\n{msg}")
            worker.done.connect(_on_action_done)
            worker.start()

        # -------- Context menus -----------------------------------

        def _on_results_context_menu(self, pos):
            item = self.results.itemAt(pos)
            if item is None:
                return
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            menu = QMenu(self)
            menu.addAction("Open CSDB page",
                lambda: self._open_csdb_for(entry))
            menu.addAction("Toggle favorite",
                lambda: self._toggle_fav_for(entry))
            menu.exec(self.results.viewport().mapToGlobal(pos))

        def _on_files_context_menu(self, pos):
            item = self.files.itemAt(pos)
            if item is None:
                return
            f = item.data(0, Qt.ItemDataRole.UserRole)
            menu = QMenu(self)
            if self.on_run is not None:
                menu.addAction("Run on U64",
                    lambda: self._download_pending_run(f))
            if self.on_mount is not None:
                menu.addAction("Mount on Drive A (RO)",
                    lambda: self._download_pending_mount(f, 'a',
                                                            'readonly'))
                menu.addAction("Mount on Drive A (RW)",
                    lambda: self._download_pending_mount(f, 'a',
                                                            'readwrite'))
                menu.addAction("Mount on Drive B (RO)",
                    lambda: self._download_pending_mount(f, 'b',
                                                            'readonly'))
            menu.addAction("Download to disk...",
                lambda: self._download_pending_save(f))
            menu.addSeparator()
            # Run in emulator (separate from "Run on U64" which
            # uses on_run callback for U64 hardware). This one
            # writes to a temp file and launches the configured
            # C64 emulator on the local machine - works without
            # any U64 hardware.
            menu.addAction("Run in Emulator (local)",
                lambda: self._download_pending_run_emulator(f))
            menu.exec(self.files.viewport().mapToGlobal(pos))

        def _download_pending_run_emulator(self, file_info):
            self._download_pending_action = "run_in_emulator"
            self._start_download(file_info)

        def _download_pending_run(self, file_info):
            self._download_pending_action = "run"
            self._start_download(file_info)

        def _download_pending_mount(self, file_info, drive, mode):
            self._download_pending_drive = drive
            self._download_pending_mode = mode
            self._download_pending_action = "mount"
            self._start_download(file_info)

        def _download_pending_save(self, file_info):
            self._download_pending_action = "savedialog"
            self._start_download(file_info)

        # -------- CSDB deep-link ---------------------------------

        def _on_open_csdb(self):
            if self._current_entry is None:
                return
            self._open_csdb_for(self._current_entry)

        def _open_csdb_for(self, entry):
            # Assembly64 IDs ARE the CSDB IDs - the deep-link works
            # directly without a separate csdb_id field.
            url = ASM64Client.csdb_release_url(entry.id)
            if not url:
                QMessageBox.information(self, "CSDB",
                    "This entry has no CSDB id.")
                return
            QDesktopServices.openUrl(QUrl(url))

        # -------- Favorites ---------------------------------------

        def _load_favorites_into_list(self):
            self.lst_favs.clear()
            for d in load_favorites():
                e = ASM64Entry.from_json(d)
                label = e.name
                if e.group:
                    label += f"  by {e.group}"
                if e.year and e.year > 0:
                    label += f"  ({e.year})"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, e)
                self.lst_favs.addItem(item)

        # -------- Last-results restore ----------------------------

        def _restore_last_results(self):
            """If a cached search session is available, pre-fill the
            filter form with the params that produced it and load
            the entries into the results table. The user can then
            continue browsing, click "Search" to refresh against the
            live server, or change filters.

            Called once during dialog construction. Silent no-op if
            no cache exists or it's unparseable."""
            cache = load_last_results()
            if not cache:
                return
            params = cache.get("params") or {}
            raw_entries = cache.get("entries") or []
            if not raw_entries:
                return

            # Repopulate filter form. _set_combo_by_data handles the
            # case where the cached value isn't in the current combo
            # (e.g. someone added a new file type since the cache
            # was written) by falling back to setEditText.
            self.ed_name.setText(params.get("name", ""))
            self.ed_group.setText(params.get("group", ""))
            self.ed_handle.setText(params.get("handle", ""))
            self.ed_year.setText(str(params.get("year", "") or ""))
            # Category combobox: pick the entry whose itemData
            # matches the saved int id, default to "(any)"
            cat_val = params.get("category", 0)
            try:
                cat_id = int(cat_val) if cat_val else 0
            except (TypeError, ValueError):
                cat_id = 0
            for _i in range(self.cmb_category.count()):
                if self.cmb_category.itemData(_i) == cat_id:
                    self.cmb_category.setCurrentIndex(_i)
                    break
            else:
                self.cmb_category.setCurrentIndex(0)
            ft = params.get("file_type", params.get("type_", ""))
            self._set_combo_by_data(self.cmb_type, ft)
            self._update_aql_preview()

            # Repopulate result table. Each cached row is a raw
            # server JSON dict; ASM64Entry.from_json parses it back
            # the same way the live server response is handled.
            entries = [ASM64Entry.from_json(j)
                       for j in raw_entries
                       if isinstance(j, dict)]
            self.results.setSortingEnabled(False)
            for e in entries:
                year_str = str(e.year) if e.year > 0 else ""
                cat_str = self._category_label(
                    e.category, e.site_category,
                    e.category_name)
                item = _SortableItem([
                    e.name, e.group, year_str, cat_str,
                ])
                item.setData(2, Qt.ItemDataRole.UserRole + 1, e.year)
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                self.results.addTopLevelItem(item)
            self.results.setSortingEnabled(True)

            self._last_search_params = dict(params)
            ts = cache.get("timestamp", "")
            ts_short = ts[:19].replace("T", " ") if ts else ""
            if ts_short:
                self.lbl_status.setText(
                    f"{len(entries)} results (cached {ts_short})")
            else:
                self.lbl_status.setText(
                    f"{len(entries)} results (cached)")
            # Cached results path: also try to learn any unknown
            # CSDB release types now. The user might have closed
            # and reopened the dialog with results from a
            # previous session that pre-dates the learner code.
            self._kick_off_type_learning(entries)

        def _on_favorite_loaded(self, item):
            """Double-clicking a favorite re-runs a search by name
            so we get a fresh entry list."""
            e = item.data(Qt.ItemDataRole.UserRole)
            if e is None:
                return
            self.ed_name.setText(e.name)
            self._on_search()

        def _on_favorite_toggle(self):
            if self._current_entry is None:
                return
            self._toggle_fav_for(self._current_entry)

        def _toggle_fav_for(self, entry):
            favs = load_favorites()
            # Match by name+group (id may not be stable across
            # sources)
            matched = [
                i for i, f in enumerate(favs)
                if f.get("name") == entry.name
                and f.get("group") == entry.group]
            if matched:
                for i in reversed(matched):
                    favs.pop(i)
            else:
                favs.append(asdict(entry))
            save_favorites(favs)
            self._load_favorites_into_list()
            self._refresh_fav_button(entry)

        def _refresh_fav_button(self, entry):
            favs = load_favorites()
            is_fav = any(
                f.get("name") == entry.name
                and f.get("group") == entry.group
                for f in favs)
            self.btn_fav.setText(
                "Remove from favorites" if is_fav
                else "Add to favorites")

        # -------- Saved searches ----------------------------------

        def _load_searches_into_combo(self):
            self.cmb_saved.blockSignals(True)
            self.cmb_saved.clear()
            self.cmb_saved.addItem("(none)", None)
            for s in load_saved_searches():
                self.cmb_saved.addItem(
                    s.get("name", "?"), s)
            self.cmb_saved.blockSignals(False)

        def _on_save_search(self):
            params = self._gather_params()
            # Skip if all empty
            if not any(v for v in params.values()
                       if not isinstance(v, int)):
                QMessageBox.information(self, "Save search",
                    "Enter some filter values first.")
                return
            name, ok = QInputDialog.getText(self, "Save search",
                "Name for this search:")
            if not ok or not name.strip():
                return
            searches = load_saved_searches()
            # Replace if name exists
            searches = [s for s in searches
                        if s.get("name") != name.strip()]
            searches.append({
                "name": name.strip(),
                "params": params,
            })
            save_saved_searches(searches)
            self._load_searches_into_combo()

        def _on_saved_picked(self, idx):
            if idx <= 0:
                return
            entry = self.cmb_saved.itemData(idx)
            if not entry:
                return
            params = entry.get("params", {})
            self.ed_name.setText(params.get("name", ""))
            self.ed_group.setText(params.get("group", ""))
            self.ed_handle.setText(params.get("handle", ""))
            self.ed_year.setText(params.get("year", ""))
            cat_val = params.get("category", 0)
            try:
                cat_id = int(cat_val) if cat_val else 0
            except (TypeError, ValueError):
                cat_id = 0
            for _i in range(self.cmb_category.count()):
                if self.cmb_category.itemData(_i) == cat_id:
                    self.cmb_category.setCurrentIndex(_i)
                    break
            else:
                self.cmb_category.setCurrentIndex(0)
            ft = params.get("file_type", params.get("type_", ""))
            self._set_combo_by_data(self.cmb_type, ft)
            self._on_search()

        def _set_combo_by_data(self, cmb, value):
            for i in range(cmb.count()):
                if cmb.itemData(i) == value:
                    cmb.setCurrentIndex(i)
                    return
            # If the type combo is editable and the value isn't in
            # the list, just set the text.
            if cmb.isEditable() and value:
                cmb.setEditText(value)

        def _on_delete_search(self):
            idx = self.cmb_saved.currentIndex()
            if idx <= 0:
                return
            entry = self.cmb_saved.itemData(idx)
            if not entry:
                return
            name = entry.get("name", "?")
            reply = QMessageBox.question(
                self, "Delete saved search",
                f"Delete saved search '{name}'?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            searches = load_saved_searches()
            searches = [s for s in searches
                        if s.get("name") != name]
            save_saved_searches(searches)
            self._load_searches_into_combo()

        # -------- Endpoints override ------------------------------

        def _on_endpoints_edit(self):
            """Pop a dialog that lets the user override URL paths.
            For diagnosing endpoint changes on the server side."""
            from PyQt6.QtWidgets import (
                QDialog as QD, QFormLayout as QF,
                QDialogButtonBox as QDBB,
            )
            d = QD(self)
            d.setWindowTitle("Assembly64 Endpoint Override")
            d.resize(540, 0)
            form = QF(d)
            form.setContentsMargins(10, 10, 10, 10)
            info = QLabel(
                "Override the URL paths if the server has moved them.\n"
                "Defaults come from the Ultimate 64 firmware:\n"
                "  search   = /search/aql\n"
                "  presets  = /search/aql/presets\n"
                "  entries  = /search/entries\n"
                "  download = /search/bin\n"
                "Leave blank to use the default.")
            info.setStyleSheet("color: #666;")
            form.addRow(info)
            edits = {}
            for k in ("search", "presets", "entries", "download"):
                le = QLineEdit(self.client.paths.get(k, ""))
                edits[k] = le
                form.addRow(f"{k}:", le)
            bb = QDBB(
                QDBB.StandardButton.Ok | QDBB.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            form.addRow(bb)
            if d.exec() == QD.DialogCode.Accepted:
                for k, le in edits.items():
                    txt = le.text().strip()
                    if txt:
                        self.client.paths[k] = txt
                self.lbl_status.setText("endpoints updated")

    return _BrowserDialog()
