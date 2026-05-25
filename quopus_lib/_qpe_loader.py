"""Import hook for Quopus-encrypted modules (.qpe files).

When the trial build ships, premium-feature-bearing modules are
replaced by AES-GCM-encrypted .qpe files sitting at the same
path as the original .py would have lived. The user's machine
has neither the source .py nor a network connection, so normal
Python imports can't find these modules.

This file installs a sys.meta_path finder/loader pair that:

  1. Sees an import like `from .telnet_client import ...` from
     somewhere inside the quopus_lib package
  2. Notices there's no telnet_client.py on disk - but there IS
     a telnet_client.qpe right next to where it would have been
  3. Decrypts the .qpe in memory using the runtime master key
     (loaded by crypto._master_key())
  4. Executes the decrypted source as the module body, returning
     a normal Python module object

Result: importing an encrypted module is indistinguishable from
importing a plain .py file - the consumer code never knows.

If the master key is missing, decryption fails, or the .qpe file
is corrupt, the loader returns None (signalling "I can't handle
this name") and Python falls through to the next finder on
sys.meta_path. With no other finder matching, ImportError is
raised in the usual way.

Importing this module installs the hook as a side-effect, so
just `import quopus_lib._qpe_loader` once at package startup is
all that's needed.

Dev workflow is unaffected: when a real .py file is present, the
default Python import finds it first (because we register our
hook AFTER the default ones in sys.meta_path), so the .qpe is
only consulted when the .py isn't there.
"""
import sys
import importlib.abc
import importlib.machinery
import importlib.util
from pathlib import Path


class _QpeLoader(importlib.abc.Loader):
    """Loader half of the meta-path pair. Given a path to a .qpe
    file and the fully-qualified module name, decrypts the bytes
    and executes them as a Python module body.
    """

    def __init__(self, fullname: str, qpe_path: Path):
        self.fullname = fullname
        self.qpe_path = qpe_path

    def create_module(self, spec):
        # Returning None means "use the default module-creation
        # logic" - that's what we want for a normal Python module.
        return None

    def exec_module(self, module):
        # Lazy-import crypto so this loader file itself stays
        # plain-text and self-contained at startup. Importing
        # crypto pulls in cryptography, which is heavyweight -
        # we only need it when actually decrypting a .qpe.
        from . import crypto
        source = crypto._decrypt_qpe(self.qpe_path)
        if source is None:
            # Common causes: missing master key, wrong key,
            # truncated/corrupt .qpe file. Without source bytes
            # there's nothing to exec - raise ImportError so the
            # caller sees a clear error instead of an empty
            # module object that would later AttributeError on
            # every access.
            raise ImportError(
                f"Failed to decrypt {self.qpe_path.name} - "
                f"check that license_keygen_master.key matches "
                f"the one used to encrypt, or that the .qpe "
                f"file isn't corrupt")
        # Make the module aware of its own location so
        # __file__-based logic (e.g. resource lookups relative
        # to the module) keeps working. We point at the .qpe
        # itself rather than a fake .py path - opening that
        # file would return ciphertext, but at least the
        # filename is honest.
        module.__file__ = str(self.qpe_path)
        # Compile + exec the source in the module's namespace.
        # The compile() step picks up syntax errors with a
        # filename pointer the user can act on.
        code = compile(source, str(self.qpe_path), 'exec')
        exec(code, module.__dict__)


class _QpeFinder(importlib.abc.MetaPathFinder):
    """Finder half. Sits on sys.meta_path. Asked whether it
    knows how to load a given fullname. Returns a ModuleSpec
    pointing at our _QpeLoader if there's a .qpe file at the
    expected location, None otherwise.

    We only handle names under the `quopus_lib.` namespace. A
    generic finder that scanned arbitrary paths would be both
    slower and a security risk: a stray .qpe file in an unrelated
    site-packages directory should not be silently executed.
    """

    PACKAGE_PREFIX = "quopus_lib."

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith(self.PACKAGE_PREFIX):
            return None
        # Build the on-disk path the matching .qpe would live at.
        # fullname is "quopus_lib.telnet_client" -> look for
        # <pkg_dir>/telnet_client.qpe. Nested submodules work too:
        # "quopus_lib._premium.foo" -> <pkg_dir>/_premium/foo.qpe.
        rel_parts = fullname.split(".")[1:]   # strip "quopus_lib"
        if not rel_parts:
            return None
        # path arg is the search path for the parent package - if
        # provided we use that, otherwise fall back to this
        # package's own __path__ entries.
        if path is None:
            try:
                import quopus_lib
                search_paths = list(quopus_lib.__path__)
            except (ImportError, AttributeError):
                return None
        else:
            search_paths = list(path)
        # Subpackages: only the LAST name component is the actual
        # module file; the leading parts are subdirectories that
        # have to already exist as packages on disk.
        leaf = rel_parts[-1]
        for sp in search_paths:
            qpe_path = Path(sp) / f"{leaf}.qpe"
            if qpe_path.is_file():
                loader = _QpeLoader(fullname, qpe_path)
                spec = importlib.machinery.ModuleSpec(
                    name=fullname, loader=loader,
                    origin=str(qpe_path))
                return spec
        return None


def _install():
    """Install the .qpe finder on sys.meta_path. Called once at
    module import time. Idempotent - if already installed,
    re-importing this module is a no-op.
    """
    for existing in sys.meta_path:
        if isinstance(existing, _QpeFinder):
            return  # already installed
    # Append (not prepend) - we want the default finders to win
    # whenever there's a real .py file present. Only when the
    # default lookup fails do we get consulted, which is exactly
    # the "ship .qpe instead of .py" semantics we want without
    # breaking dev workflows.
    sys.meta_path.append(_QpeFinder())


# Install at module import time - importing this file is the
# trigger.
_install()
