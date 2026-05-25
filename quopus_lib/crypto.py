"""Runtime decryption + loading of encrypted premium modules.

NOTE ON THE CURRENT LICENSE MODEL:
==================================
Quopus currently uses Model C (source-available with commercial
licenses). The full source is public on GitHub, so encrypting
"premium" .py files would be theatre - anyone can read them
upstream. The infrastructure in this file is RETAINED for future
flexibility (switching to Model B / dual-licensing), but isn't
actively used right now.

In Model C, license protection works through:
  1. Ed25519-signed .lic files (only the author can issue them)
  2. license.has_feature() checks at call sites that enforce trial
     limits
  3. The LICENSE file legally restricting commercial use without
     a paid license

Anyone CAN patch out the has_feature() checks since the source is
public. That's a deliberate tradeoff - we trust the goodwill of
the user base and rely on legal enforcement rather than technical
protection against motivated attackers.

LEGACY DOCSTRING (Model B context):
====================================
The build pipeline (license_keygen.py --encrypt-modules) takes the
plain .py files in quopus_lib/_premium/ and AES-GCM encrypts each
one into a .qpe file. The .py files are deleted before shipping.

At runtime, this module:
  1. Locates a .qpe file
  2. Decrypts it with the MASTER_KEY
  3. Compiles + exec()s the resulting code
  4. Returns the module object for use
"""
import io
import sys
import types
from pathlib import Path
from typing import Optional

from .config import BUNDLE_DIR, CONFIG_DIR


# Filled in by `license_keygen.py --show-master` on YOUR machine.
# Copy the hex string into here, then rebuild Quopus.
#
# The DEMO value below is for development with the demo license.
# If you ship it as-is, every customer ends up able to decrypt
# every premium module without a license. REPLACE before release.
QUOPUS_MASTER_KEY_HEX = (
    # 32 bytes - placeholder, regenerate with:
    #   python license_keygen.py --show-master
    "0000000000000000000000000000000000000000000000000000000000000000"
)

MAGIC = b"QPEv1" + b"\0" * 11   # 16 bytes, matches keygen tool


# Cache of already-loaded premium modules so we don't decrypt them
# more than once per process. Maps module name -> module object.
_loaded_premium: dict = {}


def _master_key() -> Optional[bytes]:
    """Return the master AES-256 key as bytes, or None if not set
    (default placeholder all-zeros means the build wasn't keyed).

    Lookup order (same logic as the public key in license.py):
      1. CONFIG_DIR/quopus_keys.cfg field 'master_key_hex'
      2. SCRIPT_DIR/quopus_keys.cfg
      3. The baked-in QUOPUS_MASTER_KEY_HEX constant

    Keeping the master key out of the source file means ZIP
    updates don't wipe a customized key. Users patch once via
    license_tool.py and it sticks through future updates."""
    # File-based override first
    for cfg_dir in (CONFIG_DIR, BUNDLE_DIR):
        cfg_path = cfg_dir / "quopus_keys.cfg"
        if not cfg_path.is_file():
            continue
        try:
            for raw in cfg_path.read_text(
                    encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key_name, _, val = line.partition("=")
                if key_name.strip().lower() in (
                        "master_key_hex", "master_key", "masterkey"):
                    val = val.strip().strip('"').strip("'")
                    if (len(val) == 64
                            and all(c in "0123456789abcdefABCDEF"
                                    for c in val)):
                        key = bytes.fromhex(val)
                        if key != b"\x00" * 32:
                            return key
        except OSError:
            continue
    # Fall back to baked-in
    try:
        key = bytes.fromhex(QUOPUS_MASTER_KEY_HEX)
    except ValueError:
        return None
    if len(key) != 32 or key == b"\x00" * 32:
        return None
    return key


def _decrypt_qpe(path: Path) -> Optional[bytes]:
    """Read a .qpe file, verify its header, decrypt the body.
    Returns the original .py bytes, or None if any step fails."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 16 + 12 + 16:   # magic + nonce + min(tag)
        return None
    if data[:16] != MAGIC:
        return None
    nonce = data[16:28]
    ciphertext = data[28:]
    key = _master_key()
    if key is None:
        return None
    try:
        aes = AESGCM(key)
        return aes.decrypt(nonce, ciphertext, associated_data=None)
    except Exception:
        # InvalidTag, wrong key, truncated file - all caught here.
        # No useful recovery, just refuse to load the module.
        return None


def _premium_dir() -> Path:
    """Where the .qpe files live."""
    return BUNDLE_DIR / "quopus_lib" / "_premium"


def has_premium_available() -> bool:
    """Quick check: do we have a master key configured AND at
    least one .qpe file on disk?"""
    if _master_key() is None:
        return False
    pd = _premium_dir()
    if not pd.is_dir():
        return False
    return any(pd.glob("*.qpe"))


def load_premium_module(short_name: str) -> Optional[types.ModuleType]:
    """Load a premium module by short name (e.g. "multi_sid").

    Lookup order:
      1. quopus_lib/_premium/<short_name>.qpe (release - encrypted)
      2. quopus_lib/_premium/<short_name>.py  (dev - plaintext)
      3. quopus_lib/<short_name>.py           (dev - normal location)

    The release path requires a valid license. The dev fallbacks
    do NOT - we want development to be friction-free, so plain
    .py files load unconditionally. This means a dev build is
    automatically a "registered" build for premium-feature
    testing, which is fine because dev builds never reach
    customers.

    To check whether you're testing with real protection, check
    has_premium_available() - it only returns True when there's
    a master key configured AND .qpe files on disk.

    Returns None if nothing is found or decryption fails."""
    # Lazy import to avoid circular dependency with license.py
    full_name = f"quopus_lib._premium.{short_name}"
    if full_name in _loaded_premium:
        return _loaded_premium[full_name]

    # Path 1: encrypted .qpe (release).
    # Decryption is NOT gated on license_status anymore. The
    # premium modules contain features that must run in trial
    # mode too - the license check happens INSIDE each module
    # at the feature-call site (license.has_feature(...) gates
    # the premium-only branches). What encryption protects is
    # the *source code*: a trial user gets a working binary but
    # cannot patch out the has_feature() checks because the
    # checks live inside a .qpe blob that requires the master
    # key to read. Patching the runtime decrypter also doesn't
    # help - without the master key it returns None.
    qpe_path = _premium_dir() / f"{short_name}.qpe"
    if qpe_path.is_file():
        source = _decrypt_qpe(qpe_path)
        if source is None:
            return None
        return _exec_module(full_name, source, str(qpe_path))

    # Path 2: dev plain .py inside _premium/
    dev_path = _premium_dir() / f"{short_name}.py"
    if dev_path.is_file():
        source = dev_path.read_bytes()
        return _exec_module(full_name, source, str(dev_path))

    # Path 3: dev plain .py in regular quopus_lib/ (most common
    # case while developing - the file just lives where it
    # always lived; you flip on the _premium/ path only when
    # you're ready to encrypt for release).
    try:
        from . import config
        regular_path = (config.BUNDLE_DIR / "quopus_lib"
                        / f"{short_name}.py")
        if regular_path.is_file():
            source = regular_path.read_bytes()
            return _exec_module(full_name, source,
                               str(regular_path))
    except Exception:
        pass

    return None


def _exec_module(full_name, source, file_for_traceback):
    """Compile + exec source into a fresh module object, cache it,
    return it. Shared between the encrypted and dev plaintext
    paths so they produce identical module objects.

    The full_name we use is always quopus_lib._premium.X so that
    code inside the module which does relative imports finds the
    right siblings regardless of where the source physically
    lived."""
    mod = types.ModuleType(full_name)
    mod.__file__ = file_for_traceback
    mod.__loader__ = None
    sys.modules[full_name] = mod
    try:
        code = compile(source, file_for_traceback, "exec")
        exec(code, mod.__dict__)
    except Exception:
        # Module raised during execution - clean up sys.modules
        # so a retry doesn't get the half-loaded version.
        sys.modules.pop(full_name, None)
        raise
    _loaded_premium[full_name] = mod
    return mod


def call_premium(short_name: str, attr: str, *args, **kwargs):
    """Convenience wrapper: load a premium module and call one of
    its attributes. Used by action handlers in actions.py for the
    common pattern of:

        if license.has_feature(license.FEATURE_TELNET):
            mod = crypto.load_premium_module("telnet_advanced")
            if mod:
                mod.run(...)

    becomes:

        crypto.call_premium("telnet_advanced", "run", ...)
    """
    mod = load_premium_module(short_name)
    if mod is None:
        return None
    fn = getattr(mod, attr, None)
    if fn is None:
        return None
    return fn(*args, **kwargs)
