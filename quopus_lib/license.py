"""License loading + verification + feature gating.

Architecture:

  License generation (offline, only you):
    1. `license_keygen.py` reads your secret Ed25519 key
    2. Builds a license JSON: name, email, features, expires...
    3. Signs the JSON with Ed25519, packs into .lic file
    4. You email the .lic to the customer

  License verification (in Quopus, at startup):
    1. Look for `quopus.lic` next to the EXE / in CONFIG_DIR
    2. Parse, verify the Ed25519 signature using the BUILTIN
       public key
    3. Check expiry date
    4. Populate the in-memory feature flags

  Feature gating (everywhere in Quopus):
    - `is_registered()` -> bool
    - `has_feature(name)` -> bool
    - `license_holder()` -> "Name <email>"
    - `derive_decrypt_key()` -> bytes for AES-GCM decrypt of
      premium modules

Why Ed25519:
  - Signatures are short (64 bytes)
  - Verification is fast and CPU-cheap
  - The library is well-audited (`cryptography` package)
  - The signing key is ONLY known to you. Even with the entire
    Quopus source code, nobody can forge a license without it.

What this DOESN'T protect against:
  - Someone with a valid .lic sharing it (user-bound, multi-PC
    is intentional)
  - Patching out the `is_registered()` call in source-recovered
    Python (raises nag screen but doesn't enable premium - those
    are AES-encrypted, see crypto.py)
  - Server bans (we don't phone home; once issued a license is
    forever valid until its expiry)

These are conscious tradeoffs.
"""
import base64
import hashlib
import json
import platform
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import SCRIPT_DIR, CONFIG_DIR


# Embedded Ed25519 public key. This is the DEMO key that ships
# with every public Quopus release. It can verify ONLY trial-tier
# licenses (enforced below). Production / Pro licenses must be
# signed with a SEPARATE production key configured via
# config/quopus_keys.cfg.
#
# LOADING ORDER:
#   1. CONFIG_DIR/quopus_keys.cfg          (preferred - survives updates)
#   2. SCRIPT_DIR/quopus_keys.cfg          (alternate location)
#   3. QUOPUS_LICENSE_PUBKEY_HEX below     (DEMO - trial only!)
#
# Why this matters:
#   - Public ZIPs ship with the demo key as the fallback
#   - That key's private half is also in the public repo as
#     demo_signer_secret.key - anyone can issue trial licenses
#   - Trial licenses are harmless (they trigger the same limits
#     as having no license at all) but they remove the nag-screen
#     in custom builds for testing
#   - Production licenses use Mario's REAL signing key, which never
#     leaves his machine. Customers receive his real pubkey via
#     config/quopus_keys.cfg shipped alongside the .lic file
#
# WHAT'S RESTRICTED FOR DEMO-KEY-SIGNED LICENSES:
#   - Tier MUST be "trial". Pro or lifetime licenses signed with
#     the demo key are rejected as forgeries.
#   - This protects against someone copying the public demo key
#     hex from the source and issuing "themselves" a Pro license -
#     the verifier will see "demo-key + pro-tier" and reject it.
QUOPUS_LICENSE_PUBKEY_HEX = (
    "fa538a4109e453c465a7c01025340b709559dfbd7ceb97798c528096f20c15b1"
)

# Same key string as a constant so the runtime can tell whether
# it's running with the demo key or a production override. Used
# to enforce the "demo key can only verify trial licenses" rule.
_DEMO_PUBKEY_HEX = (
    "fa538a4109e453c465a7c01025340b709559dfbd7ceb97798c528096f20c15b1"
)


def _xdg_config_dir():
    """User-config directory in the XDG sense. Returns a Path that
    points at the platform-conventional location for per-user
    application config:

      - Linux/BSD: $XDG_CONFIG_HOME/quopus, or ~/.config/quopus if
        XDG_CONFIG_HOME isn't set
      - macOS:     ~/Library/Application Support/quopus
      - Windows:   %APPDATA%/quopus, or ~/AppData/Roaming/quopus

    We use this as an ADDITIONAL search location for quopus.lic
    and quopus_keys.cfg, on top of the application-bundled paths.
    The point is letting a user drop their license into the
    "obvious" per-user location even if Quopus was installed
    system-wide (where SCRIPT_DIR/config/ isn't writable by the
    user). Returns None if the path can't be resolved (extremely
    rare - basically only happens in stripped chroots without a
    home directory).
    """
    import os, sys
    try:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        elif sys.platform.startswith("win"):
            base = Path(os.environ.get(
                "APPDATA",
                str(Path.home() / "AppData" / "Roaming")))
        else:
            base = Path(os.environ.get(
                "XDG_CONFIG_HOME",
                str(Path.home() / ".config")))
        return base / "quopus"
    except Exception:
        return None


def _keys_config_paths():
    """Where to look for the override config file.

    Priority:
      1. CONFIG_DIR/quopus_keys.cfg          - per-install config
                                               (survives updates)
      2. SCRIPT_DIR/quopus_keys.cfg          - next to the EXE,
                                               for portable setups
      3. <XDG user config>/quopus_keys.cfg   - per-user fallback
                                               for system-wide
                                               installs where the
                                               install dir is
                                               read-only

    The XDG entry is platform-aware: ~/.config/quopus on Linux,
    ~/Library/Application Support/quopus on macOS, %APPDATA%/quopus
    on Windows. First file found wins.
    """
    paths = [
        CONFIG_DIR / "quopus_keys.cfg",
        SCRIPT_DIR / "quopus_keys.cfg",
    ]
    xdg = _xdg_config_dir()
    if xdg is not None:
        paths.append(xdg / "quopus_keys.cfg")
    return paths


def _load_pubkey_override():
    """Read public key hex from the first quopus_keys.cfg we
    find. Returns None if no file exists or the file is malformed
    (in which case we fall back to the baked-in constant).

    File format is plain INI-style key=value:

        # quopus_keys.cfg
        # Public key for license signature verification
        public_key_hex = 2b64c008bf9cacc5...

    Comments (#) and blank lines are ignored. We accept either
    'public_key_hex' or just 'pubkey' as the field name."""
    for path in _keys_config_paths():
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if key in ("public_key_hex", "pubkey", "publickey"):
                    if len(val) == 64 and all(
                            c in "0123456789abcdefABCDEF"
                            for c in val):
                        return val.lower()
        except OSError:
            continue
    return None


def _effective_pubkey_hex():
    """Return the public key hex string we should use - either
    the override from quopus_keys.cfg or the baked-in constant."""
    override = _load_pubkey_override()
    if override:
        return override
    return QUOPUS_LICENSE_PUBKEY_HEX


# License file format - version field lets us evolve later
LICENSE_FORMAT_VERSION = 1


@dataclass
class LicenseInfo:
    """Parsed and verified license data."""
    valid: bool = False
    name: str = ""
    email: str = ""
    license_id: str = ""        # UUID, unique per .lic file
    issued_at: int = 0          # Unix timestamp
    expires_at: int = 0         # Unix timestamp, 0 = never
    features: list = field(default_factory=list)
    tier: str = "trial"         # "trial" | "pro" | "lifetime"
    error: str = ""             # populated on validation failure


# Module-level cache - we load the license once at startup and
# the rest of Quopus reads from this singleton.
_LICENSE: Optional[LicenseInfo] = None


def _public_key():
    """Lazy-load the verification key. Raises if cryptography lib
    isn't installed - we treat that as 'no license possible' since
    we can't verify any signature without it.

    Looks up the public key via _effective_pubkey_hex() which
    prefers the user's config-file override over the baked-in
    constant. This means a Quopus install that's been customized
    with a production key keeps working across updates - the new
    ZIP overwrites this file but NOT the config/ folder where
    the override lives."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 \
            import Ed25519PublicKey
    except ImportError:
        return None
    key_bytes = bytes.fromhex(_effective_pubkey_hex())
    return Ed25519PublicKey.from_public_bytes(key_bytes)


def _license_paths():
    """Return the list of locations we check for quopus.lic,
    in priority order. The first existing one wins.

    Searches:
      1. CONFIG_DIR/quopus.lic           - per-install config
      2. SCRIPT_DIR/quopus.lic           - next to the EXE
      3. <XDG user config>/quopus.lic    - per-user fallback for
                                           system-wide installs
    """
    paths = [
        CONFIG_DIR / "quopus.lic",
        SCRIPT_DIR / "quopus.lic",
    ]
    xdg = _xdg_config_dir()
    if xdg is not None:
        paths.append(xdg / "quopus.lic")
    return paths


def _debug_log_path() -> Path:
    """Return the path where license_debug.log lives.

    Priority:
      1. The platform user-config dir (XDG / APPDATA / Library)
         — same place quopus.lic and quopus_keys.cfg can live.
         This is the right answer for almost every install: the
         file ends up in a per-user location that is NEVER inside
         the application's working tree or a git checkout, so
         developers / sysops who ship their Quopus folder to
         others (review builds, demos, screenshots) can't
         accidentally leak the log.
      2. CONFIG_DIR fallback — only used when the user-config
         dir is unreachable (extremely rare, e.g. broken HOME).
         Still better than SCRIPT_DIR because at least the user
         can put CONFIG_DIR under .gitignore.

    Whichever path is returned, the parent dir is created on
    demand so the first call doesn't fail with ENOENT.
    """
    xdg = _xdg_config_dir()
    if xdg is not None:
        try:
            xdg.mkdir(parents=True, exist_ok=True)
            return xdg / "license_debug.log"
        except OSError:
            pass
    # Fallback: CONFIG_DIR (under SCRIPT_DIR/config). NEVER
    # SCRIPT_DIR itself - that's right next to quopus.py and a
    # magnet for accidental commits.
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return CONFIG_DIR / "license_debug.log"


def _debug_log(message: str):
    """Write license-loading diagnostics to a file. Helps debug
    "license file not found" / "license invalid" reports from
    users.

    The log goes into the platform's per-user config directory
    (see _debug_log_path() for the exact rules). It is NEVER
    written next to the EXE / next to quopus.py - that path used
    to leak users' emails and license-feature lists when people
    zipped up their Quopus folder for sharing or committed it to
    a repo without thinking. The new location is outside any
    plausible working tree.

    Failures here are silent - logging must never break the
    license check itself.
    """
    try:
        log_path = _debug_log_path()
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def find_license_file() -> Optional[Path]:
    """Return the path to a quopus.lic, or None if none exists."""
    _debug_log("=" * 60)
    _debug_log(f"License search started")
    _debug_log(f"SCRIPT_DIR = {SCRIPT_DIR}")
    _debug_log(f"CONFIG_DIR = {CONFIG_DIR}")
    _debug_log(f"sys.executable = {sys.executable}")
    _debug_log(f"frozen = {getattr(sys, 'frozen', False)}")
    # Log which public key we're using - this is the most common
    # source of "license not valid" surprises after updates.
    override = _load_pubkey_override()
    if override:
        _debug_log(f"Public key OVERRIDE active from quopus_keys.cfg")
        _debug_log(f"  effective: {override[:16]}...{override[-8:]}")
    else:
        _debug_log(f"Public key from baked-in constant (no override)")
        _debug_log(f"  effective: "
                   f"{QUOPUS_LICENSE_PUBKEY_HEX[:16]}..."
                   f"{QUOPUS_LICENSE_PUBKEY_HEX[-8:]}")
    for p in _license_paths():
        exists = p.is_file()
        _debug_log(f"  check {p} -> exists={exists}")
        if exists:
            try:
                size = p.stat().st_size
                _debug_log(f"    size = {size} bytes")
            except OSError:
                pass
            return p
    _debug_log("No license file found in any search path")
    return None


def parse_license_file(path: Path) -> LicenseInfo:
    """Read a .lic file and verify its signature.

    File format is intentionally simple - a JSON object with a
    'payload' (the license data) and a 'signature' (base64-encoded
    Ed25519 sig over the canonical-JSON payload). Anyone can read
    the file in a text editor and see what they paid for; only
    you can produce a valid signature."""
    _debug_log(f"Parsing {path}")
    info = LicenseInfo()
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        info.error = f"Cannot read license file: {e}"
        _debug_log(f"  FAIL: {info.error}")
        return info

    payload = doc.get("payload")
    sig_b64 = doc.get("signature")
    if not isinstance(payload, dict) or not isinstance(sig_b64, str):
        info.error = "License file has wrong structure"
        return info

    pub = _public_key()
    if pub is None:
        info.error = ("cryptography library missing - "
                      "license verification disabled")
        _debug_log(f"  FAIL: {info.error}")
        return info

    # Canonical re-serialization so the signature was computed
    # over the same byte sequence we just got. sort_keys=True
    # and separators=(',', ':') strip every formatting choice.
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    try:
        from cryptography.exceptions import InvalidSignature
        sig = base64.b64decode(sig_b64)
        pub.verify(sig, canonical)
        _debug_log("  Signature verified OK")
    except InvalidSignature:
        info.error = "Signature mismatch - license is forged or corrupt"
        _debug_log(f"  FAIL: {info.error}")
        _debug_log(f"  This usually means: license was signed with a")
        _debug_log(f"  different key than the one built into this EXE.")
        _debug_log(f"  Built-in public key starts: "
                   f"{QUOPUS_LICENSE_PUBKEY_HEX[:16]}...")
        return info
    except Exception as e:
        info.error = f"Signature verification failed: {e}"
        _debug_log(f"  FAIL: {info.error}")
        return info

    # Signature OK - read the fields
    if payload.get("format_version") != LICENSE_FORMAT_VERSION:
        info.error = (
            f"License format v{payload.get('format_version')} "
            f"not supported (this Quopus expects v"
            f"{LICENSE_FORMAT_VERSION})")
        _debug_log(f"  FAIL: {info.error}")
        return info

    info.name = str(payload.get("name", ""))
    info.email = str(payload.get("email", ""))
    info.license_id = str(payload.get("license_id", ""))
    info.issued_at = int(payload.get("issued_at", 0))
    info.expires_at = int(payload.get("expires_at", 0))
    info.features = list(payload.get("features", []))
    info.tier = str(payload.get("tier", "pro"))

    # Demo-key restriction: if the signature was verified by the
    # DEMO key (the one shipped publicly), this license MUST be
    # tier='trial'. The demo private key is in the public source
    # repo as demo_signer_secret.key, so anyone can issue licenses
    # with it. We allow trial-tier (it's harmless - same limits
    # as no license) but reject pro/lifetime as forgery attempts.
    #
    # Why we check the EFFECTIVE pubkey instead of the constant:
    # users with a config/quopus_keys.cfg override that ALSO
    # happens to be the demo hex (unusual but possible) still get
    # the demo-key restriction. The override mechanism is for
    # production users who set a different real key.
    effective_pub = _effective_pubkey_hex().lower()
    if effective_pub == _DEMO_PUBKEY_HEX.lower():
        # Running with the demo key as verifier
        if info.tier not in ("trial",):
            info.error = (
                f"License tier '{info.tier}' is not allowed with "
                f"the demo verification key. This Quopus build "
                f"can only accept Trial licenses unless configured "
                f"with a production public key. Contact the "
                f"vendor to purchase a Pro license.")
            _debug_log(f"  FAIL: {info.error}")
            return info
        # Trial license with demo key: also force the features
        # list to be EMPTY. Even if a malicious license includes
        # PRO_TELNET etc, we strip them out so the trial user
        # doesn't accidentally get pro features through the demo
        # key. Tier=trial is for displaying "TRIAL" branding, NOT
        # for unlocking anything.
        if info.features:
            _debug_log(f"  Demo-key trial license: stripping "
                       f"features {info.features}")
            info.features = []

    # Expiry check
    now = int(time.time())
    if info.expires_at and now > info.expires_at:
        info.error = (
            f"License expired on "
            f"{time.strftime('%Y-%m-%d', time.gmtime(info.expires_at))}")
        _debug_log(f"  FAIL: {info.error}")
        return info

    info.valid = True
    _debug_log(f"  OK: license valid for {info.name} <{info.email}>")
    _debug_log(f"      tier: {info.tier}, features: {info.features}")
    return info


def load_license() -> LicenseInfo:
    """Load + verify the license, caching the result.

    Returns a LicenseInfo with .valid=False if no license is found
    or verification fails. The rest of Quopus treats !valid as
    "trial mode" - degraded features but still usable."""
    global _LICENSE
    if _LICENSE is not None:
        return _LICENSE
    path = find_license_file()
    if path is None:
        _LICENSE = LicenseInfo(error="No quopus.lic file found")
        return _LICENSE
    _LICENSE = parse_license_file(path)
    return _LICENSE


def reload_license() -> LicenseInfo:
    """Drop the cached LicenseInfo and re-read from disk. Used
    by the License Info dialog after the user imports a new
    .lic file - lets the rest of Quopus (title-bar watermark,
    feature gates, About text) pick up the new state without a
    restart.
    """
    global _LICENSE
    _LICENSE = None
    return load_license()


def diagnostic_report() -> dict:
    """Return a dict of license-loading diagnostics for the UI
    to display. Helps users (and us, when they email a screenshot)
    figure out WHY a license isn't loading as expected. Covers:

      - The exact paths Quopus searched for quopus.lic
      - Which of those paths existed
      - The same for quopus_keys.cfg
      - The effective public key (Demo vs Production)
      - A short text summary of "what's wrong" when there's a
        mismatch (e.g. license signed with prod key but no
        keys.cfg installed -> verification fails)

    The dict shape is stable: lic_search_paths, lic_found,
    keys_search_paths, keys_found, pubkey_effective_hex,
    pubkey_is_demo, problem (text or None).
    """
    out = {
        "lic_search_paths": [str(p) for p in _license_paths()],
        "lic_found": None,
        "keys_search_paths": [str(p) for p in _keys_config_paths()],
        "keys_found": None,
        "pubkey_effective_hex": _effective_pubkey_hex(),
        "pubkey_is_demo": (
            _effective_pubkey_hex().lower()
            == _DEMO_PUBKEY_HEX.lower()),
        "problem": None,
    }
    # Locate the first existing license file (if any)
    for p in _license_paths():
        if p.is_file():
            out["lic_found"] = str(p)
            break
    for p in _keys_config_paths():
        if p.is_file():
            out["keys_found"] = str(p)
            break
    # Heuristic problem summary - matches the common failure
    # modes we see in support emails. Order matters; the most
    # specific case wins.
    lic = load_license()
    if out["lic_found"] is None:
        out["problem"] = (
            "No quopus.lic file found in any of the searched "
            "locations. Copy your license file to one of these "
            "directories.")
    elif lic.error and "Signature mismatch" in lic.error:
        if out["pubkey_is_demo"]:
            out["problem"] = (
                "License signature doesn't match the built-in "
                "Demo public key. This usually means: the .lic "
                "file was signed with the Production key, but "
                "quopus_keys.cfg (containing the Production "
                "public key) wasn't installed. Copy both files "
                "from the license ZIP into the same directory.")
        else:
            out["problem"] = (
                "License signature doesn't verify with the "
                "currently-configured Production public key. "
                "Either the .lic file is from a different "
                "issuer, or quopus_keys.cfg contains the wrong "
                "key. Re-extract the license ZIP and try again.")
    elif lic.valid and out["pubkey_is_demo"] and lic.tier == "trial":
        # Common case: paid license but no keys.cfg, so we fell
        # back to demo key, which accepted the lic only as trial
        # AND stripped its features. User thinks "I have a Pro
        # license, why is the trial cap still active?"
        if lic.features == []:
            out["problem"] = (
                "License loaded with the built-in Demo key, "
                "which forces tier='trial' and strips all "
                "premium features. To unlock Pro features, "
                "install quopus_keys.cfg (the Production "
                "public key) alongside your quopus.lic file. "
                "Both files should be in the same directory.")
    elif (lic.valid and lic.tier in ("pro", "lifetime")
            and not lic.features):
        # Pro/Lifetime tier WITHOUT a features list. Tier alone
        # grants nothing - feature gates check the features array.
        # This usually means the license was issued without an
        # explicit --features arg back when the keygen didn't
        # auto-populate. The user paid for Pro but everything is
        # still gated.
        out["problem"] = (
            "License is valid and tier is '" + lic.tier + "' "
            "BUT the features list is empty. Pro features are "
            "gated on individual feature flags (PRO_DB_UNLIMITED, "
            "PRO_TELNET, PRO_SID, ...) - the tier name alone "
            "doesn't unlock anything. Contact the issuer to "
            "re-issue the license with the correct features.")
    elif (lic.valid and lic.tier in ("pro", "lifetime")
            and lic.features and not all(
                f in lic.features for f in (
                    "PRO_DB_UNLIMITED", "PRO_TELNET",
                    "PRO_SID", "PRO_MULTI",
                    "PRO_PHONEBOOK_UNLIMITED",
                    "PRO_ASM64_SAVE",
                    "PRO_NO_NAG", "PRO_NO_WATERMARK"))):
        # Some but not all Pro flags present. Surface what's
        # missing so the user can see WHY a specific Pro feature
        # isn't working even though their tier shows "PRO".
        all_pro = [
            "PRO_DB_UNLIMITED", "PRO_TELNET", "PRO_SID",
            "PRO_MULTI", "PRO_PHONEBOOK_UNLIMITED",
            "PRO_ASM64_SAVE", "PRO_NO_NAG", "PRO_NO_WATERMARK",
        ]
        missing = [f for f in all_pro if f not in lic.features]
        if missing:
            out["problem"] = (
                "License is valid and tier is '" + lic.tier + "' "
                "but some Pro feature flags are missing from "
                "the license: " + ", ".join(missing) + ". "
                "Quopus only enables features that are EXPLICITLY "
                "listed; the tier name doesn't auto-unlock the "
                "full set. If you should have one or more of "
                "those, ask the issuer to re-issue your license "
                "with --features including the missing flags.")
    elif lic.error:
        out["problem"] = lic.error
    return out


# ============================================================
# Feature flags - the rest of Quopus calls these
# ============================================================


def is_registered() -> bool:
    """True if a valid PAID license is loaded.

    A demo-signed trial license has valid=True (the signature
    checks out) but tier='trial', which we explicitly do NOT
    consider 'registered' for feature-gating purposes. Trial
    licenses exist mainly to put the user's name in the title
    bar instead of just '[TRIAL]' - they don't unlock anything.

    So:
      - No .lic file:                 is_registered() = False
      - Trial license (demo key):     is_registered() = False
      - Pro license (real key):       is_registered() = True
      - Lifetime license (real key):  is_registered() = True
    """
    lic = load_license()
    if not lic.valid:
        return False
    if lic.tier == "trial":
        return False
    return True


def license_holder() -> str:
    """Display string for the registered user, or 'Trial Version'.

    Trial-tier licenses (signed with the demo key) get a special
    string that includes the name but flags it as trial. Pro/
    lifetime licenses just get the name."""
    lic = load_license()
    if not lic.valid:
        return "Trial Version"
    if lic.tier == "trial":
        # Demo-signed trial license. Name shows but with [TRIAL]
        # marker so the user sees it isn't a paid license.
        name = lic.name or lic.email or "Trial"
        return f"{name} (TRIAL)"
    if lic.name and lic.email:
        return f"{lic.name} <{lic.email}>"
    if lic.name:
        return lic.name
    return lic.email or "Registered"


def has_feature(name: str) -> bool:
    """Feature gate. Returns True if the loaded license includes
    the given feature flag, OR if no gate is needed (e.g. core
    Quopus features always available even in trial).

    Premium feature names should be ALL CAPS by convention to
    distinguish them from regular Python strings:
        PRO_TELNET   - SSH + advanced telnet
        PRO_SID      - SID player with full song length
        PRO_MULTI    - Multi-SID parallel playback
        PRO_PHONEBOOK_UNLIMITED - more than 3 saved sessions
        PRO_ASM64_SAVE - save Asm64 search results
        ...
    """
    lic = load_license()
    if not lic.valid:
        return False
    return name in lic.features


# Sentinel features used throughout Quopus. Listed here so we have
# a single source of truth - if you rename one, grep finds every
# caller in one shot.
FEATURE_TELNET = "PRO_TELNET"
FEATURE_SID = "PRO_SID"
FEATURE_MULTI_SID = "PRO_MULTI"
FEATURE_PHONEBOOK_UNLIMITED = "PRO_PHONEBOOK_UNLIMITED"
FEATURE_ASM64_SAVE = "PRO_ASM64_SAVE"
FEATURE_DB_UNLIMITED = "PRO_DB_UNLIMITED"
FEATURE_NO_NAG = "PRO_NO_NAG"
FEATURE_NO_WATERMARK = "PRO_NO_WATERMARK"

# Trial cap on the Quopus Database Browser. Counted on
# disk_images (D64 / D71 / D81 / G64 / TAP / etc) not on the
# `files` table - a single archive can contain dozens of disks
# and a real C64 collection has hundreds of standalone D64s too.
# Loose files (PRGs on disk, archive members) are unlimited;
# only the disk-image count is capped. 1000 is enough for a
# casual user to catalog their main folders, beyond that they
# either need Pro or to maintain multiple .sqlite files manually.
TRIAL_DB_DISK_LIMIT = 1000


def trial_limit(feature_name: str, trial_limit: int,
                pro_limit: int = -1) -> int:
    """Return the relevant numeric limit based on license state.

    Example: trial_limit('phonebook_entries', 3, -1) returns 3 for
    trial users (max 3 phonebook entries), -1 (unlimited) for
    registered users with the PRO_PHONEBOOK_UNLIMITED feature."""
    return pro_limit if is_registered() else trial_limit


# ============================================================
# AES key derivation for premium encrypted modules
# ============================================================


def derive_decrypt_key() -> Optional[bytes]:
    """Derive the AES-256 key used to decrypt premium .qpe files.

    The key is sha256(email + ":" + license_id + ":" + secret_salt)
    where the secret_salt is a constant baked into the encryption
    step at build time. Without a valid license, this returns None
    and the calling code can't decrypt anything.

    The salt below is HARDCODED but the security doesn't rely on
    it being secret - what matters is that you need the email AND
    license_id from a real signed license, which only your private
    key can produce. The salt just makes brute-force unfeasible
    even if someone has a partial license."""
    lic = load_license()
    if not lic.valid:
        return None
    # The salt is just to make sure decrypt keys differ between
    # Quopus and any other software that might use the same email.
    secret_salt = b"quopus_commander_2026_premium_v1"
    h = hashlib.sha256()
    h.update(lic.email.encode("utf-8"))
    h.update(b":")
    h.update(lic.license_id.encode("utf-8"))
    h.update(b":")
    h.update(secret_salt)
    return h.digest()


def reset_cache():
    """For testing / after replacing the license file. Forces a
    re-load on the next is_registered()/has_feature() call."""
    global _LICENSE
    _LICENSE = None
