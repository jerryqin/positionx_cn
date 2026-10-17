"""Fixture-only loader. There is deliberately no real SDK import implementation."""

import hashlib
import contextlib
import importlib.abc
import sys
from pathlib import Path

from tools.full_qmt_readonly_probe_v01.evidence import verify_directory


class ContractViolation(RuntimeError):
    pass


class RealSDKDisabled(ContractViolation):
    pass


SELECTION_KEYS = {"python", "sdk_root", "module", "native_module", "dll_roots", "dll_files"}
ALLOWED_EFFECTS = {"fixture_import", "fixture_file_read"}


class DenyRealSDK(importlib.abc.MetaPathFinder):
    def __init__(self, on_denied=None):
        self.on_denied = on_denied

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "xtquant" or fullname.startswith("xtquant."):
            if self.on_denied is not None:
                self.on_denied()
            raise RealSDKDisabled("Real SDK import forbidden in offline harness")
        return None


@contextlib.contextmanager
def deny_real_imports(on_denied=None):
    if any(name == "xtquant" or name.startswith("xtquant.") for name in sys.modules):
        if on_denied is not None:
            on_denied()
        raise RealSDKDisabled("Offline interpreter already contains an SDK")
    guard = DenyRealSDK(on_denied)
    sys.meta_path.insert(0, guard)
    try:
        yield
    finally:
        sys.meta_path.remove(guard)


def explicit_selection(selection):
    if type(selection) is not dict or set(selection) != SELECTION_KEYS:
        raise ContractViolation("Explicit single selection required; no fallback")
    paths = {}
    for name in ("python", "sdk_root", "module", "native_module"):
        value = selection[name]
        if type(value) is not str or value.startswith("\\\\") or not Path(value).is_absolute():
            raise ContractViolation("Absolute local selection required")
        path = Path(value)
        verify_directory(path if name == "sdk_root" else path.parent)
        if path.is_symlink() or not (path.is_dir() if name == "sdk_root" else path.is_file()):
            raise ContractViolation("Invalid selection identity")
        if name != "sdk_root" and (path.stat().st_nlink != 1 or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ContractViolation("Linked SDK file rejected")
        paths[name] = path.resolve(strict=True)
    for name in ("dll_roots", "dll_files"):
        values = selection[name]
        if type(values) is not list or not values or any(type(v) is not str for v in values):
            raise ContractViolation("Explicit DLL roots and files required")
        resolved = []
        for value in values:
            path = Path(value)
            if not path.is_absolute() or value.startswith("\\\\"):
                raise ContractViolation("Local DLL selection required")
            verify_directory(path if name == "dll_roots" else path.parent)
            if path.is_symlink() or not (path.is_dir() if name == "dll_roots" else path.is_file()):
                raise ContractViolation("Invalid DLL identity")
            if name == "dll_files" and (path.stat().st_nlink != 1 or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise ContractViolation("Linked DLL rejected")
            resolved.append(path.resolve(strict=True))
        if len(set(resolved)) != len(resolved):
            raise ContractViolation("Duplicate DLL selection")
        paths[name] = resolved
    if any(paths["sdk_root"] not in paths[name].parents for name in ("module", "native_module")):
        raise ContractViolation("SDK module outside approved root")
    if any(not any(root in file.parents for root in paths["dll_roots"]) for file in paths["dll_files"]):
        raise ContractViolation("DLL outside approved roots")
    return paths


def file_identity(path, deadline):
    deadline.check()
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            deadline.check()
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ContractViolation("File changed during identity capture")
    deadline.check()
    return {"sha256": digest.hexdigest(), "file_id": [after.st_dev, after.st_ino], "size": after.st_size}


def selection_inventory(selection, redactor, deadline):
    paths = explicit_selection(selection)
    files = {name: paths[name] for name in ("python", "module", "native_module")}
    files.update({"dll." + str(i): path for i, path in enumerate(paths["dll_files"])})
    roots = {"sdk_root": paths["sdk_root"], **{"dll_root." + str(i): path
                                              for i, path in enumerate(paths["dll_roots"])}}
    return {"files": {name: {**file_identity(path, deadline), "path_token": redactor.token(str(path))}
                      for name, path in files.items()},
            "roots": {name: {"file_id": [path.stat().st_dev, path.stat().st_ino],
                             "path_token": redactor.token(str(path))} for name, path in roots.items()}}


def load_fixture(mode, selection, boundary, factory):
    # Even an alleged approval cannot turn this offline version into a real loader.
    if mode != "fixture":
        raise RealSDKDisabled("Real SDK loading is not implemented or authorized")
    if (set(boundary) != {"stdout_captured", "stderr_captured", "evidence_ready", "deadline"}
            or any(boundary[name] is not True for name in ("stdout_captured", "stderr_captured", "evidence_ready"))):
        raise ContractViolation("Import boundary absent")
    boundary["deadline"].check()
    explicit_selection(selection)
    sdk = factory()
    if getattr(sdk, "fixture_kind", None) != "HARNESS_FIXTURE":
        raise ContractViolation("Unrecognized fixture")
    boundary["deadline"].check()
    return sdk


class ReadOnlyGate:
    """A closed command dispatcher, not a sandbox for malicious vendor code."""

    ORDER = ("construct", "register_callback", "start", "connect", "construct_account", "subscribe",
             "query_asset", "query_positions", "query_orders", "query_trades", "bounded_callback_observation", "stop")

    def __init__(self, sdk, account_id, session_id, violation):
        import threading
        self._sdk, self._account_id, self._session = sdk, account_id, session_id
        self._violation, self._owner = violation, threading.get_ident()
        self._local = threading.local()
        self._index, self._stopped = 0, False
        self.trader = self.account = None
        self.callback = None
        self.observe = None

    def invoke(self, name):
        import threading
        permitted = (type(name) is str and name in self.ORDER and not self._stopped
                     and not getattr(self._local, "callback", False) and threading.get_ident() == self._owner)
        permitted &= name == "stop" or (self._index < len(self.ORDER) and name == self.ORDER[self._index])
        if not permitted:
            self._violation(name)
            raise ContractViolation("SDK scope violation")
        if name == "stop":
            self._stopped = True
            if type(self.trader) is not self._sdk.TraderType:
                raise ContractViolation("No owned trader to stop")
            return self.trader.stop()
        self._index += 1
        if name == "construct":
            self.trader = self._sdk.Trader("FIXTURE_USERDATA", self._session)
            return self.trader
        if name == "construct_account":
            self.account = self._sdk.Account(self._account_id, "STOCK")
            return self.account
        if name == "bounded_callback_observation":
            return self.observe()
        if name == "subscribe":
            if (type(self.account) is not self._sdk.AccountType
                    or type(self.account.account_id) is not str or self.account.account_id != self._account_id
                    or self.account.account_type != "STOCK"):
                self._violation("account_binding")
                raise ContractViolation("Account identity mismatch")
            return self.trader.subscribe(self.account)
        if name.startswith("query_"):
            if (type(self.account) is not self._sdk.AccountType or type(self.account.account_id) is not str
                    or self.account.account_id != self._account_id or self.account.account_type != "STOCK"):
                self._violation("account_binding")
                raise ContractViolation("Query account identity mismatch")
        methods = {"register_callback": lambda: self.trader.register_callback(self.callback),
                   "start": lambda: self.trader.start(), "connect": lambda: self.trader.connect(),
                   "query_asset": lambda: self.trader.query_asset(self.account),
                   "query_positions": lambda: self.trader.query_positions(self.account),
                   "query_orders": lambda: self.trader.query_orders(self.account),
                   "query_trades": lambda: self.trader.query_trades(self.account)}
        return methods[name]()

    def callback_call(self, operation):
        previous = getattr(self._local, "callback", False)
        self._local.callback = True
        try:
            return operation()
        finally:
            self._local.callback = previous
